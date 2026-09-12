#!/usr/bin/env python3
"""
delete_daemon.py
Позволяет удалять треки из Navidrome через Amperfy.

Как пользоваться:
  1. В Amperfy: открой трек → «Добавить в плейлист» → «🗑 Delete Queue»
  2. Демон раз в 5 минут проверяет этот плейлист
  3. Для каждого трека: удаляет физический файл, запускает ресканирование
  4. После следующего скана Navidrome трек исчезает из библиотеки навсегда

Важно:
  - Удалённый трек больше НЕ вернётся после ресканирования (файл удалён)
  - Starred треки по умолчанию защищены от удаления (см. PROTECT_STARRED)
  - Удалённые файлы перемещаются в TRASH_DIR на 7 дней, затем удаляются насовсем
"""

import requests
import hashlib
import random
import string
import json
import os
import shutil
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
from config import (
    NAVIDROME_URL,
    NAVIDROME_USER,
    NAVIDROME_PASS,
    MUSIC_ROOT_HOST,
    MUSIC_ROOT_CONTAINER,
    TRASH_DIR,
    TRASH_DAYS,
    DELETE_PLAYLIST,
    PROTECT_STARRED,
    POLL_INTERVAL,
    DELETE_DAEMON_LOG_FILE as LOG_FILE,
)

