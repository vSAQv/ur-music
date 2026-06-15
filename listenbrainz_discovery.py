#!/usr/bin/env python3
"""
listenbrainz_discovery.py
Анализирует историю прослушивания через Navidrome/ListenBrainz,
формирует персональные плейлисты рекомендаций в Navidrome,
автоматически ротирует их по расписанию.

Плейлисты:
  🔮 Discover Weekly   — обновляется каждый понедельник (20 треков)
  🌙 Discover Monthly  — обновляется 1-го числа каждого месяца (40 треков)

Логика ротации:
  - Перед очисткой проверяет, какие треки из плейлиста пользователь заcтарил
    или добавил в другие плейлисты — эти треки остаются в библиотеке нетронутыми.
  - Сам плейлист Discovery очищается и наполняется новыми рекомендациями.
  - Файлы НЕ удаляются никогда.
"""

import requests
import hashlib
import random
import string
import json
import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
from config import (
    NAVIDROME_URL,
    NAVIDROME_USER,
    NAVIDROME_PASS,
    LISTENBRAINZ_TOKEN,
    LISTENBRAINZ_USER,
    SUBMIT_LISTENS_COUNT,
    WEEKLY_COUNT,
    MONTHLY_COUNT,
    WEEKLY_NAME,
    MONTHLY_NAME,
    STATE_FILE,
    DISCOVERY_LOG_FILE as LOG_FILE,
    SYNC_QUEUE_FILE,
)

# ─── ЛОГИРОВАНИЕ ─────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

LB_API = "https://api.listenbrainz.org"
MB_API = "https://musicbrainz.org/ws/2"


# ─── ВСПОМОГАТЕЛЬНОЕ ─────────────────────────────────────────────────────────
def _lb_headers():
    return {"Authorization": f"Token {LISTENBRAINZ_TOKEN}"}


def _mb_headers():
    return {"User-Agent": "NavidromeDiscovery/1.0 (homelab)"}


