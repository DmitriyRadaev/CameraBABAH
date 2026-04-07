"""
camera_recorder.py - Запись камеры + экран + звук.
Папки: Desktop/VR/<ID>_<priyom>/

Установка:
    pip install opencv-python mss numpy sounddevice scipy
    ffmpeg в PATH (для склейки видео+аудио)
"""

import cv2
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path
import json
import os
import time
import re
import numpy as np
import subprocess
import wave

try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

CONFIG_FILE      = "recorder_config.json"
DESKTOP_VR       = Path.home() / "Desktop" / "VR"
AUDIO_SAMPLERATE = 44100
AUDIO_CHANNELS   = 1   # безопаснее — каналы определяются из устройства


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"camera_index": 1}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def draw_timestamp(frame):
    ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S.%f")[:-3]
    cv2.putText(frame, ts, (20, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                (255, 255, 255), 3, cv2.LINE_AA)
    return frame

def get_next_session(patient_id: str) -> int:
    """Находит следующий номер приёма для пациента"""
    DESKTOP_VR.mkdir(parents=True, exist_ok=True)
    existing = []
    pattern  = re.compile(rf"^{re.escape(patient_id)}_(\d+)$")
    for d in DESKTOP_VR.iterdir():
        if d.is_dir():
            m = pattern.match(d.name)
            if m:
                existing.append(int(m.group(1)))
    return max(existing) + 1 if existing else 0

def ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


class RecorderApp:
    def __init__(self, root):
        self.root            = root
        self.root.title("VR Session Recorder")
        self.root.resizable(False, False)

        self.cap             = None
        self.out_cam         = None
        self.out_scr         = None
        self.recording       = False
        self.start_time      = None
        self.file_cam        = None
        self.file_cam_noaudio= None
        self.file_scr        = None
        self.file_scr_noaudio= None
        self.file_wav        = None
        self.timer_job       = None
        self._active_threads = 0
        self._threads_lock   = threading.Lock()
        self._audio_frames   = []

        self.cfg = load_config()
        self._build_ui()

    # ── UI ──────────────────────────────────
    def _build_ui(self):
        PAD = dict(padx=10, pady=6)

        # ── Пациент + приём ──
        frame_patient = ttk.LabelFrame(self.root, text="Пациент")
        frame_patient.pack(fill="x", **PAD)

        ttk.Label(frame_patient, text="ID пациента:").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        self.patient_id_var = tk.StringVar()
        self.patient_id_var.trace_add("write", self._on_patient_id_change)

        self.id_entry = ttk.Entry(frame_patient, textvariable=self.patient_id_var,
                                  width=12, font=("Segoe UI", 11))
        self.id_entry.grid(row=0, column=1, padx=4, pady=4, sticky="w")

        ttk.Label(frame_patient, text="(любые символы)",
                  foreground="gray").grid(row=0, column=2, padx=8, sticky="w")

        ttk.Label(frame_patient, text="Номер приёма:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        self.session_var = tk.IntVar(value=0)
        self.session_spin = tk.Spinbox(frame_patient, from_=0, to=999, width=5,
                                       textvariable=self.session_var,
                                       font=("Segoe UI", 11))
        self.session_spin.grid(row=1, column=1, padx=4, pady=4, sticky="w")
        self.session_hint = ttk.Label(frame_patient, text="", foreground="gray")
        self.session_hint.grid(row=1, column=2, padx=8, sticky="w")

        ttk.Label(frame_patient, text="Папка сессии:").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        self.folder_label = ttk.Label(frame_patient, text="—", foreground="#2980b9",
                                      font=("Segoe UI", 9))
        self.folder_label.grid(row=2, column=1, columnspan=2, padx=4, sticky="w")

        # ── Камера ──
        frame_cam = ttk.LabelFrame(self.root, text="Камера")
        frame_cam.pack(fill="x", **PAD)
        ttk.Label(frame_cam, text="Индекс:").pack(side="left", padx=8)
        self.cam_idx_var = tk.IntVar(value=self.cfg.get("camera_index", 1))
        tk.Spinbox(frame_cam, from_=0, to=5, width=4,
                   textvariable=self.cam_idx_var,
                   font=("Segoe UI", 11)).pack(side="left", padx=4)
        ttk.Label(frame_cam, text="(0 = вебка, 1 = карта захвата)",
                  foreground="gray").pack(side="left", padx=8)

        # ── Звук ──
        frame_aud = ttk.LabelFrame(self.root, text="Звук")
        frame_aud.pack(fill="x", **PAD)

        row0 = tk.Frame(frame_aud)
        row0.pack(fill="x")
        self.record_audio_var = tk.BooleanVar(value=True)
        aud_check = ttk.Checkbutton(row0, text="Записывать звук",
                                     variable=self.record_audio_var)
        aud_check.pack(side="left", padx=8, pady=6)
        if not AUDIO_AVAILABLE:
            ttk.Label(row0, text="⚠ pip install sounddevice scipy",
                      foreground="red").pack(side="left", padx=8)
            aud_check.config(state="disabled")
        if not ffmpeg_available():
            ttk.Label(row0, text="⚠ ffmpeg не найден (нужен для звука)",
                      foreground="orange").pack(side="left", padx=8)

        # Строка выбора микрофона
        row1 = tk.Frame(frame_aud)
        row1.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row1, text="Микрофон:").pack(side="left")

        self.mic_names   = []
        self.mic_indices = []
        self.mic_var     = tk.StringVar()

        if AUDIO_AVAILABLE:
            self._populate_mics()

        self.mic_combo = ttk.Combobox(row1, textvariable=self.mic_var,
                                       values=self.mic_names, width=46,
                                       state="readonly")
        self.mic_combo.pack(side="left", padx=6)
        if self.mic_names:
            self.mic_combo.current(0)

        ttk.Button(row1, text="↻", width=3,
                   command=self._refresh_mics).pack(side="left", padx=2)

        # ── Экран ──
        frame_scr = ttk.LabelFrame(self.root, text="Запись экрана")
        frame_scr.pack(fill="x", **PAD)
        top = tk.Frame(frame_scr)
        top.pack(fill="x")
        self.record_screen_var = tk.BooleanVar(value=False)
        scr_check = ttk.Checkbutton(top, text="Записывать экран",
                                     variable=self.record_screen_var,
                                     command=self._on_screen_toggle)
        scr_check.pack(side="left", padx=8, pady=6)
        if not MSS_AVAILABLE:
            ttk.Label(top, text="⚠ pip install mss",
                      foreground="red").pack(side="left", padx=8)
            scr_check.config(state="disabled")

        self.monitor_row = tk.Frame(frame_scr)
        ttk.Label(self.monitor_row, text="Монитор:").pack(side="left", padx=8)
        self.monitor_var = tk.IntVar(value=1)
        tk.Spinbox(self.monitor_row, from_=1, to=4, width=3,
                   textvariable=self.monitor_var,
                   font=("Segoe UI", 11)).pack(side="left", padx=4)
        ttk.Label(self.monitor_row, text="(1 = основной)",
                  foreground="gray").pack(side="left")
        self._on_screen_toggle()

        # ── Статус ──
        frame_status = ttk.LabelFrame(self.root, text="Статус")
        frame_status.pack(fill="x", **PAD)
        self.status_label = ttk.Label(frame_status, text="⏹  Готов к записи",
                                      font=("Segoe UI", 11))
        self.status_label.pack(pady=4)
        self.timer_label = ttk.Label(frame_status, text="00:00:00",
                                     font=("Segoe UI", 24, "bold"))
        self.timer_label.pack(pady=2)
        self.info_label = ttk.Label(frame_status, text="", foreground="gray",
                                    font=("Segoe UI", 9))
        self.info_label.pack(pady=2)

        # ── Кнопки ──
        frame_btn = tk.Frame(self.root)
        frame_btn.pack(pady=10)
        self.btn_start = tk.Button(
            frame_btn, text="▶  НАЧАТЬ ЗАПИСЬ",
            bg="#27ae60", fg="white", font=("Segoe UI", 13, "bold"),
            width=18, height=2, command=self._start)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = tk.Button(
            frame_btn, text="■  ОСТАНОВИТЬ",
            bg="#e74c3c", fg="white", font=("Segoe UI", 13, "bold"),
            width=18, height=2, command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        # ── Лог ──
        frame_log = ttk.LabelFrame(self.root, text="Лог")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text = tk.Text(frame_log, height=8, state="disabled",
                                font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame_log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Микрофоны ───────────────────────────
    def _populate_mics(self):
        """Заполняет списки mic_names / mic_indices устройствами-входами"""
        self.mic_names   = []
        self.mic_indices = []
        try:
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    ch   = d["max_input_channels"]
                    sr   = int(d["default_samplerate"])
                    name = f"{d['name']}  [{ch}ch / {sr}Hz]"
                    self.mic_names.append(name)
                    self.mic_indices.append(i)
        except Exception as e:
            self._log(f"⚠ Не удалось получить список микрофонов: {e}")

    def _refresh_mics(self):
        self._populate_mics()
        self.mic_combo.config(values=self.mic_names)
        if self.mic_names:
            self.mic_combo.current(0)
        self._log(f"🎙 Найдено микрофонов: {len(self.mic_names)}")

    # ── Валидация / автозаполнение ───────────
    def _validate_digits(self, value):
        return value == "" or value.isdigit()

    def _on_patient_id_change(self, *_):
        pid = self.patient_id_var.get().strip()
        if pid:
            next_s = get_next_session(pid)
            self.session_var.set(next_s)
            hint = "новая папка" if next_s == 0 else f"последний приём был {next_s - 1}"
            self.session_hint.config(text=f"← {hint}")
            folder = DESKTOP_VR / f"{pid}_{next_s}"
            self.folder_label.config(text=str(folder))
        else:
            self.session_hint.config(text="")
            self.folder_label.config(text="—")

    def _on_screen_toggle(self):
        if self.record_screen_var.get() and MSS_AVAILABLE:
            self.monitor_row.pack(fill="x", pady=4)
        else:
            self.monitor_row.pack_forget()

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _log_t(self, msg):
        self.root.after(0, lambda: self._log(msg))

    # ── Старт ───────────────────────────────
    def _start(self):
        patient_id = self.patient_id_var.get().strip()
        if not patient_id:
            messagebox.showerror("Ошибка", "Введите ID пациента.")
            return

        session_num  = self.session_var.get()
        output_dir   = DESKTOP_VR / f"{patient_id}_{session_num}"
        if output_dir.exists():
            if not messagebox.askyesno("Папка уже существует",
                f"Папка {output_dir.name} уже есть.\n"
                "Продолжить и сохранить туда же?"):
                return
        output_dir.mkdir(parents=True, exist_ok=True)

        cam_idx  = self.cam_idx_var.get()
        self.cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            messagebox.showerror("Ошибка",
                f"Не удалось подключиться к камере [{cam_idx}].")
            return

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1920)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.cap.set(cv2.CAP_PROP_BRIGHTNESS, 128)
        self.cap.set(cv2.CAP_PROP_CONTRAST,   128)
        self.cap.set(cv2.CAP_PROP_SATURATION,  128)

        frame_w  = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h  = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        real_fps = self.cap.get(cv2.CAP_PROP_FPS)
        if real_fps <= 0 or real_fps > 120:
            real_fps = 29.9

        self.start_time    = datetime.now()
        ts_str             = self.start_time.strftime("%Y-%m-%d_%H-%M-%S")
        start_str          = self.start_time.strftime("%H:%M:%S.%f")[:-3]

        record_audio  = self.record_audio_var.get() and AUDIO_AVAILABLE and ffmpeg_available()
        record_screen = self.record_screen_var.get() and MSS_AVAILABLE

        base = f"{patient_id}_{session_num}_{ts_str}"
        if record_audio:
            self.file_cam_noaudio = str(output_dir / f"{base}_camera_raw.avi")
            self.file_cam         = str(output_dir / f"{base}_camera.avi")
            self.file_wav         = str(output_dir / f"{base}_audio.wav")
            if record_screen:
                self.file_scr_noaudio = str(output_dir / f"{base}_screen_raw.avi")
                self.file_scr         = str(output_dir / f"{base}_screen.avi")
        else:
            self.file_cam_noaudio = None
            self.file_cam         = str(output_dir / f"{base}_camera.avi")
            self.file_wav         = None
            if record_screen:
                self.file_scr_noaudio = None
                self.file_scr         = str(output_dir / f"{base}_screen.avi")

        cam_write = self.file_cam_noaudio if record_audio else self.file_cam
        fourcc    = cv2.VideoWriter_fourcc(*"MJPG")
        self.out_cam = cv2.VideoWriter(cam_write, fourcc, real_fps,
                                       (frame_w, frame_h), isColor=True)

        with open(str(output_dir / f"{base}_meta.txt"), "w", encoding="utf-8") as f:
            f.write(f"PATIENT_ID={patient_id}\n")
            f.write(f"SESSION={session_num}\n")
            f.write(f"VIDEO_START={start_str}\n")
            f.write(f"DATE={self.start_time.strftime('%Y-%m-%d')}\n")
            f.write(f"VIDEO_FILE_CAMERA={Path(self.file_cam).name}\n")
            if record_screen:
                f.write(f"VIDEO_FILE_SCREEN={Path(self.file_scr).name}\n")
            f.write(f"AUDIO={'yes' if record_audio else 'no'}\n")

        self.recording       = True
        self._active_threads = 0
        self._audio_frames   = []

        self._log(f"Пациент: {patient_id}  |  Приём: {session_num}")
        self._log(f"Папка: {output_dir}")
        self._log(f"Старт: {start_str}")
        self._log(f"Камера [{cam_idx}]: {frame_w}x{frame_h} @ {real_fps:.1f}fps")

        with self._threads_lock:
            self._active_threads += 1
        threading.Thread(target=self._camera_loop, daemon=True).start()

        if record_audio:
            self._log("🎙 Запись звука включена")
            with self._threads_lock:
                self._active_threads += 1
            threading.Thread(target=self._audio_loop, daemon=True).start()

        if record_screen:
            monitor_num = self.monitor_var.get()
            self._log(f"🖥 Запись экрана: монитор {monitor_num}")
            with self._threads_lock:
                self._active_threads += 1
            threading.Thread(target=self._screen_loop,
                             args=(monitor_num, real_fps, record_audio),
                             daemon=True).start()

        info_parts = [f"Пациент {patient_id} | приём {session_num}",
                      f"{frame_w}x{frame_h} @ {real_fps:.1f}fps"]
        if record_audio:  info_parts.append("🎙 звук")
        if record_screen: info_parts.append("🖥 экран")
        self.info_label.config(text="  |  ".join(info_parts))

        self.cfg["camera_index"] = cam_idx
        save_config(self.cfg)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status_label.config(
            text=f"🔴  Запись  |  {patient_id} / приём {session_num}", foreground="red")
        self._update_timer()

    # ── Потоки записи ───────────────────────
    def _camera_loop(self):
        while self.recording:
            ret, frame = self.cap.read()
            if not ret:
                self._log_t("❌ Камера: нет кадра.")
                break
            draw_timestamp(frame)
            self.out_cam.write(frame)
        self.cap.release()
        self.out_cam.release()
        self.cap = self.out_cam = None
        self._log_t("📷 Камера остановлена.")
        self._thread_finished()

    def _audio_loop(self):
        """Запись звука — устройство и каналы берутся из выпадающего списка"""
        # Определяем устройство
        sel = self.mic_combo.current()
        if sel >= 0 and sel < len(self.mic_indices):
            device_idx = self.mic_indices[sel]
        else:
            device_idx = None  # системный по умолчанию

        # Определяем число каналов из устройства
        try:
            dev_info = sd.query_devices(device_idx, "input")
            channels = min(dev_info["max_input_channels"], 2)
            channels = max(channels, 1)
            dev_name = dev_info["name"]
        except Exception:
            channels   = 1
            device_idx = None
            dev_name   = "по умолчанию"

        self._log_t(f"🎙 Устройство: {dev_name} | каналы: {channels}")

        def callback(indata, frames, time_info, status):
            if self.recording:
                self._audio_frames.append(indata.copy())

        try:
            with sd.InputStream(device=device_idx,
                                samplerate=AUDIO_SAMPLERATE,
                                channels=channels,
                                callback=callback):
                while self.recording:
                    time.sleep(0.1)
        except Exception as e:
            self._log_t(f"⚠ Ошибка звука: {e}")

        # Сохраняем WAV
        try:
            if self._audio_frames:
                audio_data = np.concatenate(self._audio_frames, axis=0)
                audio_int  = (audio_data * 32767).astype(np.int16)
                with wave.open(self.file_wav, "w") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)
                    wf.setframerate(AUDIO_SAMPLERATE)
                    wf.writeframes(audio_int.tobytes())
                self._log_t(f"🎙 Аудио сохранено: {Path(self.file_wav).name}")
        except Exception as e:
            self._log_t(f"⚠ Ошибка сохранения аудио: {e}")

        self._log_t("🎙 Запись звука остановлена.")
        self._thread_finished()

    def _screen_loop(self, monitor_num, fps, with_audio):
        frame_interval = 1.0 / fps
        scr_write = self.file_scr_noaudio if with_audio else self.file_scr

        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_num >= len(monitors):
                monitor_num = 1
            mon   = monitors[monitor_num]
            scr_w, scr_h = mon["width"], mon["height"]

            fourcc       = cv2.VideoWriter_fourcc(*"MJPG")
            self.out_scr = cv2.VideoWriter(scr_write, fourcc, fps,
                                           (scr_w, scr_h), isColor=True)
            self._log_t(f"🖥 Экран: {scr_w}x{scr_h}")

            while self.recording:
                t0    = time.time()
                frame = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
                draw_timestamp(frame)
                self.out_scr.write(frame)
                wait = frame_interval - (time.time() - t0)
                if wait > 0:
                    time.sleep(wait)

        if self.out_scr:
            self.out_scr.release()
            self.out_scr = None
        self._log_t("🖥 Экран остановлен.")
        self._thread_finished()

    # ── Склейка видео + аудио ───────────────
    def _merge_audio_video(self, video_raw, video_out, wav_file):
        if not os.path.exists(video_raw) or not os.path.exists(wav_file):
            return False
        cmd = [
            "ffmpeg", "-y",
            "-i", video_raw,
            "-i", wav_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            video_out
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            os.remove(video_raw)
            return True
        else:
            self._log(f"⚠ ffmpeg ошибка при склейке: {result.stderr.decode()[-200:]}")
            return False

    # ── Синхронизация потоков ───────────────
    def _thread_finished(self):
        with self._threads_lock:
            self._active_threads -= 1
            all_done = self._active_threads == 0
        if all_done:
            self.root.after(0, self._finalize)

    def _finalize(self):
        record_audio  = self.record_audio_var.get() and AUDIO_AVAILABLE and ffmpeg_available()
        record_screen = self.record_screen_var.get() and MSS_AVAILABLE

        if record_audio and self.file_wav and self.file_cam_noaudio:
            self._log("🔀 Склеиваю видео + аудио...")
            ok = self._merge_audio_video(
                self.file_cam_noaudio, self.file_cam, self.file_wav)
            if ok:
                self._log(f"✅ Камера с аудио: {Path(self.file_cam).name}")
            if record_screen and self.file_scr_noaudio:
                ok2 = self._merge_audio_video(
                    self.file_scr_noaudio, self.file_scr, self.file_wav)
                if ok2:
                    self._log(f"✅ Экран с аудио: {Path(self.file_scr).name}")
            self._log(f"💾 Аудио WAV: {Path(self.file_wav).name}")

        self._on_stopped()

    def _on_stopped(self):
        if self.timer_job:
            self.root.after_cancel(self.timer_job)

        duration  = (datetime.now() - self.start_time).total_seconds()
        start_str = self.start_time.strftime("%H:%M:%S.%f")[:-3]

        self._log(f"✅ Готово. Длительность: {duration:.1f}s")
        self._log("─" * 48)
        self._log("Команда для обрезки по CSV:")
        self._log(f'python trim_video.py --csv ОТЧЁТ.csv --video "{Path(self.file_cam).name}" --video-start {start_str}')

        self.status_label.config(text="✅  Запись сохранена", foreground="green")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.timer_label.config(text="00:00:00")
        self.info_label.config(text="")
        self._on_patient_id_change()

    def _update_timer(self):
        if self.recording and self.start_time:
            elapsed = int((datetime.now() - self.start_time).total_seconds())
            h, rem  = divmod(elapsed, 3600)
            m, s    = divmod(rem, 60)
            self.timer_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.timer_job = self.root.after(1000, self._update_timer)

    def _stop(self):
        self._log("Останавливаю...")
        self.recording = False

    def _on_close(self):
        if self.recording:
            if messagebox.askyesno("Выход", "Запись ещё идёт. Остановить и выйти?"):
                self._stop()
                self.root.after(1500, self.root.destroy)
        else:
            self.root.destroy()


if __name__ == "__main__":
    missing = []
    for pkg, name in [("cv2", "opencv-python"), ("numpy", "numpy")]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(name)
    if missing:
        print(f"❌ Установи: pip install {' '.join(missing)}")
        input("Нажми Enter...")
        exit(1)

    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()