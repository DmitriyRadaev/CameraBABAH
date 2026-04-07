"""
list_devices.py — Показывает все доступные устройства захвата видео/аудио.
Запусти этот скрипт ПЕРВЫМ, чтобы узнать имя карты захвата для camera_recorder.py

Требует: ffmpeg установлен и доступен в PATH
"""
import subprocess
import re

print("Ищу устройства захвата...\n")

result = subprocess.run(
    ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
    capture_output=True, text=True, encoding="utf-8", errors="replace"
)

output = result.stderr

video_devices = re.findall(r'"([^"]+)"\s*\(video\)', output)
audio_devices = re.findall(r'"([^"]+)"\s*\(audio\)', output)

print("=" * 50)
print("ВИДЕОУСТРОЙСТВА:")
if video_devices:
    for i, d in enumerate(video_devices):
        print(f"  [{i}] {d}")
else:
    print("  Не найдено. Проверь что карта захвата подключена.")

print("\nАУДИОУСТРОЙСТВА:")
if audio_devices:
    for i, d in enumerate(audio_devices):
        print(f"  [{i}] {d}")
else:
    print("  Не найдено.")

print("=" * 50)
print("\nСкопируй нужное имя в camera_recorder.py")
input("\nНажми Enter для выхода...")