# ─── NAVIDROME SUBSONIC API ───────────────────────────────────────────────────
class NavidromeClient:
    """Тонкая обёртка над Subsonic API Navidrome."""

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
            "c": "navidrome_discovery",
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
            if data.get("status") != "ok":
                log.warning(f"Subsonic error: {data.get('error', {})}")
                return None
            return data
        except Exception as e:
            log.error(f"Navidrome API error ({endpoint}): {e}")
            return None

    def _post(self, endpoint, **kwargs):
        try:
            resp = requests.post(
                f"{self.url}/rest/{endpoint}", data=self._params(**kwargs), timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("subsonic-response", {})
            if data.get("status") != "ok":
                log.warning(f"Subsonic error: {data.get('error', {})}")
                return None
            return data
        except Exception as e:
            log.error(f"Navidrome API error ({endpoint}): {e}")
            return None

    # ── Чтение ────────────────────────────────────────────────────────────────
    def get_recent_plays(self, count=500):
        """Возвращает недавно воспроизведённые треки."""
        result = []
        offset = 0
        while len(result) < count:
            data = self._get(
                "getAlbumList2",
                type="recent",
                size=min(500, count - len(result)),
                offset=offset,
            )
            if not data:
                break
            albums = data.get("albumList2", {}).get("album", [])
            if not albums:
                break
            for album in albums:
                songs_data = self._get("getAlbumSongs", id=album["id"])
                if songs_data:
                    for song in songs_data.get("songs", {}).get("song", []):
                        result.append(
                            {
                                "id": song.get("id"),
                                "artist": song.get("artist", ""),
                                "title": song.get("title", ""),
                                "album": song.get("album", ""),
                                "played_at": int(time.time()),
                            }
                        )
            offset += len(albums)
            if len(albums) < 500:
                break
        return result[:count]

    def get_starred_ids(self):
        """ID треков, которые пользователь пометил звёздочкой."""
        data = self._get("getStarred2")
        if not data:
            return set()
        songs = data.get("starred2", {}).get("song", [])
        return {s["id"] for s in songs}

    def get_all_playlists(self):
        """Список всех плейлистов."""
        data = self._get("getPlaylists")
        if not data:
            return []
        return data.get("playlists", {}).get("playlist", [])

    def get_playlist_songs(self, playlist_id):
        """Треки в конкретном плейлисте."""
        data = self._get("getPlaylist", id=playlist_id)
        if not data:
            return []
        return data.get("playlist", {}).get("entry", [])

    def get_songs_in_all_user_playlists(self):
        """IDs всех треков во всех пользовательских плейлистах (кроме Discovery)."""
        all_ids = set()
        for pl in self.get_all_playlists():
            if pl.get("name") in (WEEKLY_NAME, MONTHLY_NAME):
                continue
            for song in self.get_playlist_songs(pl["id"]):
                all_ids.add(song.get("id"))
        return all_ids

    def search_track(self, artist, title):
        """Ищет трек в библиотеке Navidrome. Возвращает ID первого совпадения."""
        query = f"{artist} {title}"
        data = self._get(
            "search3", query=query, songCount=5, albumCount=0, artistCount=0
        )
        if not data:
            return None
        songs = data.get("searchResult3", {}).get("song", [])
        if not songs:
            return None
        # Ищем наиболее точное совпадение
        q_title = title.lower()
        q_artist = artist.lower()
        for song in songs:
            if (
                q_title in song.get("title", "").lower()
                and q_artist in song.get("artist", "").lower()
            ):
                return song["id"]
        return songs[0]["id"] if songs else None

    # ── Плейлисты ─────────────────────────────────────────────────────────────
    def get_or_create_playlist(self, name):
        """Находит плейлист по имени или создаёт новый. Возвращает ID."""
        for pl in self.get_all_playlists():
            if pl.get("name") == name:
                return pl["id"]
        data = self._get("createPlaylist", name=name)
        if data:
            return data.get("playlist", {}).get("id")
        return None

    def clear_playlist(self, playlist_id):
        """Удаляет все треки из плейлиста."""
        songs = self.get_playlist_songs(playlist_id)
        if not songs:
            return
        # Subsonic: songIndexToRemove (можно несколько параметров с одинаковым именем)
        indices = list(range(len(songs)))
        # Батчами по 50
        for i in range(0, len(indices), 50):
            batch = indices[i : i + 50]
            params = self._params(playlistId=playlist_id)
            for idx in batch:
                params.setdefault("songIndexToRemove", [])
                if isinstance(params["songIndexToRemove"], list):
                    params["songIndexToRemove"].append(idx)
            try:
                requests.post(
                    f"{self.url}/rest/updatePlaylist", params=params, timeout=15
                )
            except Exception as e:
                log.warning(f"Ошибка очистки плейлиста: {e}")

    def add_songs_to_playlist(self, playlist_id, song_ids):
        """Добавляет треки в плейлист батчами."""
        for i in range(0, len(song_ids), 50):
            batch = song_ids[i : i + 50]
            params = self._params(playlistId=playlist_id)
            for sid in batch:
                params.setdefault("songIdToAdd", [])
                if isinstance(params["songIdToAdd"], list):
                    params["songIdToAdd"].append(sid)
            try:
                requests.post(
                    f"{self.url}/rest/updatePlaylist", params=params, timeout=15
                )
            except Exception as e:
                log.warning(f"Ошибка добавления в плейлист: {e}")

    def start_scan(self):
        """Запускает пересканирование библиотеки Navidrome."""
        self._get("startScan")


# ─── LISTENBRAINZ ─────────────────────────────────────────────────────────────
def submit_listens_to_lb(navidrome: NavidromeClient):
    """
    Отправляет историю прослушиваний Navidrome в ListenBrainz.
    Нужно для того, чтобы LB накопил данные и мог строить персональные рекомендации.
    """
    log.info("[LB] Синхронизация истории прослушиваний с ListenBrainz...")
    recent = navidrome.get_recent_plays(SUBMIT_LISTENS_COUNT)
    if not recent:
        log.info("[LB] Нет треков для синхронизации")
        return

    payload = []
    for track in recent:
        payload.append(
            {
                "listened_at": track["played_at"],
                "track_metadata": {
                    "artist_name": track["artist"],
                    "track_name": track["title"],
                    "release_name": track["album"],
                    "additional_info": {
                        "media_player": "navidrome",
                        "submission_client": "navidrome_discovery",
                    },
                },
            }
        )

    # LB принимает батчами по 1000
    for i in range(0, len(payload), 1000):
        batch = payload[i : i + 1000]
        try:
            resp = requests.post(
                f"{LB_API}/1/submit-listens",
                headers={**_lb_headers(), "Content-Type": "application/json"},
                json={"listen_type": "import", "payload": batch},
                timeout=30,
            )
            if resp.ok:
                log.info(f"[LB] Отправлено {len(batch)} прослушиваний")
            else:
                log.warning(
                    f"[LB] Ошибка отправки: {resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            log.error(f"[LB] Ошибка: {e}")


def fetch_lb_recommendation_playlists():
    """
    Получает авторские плейлисты-рекомендации от ListenBrainz:
    - Exploration playlist (weekly)
    - Top Discoveries (monthly)

    Возвращает {"weekly": [...tracks], "monthly": [...tracks]}
    где каждый трек: {"artist": str, "title": str}
    """
    log.info("[LB] Загружаем рекомендательные плейлисты...")
    result = {"weekly": [], "monthly": []}

    try:
        resp = requests.get(
            f"{LB_API}/1/user/{LISTENBRAINZ_USER}/playlists/recommendations",
            headers=_lb_headers(),
            timeout=30,
        )
        if not resp.ok:
            log.warning(f"[LB] Ошибка загрузки плейлистов: {resp.status_code}")
            return result

        playlists = resp.json().get("playlists", [])
        log.info(f"[LB] Найдено плейлистов: {len(playlists)}")

        for pl_wrapper in playlists:
            pl = pl_wrapper.get("playlist", {})
            title = pl.get("title", "").lower()
            tracks = []

            for track in pl.get("track", []):
                artist = track.get("creator", "")
                name = track.get("title", "")
                # Иногда creator пустой — ищем в extension
                if not artist:
                    ext = track.get("extension", {})
                    for v in ext.values():
                        if isinstance(v, dict) and v.get("artist_credit_name"):
                            artist = v["artist_credit_name"]
                            break
                if artist and name:
                    tracks.append({"artist": artist, "title": name})

            if "exploration" in title or "weekly" in title:
                result["weekly"] = tracks
                log.info(f"[LB] Weekly: {len(tracks)} треков")
            elif "discoveries" in title or "monthly" in title or "top" in title:
                result["monthly"] = tracks
                log.info(f"[LB] Monthly: {len(tracks)} треков")

    except Exception as e:
        log.error(f"[LB] Ошибка: {e}")

    # Фоллбэк: CF-рекомендации если авторских плейлистов нет
    if not result["weekly"] and not result["monthly"]:
        log.info("[LB] Авторских плейлистов нет — загружаем CF рекомендации...")
        result["weekly"] = _fetch_cf_recommendations(WEEKLY_COUNT * 2)
        result["monthly"] = _fetch_cf_recommendations(MONTHLY_COUNT * 2)

    return result


def _fetch_cf_recommendations(count=50):
    """
    Collaborative Filtering рекомендации из LB.
    Используется как фоллбэк если авторских плейлистов ещё нет.
    """
    tracks = []
    try:
        resp = requests.get(
            f"{LB_API}/1/cf/recommendation/user/{LISTENBRAINZ_USER}/recording",
            headers=_lb_headers(),
            params={"count": count, "offset": 0},
            timeout=30,
        )
        if not resp.ok:
            return tracks

        for item in resp.json().get("payload", {}).get("mbids", []):
            mbid = item.get("recording_mbid")
            if mbid:
                info = _lookup_mbid(mbid)
                if info:
                    tracks.append(info)
                time.sleep(0.1)  # уважаем MB rate limit
    except Exception as e:
        log.warning(f"[LB CF] Ошибка: {e}")
    return tracks


def _lookup_mbid(recording_mbid):
    """Получает artist+title для MusicBrainz recording ID."""
    try:
        resp = requests.get(
            f"{MB_API}/recording/{recording_mbid}",
            headers=_mb_headers(),
            params={"fmt": "json", "inc": "artists"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            title = data.get("title", "")
            artist = ""
            for credit in data.get("artist-credit", []):
                if isinstance(credit, dict) and credit.get("artist"):
                    artist = credit["artist"].get("name", "")
                    break
            if artist and title:
                return {"artist": artist, "title": title}
    except Exception:
        pass
    return None


# ─── ОЧЕРЕДЬ СКАЧИВАНИЯ ───────────────────────────────────────────────────────
def queue_for_download(tracks_to_download):
    """
    Добавляет треки, которых нет в библиотеке, в файл очереди sync_music.py.
    sync_music.py подхватит их при следующем запуске.
    """
    if not tracks_to_download:
        return

    os.makedirs(os.path.dirname(SYNC_QUEUE_FILE), exist_ok=True)

    existing = []
    if os.path.exists(SYNC_QUEUE_FILE):
        try:
            with open(SYNC_QUEUE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    existing_keys = {f"{t['artist'].lower()} - {t['title'].lower()}" for t in existing}

    added = 0
    for t in tracks_to_download:
        key = f"{t['artist'].lower()} - {t['title'].lower()}"
        if key not in existing_keys:
            existing.append(t)
            existing_keys.add(key)
            added += 1

    with open(SYNC_QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    log.info(f"[Queue] Добавлено {added} треков для скачивания")


# ─── СОСТОЯНИЕ РОТАЦИИ ────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_weekly": None, "last_monthly": None}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_weekly": None, "last_monthly": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_rotate_weekly(state):
    """Ротация еженедельного плейлиста — каждый понедельник."""
    last = state.get("last_weekly")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    now = datetime.now(timezone.utc)
    # Ротируем если прошла неделя И сегодня понедельник (или более 7 дней)
    return (now - last_dt) >= timedelta(days=7)


def should_rotate_monthly(state):
    """Ротация ежемесячного плейлиста — 1-е число каждого месяца."""
    last = state.get("last_monthly")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    now = datetime.now(timezone.utc)
    return (now - last_dt) >= timedelta(days=30)


# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────
def update_discovery_playlist(
    navidrome: NavidromeClient, playlist_name, recommended_tracks, count
):
    """
    Обновляет один Discovery плейлист:
    1. Находит треки из рекомендаций в библиотеке Navidrome
    2. Треки, которых нет, — добавляет в очередь скачивания
    3. Очищает плейлист (не трогая файлы и starred)
    4. Заполняет новыми треками
    """
    playlist_id = navidrome.get_or_create_playlist(playlist_name)
    if not playlist_id:
        log.error(f"Не удалось создать плейлист: {playlist_name}")
        return

    log.info(f"\n[Playlist] Обновление: {playlist_name}")

    # Какие треки из текущего плейлиста пользователь сохранил
    starred_ids = navidrome.get_starred_ids()
    user_playlist_ids = navidrome.get_songs_in_all_user_playlists()
    protected_ids = starred_ids | user_playlist_ids

    current_songs = navidrome.get_playlist_songs(playlist_id)
    log.info(
        f"  Текущих треков: {len(current_songs)}, защищённых пользователем: "
        f"{len([s for s in current_songs if s.get('id') in protected_ids])}"
    )

    # Ищем рекомендованные треки в библиотеке
    found_ids = []
    to_download = []

    for track in recommended_tracks:
        if len(found_ids) >= count:
            break
        song_id = navidrome.search_track(track["artist"], track["title"])
        if song_id:
            found_ids.append(song_id)
            log.info(f"  [✓] В библиотеке: {track['artist']} — {track['title']}")
        else:
            to_download.append(track)
            log.info(f"  [↓] Нет в библиотеке: {track['artist']} — {track['title']}")

    # Добавляем отсутствующие треки в очередь скачивания
    queue_for_download(to_download)

    if not found_ids:
        log.info(f"  Нет треков для добавления в плейлист (ещё скачиваются)")
        return

    # Очищаем плейлист
    navidrome.clear_playlist(playlist_id)
    log.info(f"  Плейлист очищен")

    # Заполняем новыми треками
    navidrome.add_songs_to_playlist(playlist_id, found_ids)
    log.info(f"  [✓] Добавлено {len(found_ids)} треков в {playlist_name}")


def main():
    log.info("=" * 60)
    log.info("listenbrainz_discovery.py запущен")
    log.info(f"Время: {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 60)

    if not NAVIDROME_PASS or not LISTENBRAINZ_TOKEN or not LISTENBRAINZ_USER:
        log.error(
            "Заполни NAVIDROME_PASS, LISTENBRAINZ_TOKEN, LISTENBRAINZ_USER в .env!"
        )
        return

    navidrome = NavidromeClient(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS)
    state = load_state()

    # Синхронизируем историю прослушиваний с LB (для обучения модели)
    submit_listens_to_lb(navidrome)

    need_weekly = should_rotate_weekly(state)
    need_monthly = should_rotate_monthly(state)

    if not need_weekly and not need_monthly:
        log.info("[*] Ротация не требуется")
        return

    # Загружаем рекомендации из ListenBrainz
    recommendations = fetch_lb_recommendation_playlists()

    if need_weekly:
        tracks = recommendations.get("weekly", [])
        if tracks:
            update_discovery_playlist(navidrome, WEEKLY_NAME, tracks, WEEKLY_COUNT)
            state["last_weekly"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        else:
            log.warning(
                "[Weekly] LB не вернул рекомендаций (недостаточно истории прослушиваний)"
            )

    if need_monthly:
        tracks = recommendations.get("monthly", [])
        if tracks:
            update_discovery_playlist(navidrome, MONTHLY_NAME, tracks, MONTHLY_COUNT)
            state["last_monthly"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        else:
            log.warning("[Monthly] LB не вернул рекомендаций")

    log.info("[*] Discovery обновление завершено.")


if __name__ == "__main__":
    main()
