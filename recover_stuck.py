#!/usr/bin/env python3
"""
recover_stuck.py — одноразовый скрипт для восстановления 20 застрявших треков.

Проблема: sync_music.py v1 записывал трек в историю сразу после отправки
команды на скачивание (не дожидаясь результата). Если пир ушёл оффлайн,
трек висит в slskd как Queued/Errored, но в истории он помечен как "done".
sync_music.py его игнорирует навсегда.

Решение: найти такие треки, убрать из истории → при следующем запуске
sync_music.py найдёт их заново и попробует скачать у другого пира.

ВНИМАНИЕ: не трогает треки в статусе InProgress (активная загрузка).
"""

import requests
import json
import os
import re

from config import (
    SLSKD_URL,
    SLSKD_USERNAME,
    SLSKD_PASSWORD,
    HISTORY_FILE,
    MUSIC_ROOT,
)


def normalize(text):
    return re.sub(r'[^\w]', '', str(text).lower())


def is_on_disk(artist, title):
    """Проверяет наличие трека на диске (упрощённая версия)."""
    title_norm  = normalize(title)
    artist_norm = normalize(artist)
    for root, _, files in os.walk(MUSIC_ROOT):
        for f in files:
            if not f.lower().endswith((".flac", ".mp3", ".m4a", ".ogg")):
                continue
            fname_norm = normalize(os.path.splitext(f)[0])
            if title_norm and len(title_norm) >= 4 and title_norm in fname_norm:
                path_norm = normalize(os.path.join(root, f))
                if not artist_norm or artist_norm in path_norm:
                    return True
    return False