# ─── ЛОГИРОВАНИЕ ─────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── NAVIDROME API ────────────────────────────────────────────────────────────
class NavidromeClient:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.user = user
        self._password = password

    def _params(self, **extra):
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        token = hashlib.md5((self._password + salt).encode()).hexdigest()
        p = {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "delete_daemon",
            "f": "json",
        }
        p.update(extra)
        return p

    def _get(self, endpoint, **kwargs):
        try:
            resp = requests.get(
                f"{self.url}/rest/{endpoint}", params=self._params(**kwargs), timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("subsonic-response", {})
            return data if data.get("status") == "ok" else None
        except Exception as e:
            log.error(f"API error ({endpoint}): {e}")
            return None

    def _post(self, endpoint, **kwargs):
        try:
            resp = requests.post(
                f"{self.url}/rest/{endpoint}", data=self._params(**kwargs), timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("subsonic-response", {})
            return data if data.get("status") == "ok" else None
        except Exception as e:
            log.error(f"API error ({endpoint}): {e}")
            return None

    def get_all_playlists(self):
        data = self._get("getPlaylists")
        return (data or {}).get("playlists", {}).get("playlist", [])

    def get_playlist_songs(self, playlist_id):
        data = self._get("getPlaylist", id=playlist_id)
        return (data or {}).get("playlist", {}).get("entry", [])

    def get_song(self, song_id):
        """Полные метаданные трека включая path."""
        data = self._get("getSong", id=song_id)
        return (data or {}).get("song")

    def get_starred_ids(self):
        data = self._get("getStarred2")
        songs = (data or {}).get("starred2", {}).get("song", [])
        return {s["id"] for s in songs}

    def find_playlist_id(self, name):
        for pl in self.get_all_playlists():
            if pl.get("name") == name:
                return pl["id"]
        return None

    def create_playlist(self, name):
        data = self._get("createPlaylist", name=name)
        return (data or {}).get("playlist", {}).get("id")

    def get_or_create_playlist(self, name):
        pid = self.find_playlist_id(name)
        return pid if pid else self.create_playlist(name)

    def remove_from_playlist(self, playlist_id, song_indices):
        """Удаляет треки из плейлиста по индексам (начиная с конца, чтобы не сбить индексы)."""
        for idx in sorted(song_indices, reverse=True):
            if not self._post("updatePlaylist", playlistId=playlist_id, songIndexToRemove=idx):
                log.warning(f"Ошибка удаления индекса {idx} из плейлиста")
                return False
        return True

    def start_scan(self):
        if self._get("startScan") is None:
            return False
        log.info("[Scan] Запущено ресканирование библиотеки Navidrome")
        return True

    def wait_for_scan(self, timeout=120):
        """Ждёт завершения сканирования."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._get("getScanStatus")
            if data and not data.get("scanStatus", {}).get("scanning", True):
                log.info("[Scan] Завершено")
                return
            time.sleep(5)
        log.warning("[Scan] Таймаут ожидания сканирования")


# ─── ОПЕРАЦИИ С ФАЙЛАМИ ───────────────────────────────────────────────────────
def container_path_to_host(container_path):
    """
    Конвертирует путь из ответа Navidrome (внутри контейнера) в путь на хосте.
    /music/Artist/Album/track.flac → /home/cif/homelab/data/music/Artist/Album/track.flac
    """
    if not container_path:
        return None

    root = Path(MUSIC_ROOT_HOST).absolute()
    candidate = Path(str(container_path))
    container_root = Path(MUSIC_ROOT_CONTAINER).absolute()

    if candidate.is_absolute():
        if str(candidate) == str(container_root) or str(candidate).startswith(f"{container_root}{os.sep}"):
            candidate = root / str(candidate.relative_to(container_root))
        elif str(candidate) == str(root) or str(candidate).startswith(f"{root}{os.sep}"):
            candidate = candidate
        else:
            return None
    else:
        candidate = root / candidate

    candidate = candidate.resolve(strict=False)
    root = root.resolve(strict=False)
    try:
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            return None
    except ValueError:
        return None
    return str(candidate)


def move_to_trash(file_path):
    """
    Перемещает файл в TRASH_DIR с временной меткой.
    Возвращает True при успехе.
    """
    if not file_path or container_path_to_host(file_path) != str(Path(file_path).resolve(strict=False)):
        log.error(f"Отказано: путь вне MUSIC_ROOT_HOST: {file_path}")
        return False

    if not os.path.exists(file_path):
        log.warning(f"Файл уже не существует: {file_path}")
        return True

    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        basename = os.path.basename(file_path)
        trash_name = f"{ts}_{basename}"
        trash_path = os.path.join(TRASH_DIR, trash_name)

        shutil.move(file_path, trash_path)
        log.info(f"  Перемещён в корзину: {trash_name}")

        # Удалить пустые родительские папки (альбом/артист)
        parent = os.path.dirname(file_path)
        for _ in range(3):  # max 3 уровня вверх
            if os.path.abspath(parent) == os.path.abspath(MUSIC_ROOT_HOST):
                break
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    log.info(f"  Удалена пустая папка: {parent}")
                    parent = os.path.dirname(parent)
                else:
                    break
            except Exception:
                break

        return True
    except Exception as e:
        log.error(f"  Ошибка перемещения в корзину: {e}")
        return False


def purge_old_trash():
    """Удаляет файлы из корзины старше TRASH_DAYS дней."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRASH_DAYS)
    for filename in os.listdir(TRASH_DIR):
        file_path = os.path.join(TRASH_DIR, filename)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
            if mtime < cutoff:
                os.remove(file_path)
                log.info(f"[Trash] Окончательно удалён: {filename}")
        except Exception as e:
            log.warning(f"[Trash] Ошибка: {e}")


# ─── ОСНОВНАЯ ЛОГИКА ─────────────────────────────────────────────────────────
def process_delete_queue(navidrome: NavidromeClient):
    """
    Проверяет плейлист «Delete Queue», удаляет файлы, обновляет библиотеку.
    """
    playlist_id = navidrome.get_or_create_playlist(DELETE_PLAYLIST)
    if not playlist_id:
        log.error("Не удалось создать/найти плейлист Delete Queue")
        return

    songs = navidrome.get_playlist_songs(playlist_id)
    if not songs:
        return

    log.info(f"[Delete] Найдено {len(songs)} треков в Delete Queue")

    starred_ids = navidrome.get_starred_ids() if PROTECT_STARRED else set()
    deleted_count = 0
    skipped_count = 0
    indices_to_remove = []

    for idx, song in enumerate(songs):
        song_id = song.get("id")
        song_title = f"{song.get('artist', '?')} — {song.get('title', '?')}"

        # Защита: не удалять звёздочки
        if PROTECT_STARRED and song_id in starred_ids:
            log.info(f"  [Пропущен/Starred] {song_title}")
            indices_to_remove.append(idx)  # убираем из плейлиста но не удаляем файл
            skipped_count += 1
            continue

        # Получаем полный путь файла
        song_info = navidrome.get_song(song_id)
        if not song_info:
            log.warning(f"  Не удалось получить метаданные: {song_title}")
            continue

        container_path = song_info.get("path", "")
        if not container_path:
            log.warning(f"  Путь не найден в метаданных: {song_title}")
            continue

        host_path = container_path_to_host(container_path)
        if not host_path:
            log.error(f"  Отказано: путь вне музыкального корня: {container_path}")
            continue
        log.info(f"  Удаляем: {song_title}")
        log.info(f"    Путь: {host_path}")

        if move_to_trash(host_path):
            deleted_count += 1
            indices_to_remove.append(idx)
            log.info(f"  [✓] Удалён: {song_title}")
        else:
            log.error(f"  [✗] Не удалось удалить: {song_title}")

    # Убираем обработанные треки из плейлиста
    if indices_to_remove and not navidrome.remove_from_playlist(playlist_id, indices_to_remove):
        log.error("[Delete] Не удалось обновить Delete Queue; оставляем его для следующего запуска")
        return

    if deleted_count > 0:
        # Запускаем ресканирование чтобы треки исчезли из Navidrome
        if not navidrome.start_scan():
            log.error("[Delete] Не удалось запустить сканирование после удаления")
            return
        navidrome.wait_for_scan()
        log.info(
            f"[Delete] Итог: удалено={deleted_count}, пропущено(starred)={skipped_count}"
        )


def main():
    log.info("=" * 60)
    log.info("delete_daemon.py запущен")
    log.info("=" * 60)

    if not NAVIDROME_PASS:
        log.error("Заполни NAVIDROME_PASS в .env!")
        return

    navidrome = NavidromeClient(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS)

    # Создаём плейлист Delete Queue если не существует
    navidrome.get_or_create_playlist(DELETE_PLAYLIST)
    log.info(
        f"Плейлист «{DELETE_PLAYLIST}» готов — добавляй треки через Amperfy для удаления"
    )
    log.info(
        f"Интервал проверки: {POLL_INTERVAL}с | Корзина: {TRASH_DIR} ({TRASH_DAYS} дней)"
    )

    while True:
        try:
            process_delete_queue(navidrome)
            purge_old_trash()
        except Exception as e:
            log.error(f"Критическая ошибка: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
