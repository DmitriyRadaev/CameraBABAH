"""
trim_video.py — Обрезка видео по времени старта/конца VR сцены (ЕМДР) из CSV лога.

Использование:
    python trim_video.py --csv отчёт.csv --video запись.mp4

Требования:
    - ffmpeg установлен и доступен в PATH
    - pip install pandas  (если ещё не установлен)

Что делает скрипт:
    1. Читает CSV лог сессии (кодировка windows-1251, разделитель ;)
    2. Находит первую и последнюю строку с модулем "ЕМДР"
    3. Вычисляет смещение относительно начала видео (нужно ввести время старта записи)
    4. Обрезает видео через ffmpeg без перекодирования (быстро, без потери качества)
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


# ─────────────────────────────────────────────
# Парсинг аргументов
# ─────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Обрезка видео по старту ЕМДР-сцены из CSV лога"
    )
    parser.add_argument("--csv",   required=True,  help="Путь к CSV файлу отчёта")
    parser.add_argument("--video", required=True,  help="Путь к видеофайлу")
    parser.add_argument(
        "--video-start",
        default=None,
        help=(
            "Время начала видеозаписи в формате HH:MM:SS или HH:MM:SS.mmm "
            "(если не указано — спросит вручную)"
        ),
    )
    parser.add_argument(
        "--pad-before",
        type=float,
        default=2.0,
        help="Секунд добавить ДО старта ЕМДР (по умолчанию: 2)"
    )
    parser.add_argument(
        "--pad-after",
        type=float,
        default=2.0,
        help="Секунд добавить ПОСЛЕ конца ЕМДР (по умолчанию: 2)"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Путь к выходному файлу (по умолчанию: <имя_видео>_trimmed.mp4)"
    )
    return parser.parse_args()


# ─────────────────────────────────────────────
# Чтение CSV
# ─────────────────────────────────────────────
def load_csv(csv_path: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(
            csv_path,
            sep=";",
            encoding="windows-1251",
            header=0,
            dtype=str,
        )
        # Убираем лишние пробелы в названиях колонок
        df.columns = df.columns.str.strip()
        return df
    except Exception as e:
        print(f"[ОШИБКА] Не удалось прочитать CSV: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────
# Поиск границ ЕМДР сцены
# ─────────────────────────────────────────────
def find_emdr_boundaries(df: pd.DataFrame):
    emdr_rows = df[df["Модуль"].str.strip() == "ЕМДР"]

    if emdr_rows.empty:
        print("[ОШИБКА] В CSV не найдено ни одной строки с модулем 'ЕМДР'.")
        print("         Проверьте правильность файла.")
        sys.exit(1)

    time_start_str = emdr_rows.iloc[0]["Время"].strip()
    time_end_str   = emdr_rows.iloc[-1]["Время"].strip()

    print(f"\n[CSV] Найдено {len(emdr_rows)} строк ЕМДР")
    print(f"[CSV] Старт ЕМДР : {time_start_str}")
    print(f"[CSV] Конец ЕМДР : {time_end_str}")

    return time_start_str, time_end_str


# ─────────────────────────────────────────────
# Парсинг времени
# ─────────────────────────────────────────────
def parse_time(time_str: str) -> datetime:
    """Поддерживает форматы HH:MM:SS и HH:MM:SS.mmm"""
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue
    print(f"[ОШИБКА] Не удалось распознать время: '{time_str}'")
    print("         Используйте формат HH:MM:SS или HH:MM:SS.mmm (например 16:12:09 или 16:12:09.415)")
    sys.exit(1)


# ─────────────────────────────────────────────
# Вычисление смещений в видео
# ─────────────────────────────────────────────
def compute_offsets(
    video_start_str: str,
    emdr_start_str: str,
    emdr_end_str: str,
    pad_before: float,
    pad_after: float,
):
    video_start = parse_time(video_start_str)
    emdr_start  = parse_time(emdr_start_str)
    emdr_end    = parse_time(emdr_end_str)

    offset_start = (emdr_start - video_start).total_seconds() - pad_before
    offset_end   = (emdr_end   - video_start).total_seconds() + pad_after

    if offset_start < 0:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Вычисленный старт обрезки отрицательный ({offset_start:.3f}s).")
        print("                 Проверьте время начала видеозаписи.")
        offset_start = 0.0

    duration = offset_end - offset_start

    print(f"\n[Синхронизация]")
    print(f"  Старт видеозаписи  : {video_start_str}")
    print(f"  Старт ЕМДР в CSV   : {emdr_start_str}")
    print(f"  Конец ЕМДР в CSV   : {emdr_end_str}")
    print(f"  Отступ до          : -{pad_before}s")
    print(f"  Отступ после       : +{pad_after}s")
    print(f"\n  ✂  Обрезать с      : {offset_start:.3f}s")
    print(f"  ✂  Длительность    : {duration:.3f}s  (~{duration/60:.1f} мин)")

    return offset_start, duration


# ─────────────────────────────────────────────
# Обрезка через ffmpeg
# ─────────────────────────────────────────────
def trim_video(
    video_path: str,
    output_path: str,
    offset_start: float,
    duration: float,
):
    cmd = [
        "ffmpeg",
        "-y",                          # перезаписать без вопросов
        "-ss", f"{offset_start:.3f}",  # старт
        "-i", video_path,              # входной файл
        "-t", f"{duration:.3f}",       # длительность
        "-c", "copy",                  # без перекодирования (быстро!)
        output_path,
    ]

    print(f"\n[ffmpeg] Запускаю обрезку...")
    print(f"[ffmpeg] Команда: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode == 0:
        print(f"\n[ГОТОВО] Файл сохранён: {output_path}")
    else:
        print(f"\n[ОШИБКА] ffmpeg завершился с ошибкой (код {result.returncode})")
        sys.exit(1)


# ─────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────
def main():
    args = parse_args()

    # Проверяем что ffmpeg есть
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("[ОШИБКА] ffmpeg не найден. Установите: https://ffmpeg.org/download.html")
        sys.exit(1)

    # Читаем CSV
    print(f"[CSV] Читаю файл: {args.csv}")
    df = load_csv(args.csv)

    # Находим границы ЕМДР
    emdr_start_str, emdr_end_str = find_emdr_boundaries(df)

    # Спрашиваем время старта видео если не передано
    video_start_str = args.video_start
    if video_start_str is None:
        print(f"\n[ВОПРОС] Когда началась видеозапись?")
        print(f"         (CSV начинается с {df.iloc[0]['Время'].strip()}, запись должна быть раньше)")
        video_start_str = input("         Введите время старта видео (HH:MM:SS или HH:MM:SS.mmm): ").strip()

    # Вычисляем смещения
    offset_start, duration = compute_offsets(
        video_start_str,
        emdr_start_str,
        emdr_end_str,
        args.pad_before,
        args.pad_after,
    )

    # Определяем путь выходного файла
    if args.output:
        output_path = args.output
    else:
        video_p = Path(args.video)
        output_path = str(video_p.parent / f"{video_p.stem}_trimmed{video_p.suffix}")

    # Обрезаем
    trim_video(args.video, output_path, offset_start, duration)


if __name__ == "__main__":
    main()