def main():
    print("=" * 60)
    print("recover_stuck.py")
    print("=" * 60)

    # 1. Авторизация slskd
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/session",
            json={"username": SLSKD_USERNAME, "password": SLSKD_PASSWORD},
            timeout=10
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        print("✅ slskd: авторизован")
    except Exception as e:
        print(f"❌ slskd недоступен: {e}")
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 2. Загрузка истории
    if not os.path.exists(HISTORY_FILE):
        print(f"❌ История не найдена: {HISTORY_FILE}")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        # v1 формат
        history = {
            "version":   2,
            "completed": set(raw),
            "pending":   {},
            "failed":    set(),
        }
        print(f"ℹ️  История v1 → загружено {len(history['completed'])} треков")
    else:
        history = {
            "version":   2,
            "completed": set(raw.get("completed", [])),
            "pending":   raw.get("pending", {}),
            "failed":    set(raw.get("failed", [])),
        }
        print(f"ℹ️  История v2: completed={len(history['completed'])}, "
              f"pending={len(history['pending'])}, failed={len(history['failed'])}")

    # 3. Получение всех трансферов из slskd
    try:
        resp = requests.get(
            f"{SLSKD_URL}/api/v0/transfers/downloads",
            headers=headers, timeout=15
        )
        resp.raise_for_status()
        all_transfers = []
        for ue in resp.json():
            for d in ue.get("directories", []):
                for f in d.get("files", []):
                    all_transfers.append({
                        "username": ue.get("username", ""),
                        "filename": f.get("filename", ""),
                        "id":       f.get("id", ""),
                        "state":    f.get("state", ""),
                    })
        print(f"ℹ️  slskd: {len(all_transfers)} трансферов всего")
    except Exception as e:
        print(f"❌ Ошибка получения трансферов: {e}")
        return

    # 4. Находим "потерянные" треки:
    #    В истории помечены как completed, но в slskd висят как Queued/Errored/Cancelled
    #    При этом файл отсутствует на диске
    STUCK_STATES = {"Queued", "Requested", "Errored", "TimedOut", "Cancelled", "Rejected"}

    stuck_transfers = [t for t in all_transfers
                   if any(s in (t.get("state") or "").lower()
                          for s in ("errored","rejected","queued","requested","cancelled","timedout","aborted"))
                   and not ("inprogress" in (t.get("state") or "").lower())]
    in_progress     = [t for t in all_transfers if t["state"] == "InProgress"]

    print(f"ℹ️  Застрявших: {len(stuck_transfers)}, активных: {len(in_progress)}")

    if in_progress:
        print("\n⚠️  Сейчас активно скачивается:")
        for t in in_progress:
            print(f"   [InProgress] {os.path.basename(t['filename'])}")

    # 5. Матчинг stuck transfers → history entries
    to_recover   = []   # (track_id, transfer)
    already_done = []   # на диске, просто не синхронизировались

    for t in stuck_transfers:
        fname_stem = normalize(os.path.splitext(os.path.basename(t["filename"]))[0])
        if not fname_stem or len(fname_stem) < 3:
            continue

        best_match = None
        best_score = 0

        for track_id in history["completed"]:
            parts = track_id.split(" - ", 1)
            if len(parts) != 2:
                continue
            _, title = parts
            title_norm = normalize(title)
            if len(title_norm) < 3:
                continue

            # Степень совпадения: сколько символов названия есть в имени файла
            if title_norm in fname_stem:
                score = len(title_norm)
                if score > best_score:
                    best_score = score
                    best_match = (track_id, t)

        if best_match and best_score >= 4:
            track_id, transfer = best_match
            parts = track_id.split(" - ", 1)
            artist = parts[0] if len(parts) == 2 else ""
            title  = parts[1] if len(parts) == 2 else track_id

            if is_on_disk(artist, title):
                already_done.append((track_id, transfer))
            else:
                to_recover.append((track_id, transfer))

    # 6. Показываем что нашли
    print()

    if already_done:
        print(f"✅ Уже на диске (просто не синхронизированы в истории): {len(already_done)}")
        # Эти треки действительно completed — ничего делать не надо

    if not to_recover:
        print("✅ Застрявших треков без файла не обнаружено")
        print("   (Если треки всё ещё ждут в очереди slskd — они продолжат скачиваться)")
        return

    print(f"⚠️  Найдено {len(to_recover)} треков, которые нужно восстановить:")
    print()
    for track_id, t in to_recover:
        print(f"   [{t['state']:10s}] {track_id}")
        print(f"               ↳ {os.path.basename(t['filename'])}")
    print()

    # 7. Подтверждение
    print("Эти треки будут:")
    print("  • Убраны из истории (completed)")
    print("  • Отменены в slskd (если Queued/Errored)")
    print("  • При следующем запуске sync_music.py — найдены заново")
    print()
    answer = input("Продолжить? (yes/no): ").strip().lower()
    if answer != "yes":
        print("Отменено.")
        return

    # 8. Применяем восстановление
    recovered = 0
    for track_id, t in to_recover:
        # Отмена трансфера в slskd
        if t["state"] in ("Queued", "Errored", "Cancelled", "TimedOut") and t["id"]:
            try:
                import urllib.parse
                encoded = urllib.parse.quote(t["username"])
                resp = requests.delete(
                    f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}/{t['id']}",
                    headers=headers, timeout=10
                )
                if resp.ok:
                    print(f"  ✅ Отменён трансфер: {os.path.basename(t['filename'])}")
                else:
                    print(f"  ⚠️  Не удалось отменить трансфер (HTTP {resp.status_code})")
            except Exception as e:
                print(f"  ⚠️  Ошибка отмены трансфера: {e}")

        # Убираем из истории
        history["completed"].discard(track_id)
        recovered += 1
        print(f"  ✅ Возвращён в очередь: {track_id}")

    # 9. Сохраняем историю
    backup_path = HISTORY_FILE + ".bak"
    with open(HISTORY_FILE, "r") as f:
        original = f.read()
    with open(backup_path, "w") as f:
        f.write(original)
    print(f"\nℹ️  Резервная копия: {backup_path}")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "version":   2,
            "completed": sorted(history["completed"]),
            "pending":   history["pending"],
            "failed":    sorted(history["failed"]),
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Восстановлено {recovered} треков.")
    print("   Запусти sync_music.py чтобы они снова попали в очередь скачивания.")


if __name__ == "__main__":
    main()
