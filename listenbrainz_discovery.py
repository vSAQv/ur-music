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
import urllib.parse
from collections import Counter
from datetime import datetime, timezone, timedelta

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
from config import (
    NAVIDROME_URL,
    NAVIDROME_USER,
    NAVIDROME_PASS,
    LISTENBRAINZ_TOKEN,
    LISTENBRAINZ_USER,
    LISTEN_HISTORY_COUNT,
    DISCOVERY_SEED_ARTISTS,
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
        """Ищет только точное совпадение трека в библиотеке Navidrome."""
        query = f"{artist} {title}"
        data = self._get(
            "search3", query=query, songCount=5, albumCount=0, artistCount=0
        )
        if not data:
            return None
        songs = data.get("searchResult3", {}).get("song", [])
        if not songs:
            return None
        def normalize(value):
            return set(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))

        q_title = normalize(title)
        q_artist = normalize(artist)
        for song in songs:
            if normalize(song.get("title", "")) == q_title and normalize(song.get("artist", "")) == q_artist:
                return song["id"]
        return None

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
            return True
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
                response = requests.post(
                    f"{self.url}/rest/updatePlaylist", params=params, timeout=15
                )
                response.raise_for_status()
                data = response.json().get("subsonic-response", {})
                if data.get("status") != "ok":
                    log.warning(f"Ошибка очистки плейлиста: {data.get('error', {})}")
                    return False
            except Exception as e:
                log.warning(f"Ошибка очистки плейлиста: {e}")
                return False
        return True

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
                response = requests.post(
                    f"{self.url}/rest/updatePlaylist", params=params, timeout=15
                )
                response.raise_for_status()
                data = response.json().get("subsonic-response", {})
                if data.get("status") != "ok":
                    log.warning(f"Ошибка добавления в плейлист: {data.get('error', {})}")
                    return False
            except Exception as e:
                log.warning(f"Ошибка добавления в плейлист: {e}")
                return False
        return True

    def start_scan(self):
        """Запускает пересканирование библиотеки Navidrome."""
        self._get("startScan")


# ─── LISTENBRAINZ ─────────────────────────────────────────────────────────────
def _fetch_listens_since(days):
    """Fetch real ListenBrainz listens from the requested time window."""
    min_ts = int(time.time()) - days * 24 * 60 * 60
    try:
        response = requests.get(
            f"{LB_API}/1/user/{LISTENBRAINZ_USER}/listens",
            headers=_lb_headers(),
            params={"min_ts": min_ts, "count": LISTEN_HISTORY_COUNT},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json().get("payload", {})
        listens = payload.get("listens", [])
        return listens if isinstance(listens, list) else []
    except (requests.RequestException, TypeError, ValueError) as error:
        log.warning(f"[LB] Не удалось получить историю за {days} дней: {error}")
        return []


def _listen_seed(listen):
    """Extract an artist seed and recording identity from one ListenBrainz listen."""
    metadata = listen.get("track_metadata", {}) if isinstance(listen, dict) else {}
    additional = metadata.get("additional_info", {}) or {}
    mapping = metadata.get("mbid_mapping", {}) or {}
    artist_mbids = mapping.get("artist_mbids") or additional.get("artist_mbids") or []
    recording_mbid = mapping.get("recording_mbid") or additional.get("recording_mbid")
    artist_mbid = artist_mbids[0] if artist_mbids else None
    return {
        "artist": metadata.get("artist_name", ""),
        "title": metadata.get("track_name", ""),
        "artist_mbid": artist_mbid,
        "recording_mbid": recording_mbid,
    }


def _fetch_radio_recordings(artist_mbid):
    """Get ListenBrainz radio recordings for one artist seed."""
    try:
        response = requests.get(
            f"{LB_API}/1/lb-radio/artist/{urllib.parse.quote(artist_mbid, safe='')}",
            headers=_lb_headers(),
            params={
                "mode": "medium",
                "max_similar_artists": 10,
                "max_recordings_per_artist": 10,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, TypeError, ValueError) as error:
        log.warning(f"[LB] Radio error for artist {artist_mbid}: {error}")
        return []

    recordings = []
    values = payload.values() if isinstance(payload, dict) else []
    for group in values:
        if not isinstance(group, list):
            continue
        recordings.extend(item for item in group if isinstance(item, dict))
    return recordings


def _fetch_period_recommendations(days, count):
    """Build recommendations from artists listened to during a time window."""
    listens = _fetch_listens_since(days)
    artist_counts = Counter()
    listened_recordings = set()
    for listen in listens:
        seed = _listen_seed(listen)
        if seed["artist_mbid"]:
            artist_counts[seed["artist_mbid"]] += 1
        if seed["recording_mbid"]:
            listened_recordings.add(seed["recording_mbid"])

    recommendations = []
    seen_recordings = set()
    for artist_mbid, _ in artist_counts.most_common(DISCOVERY_SEED_ARTISTS):
        for recording in _fetch_radio_recordings(artist_mbid):
            recording_mbid = recording.get("recording_mbid")
            if not recording_mbid or recording_mbid in listened_recordings or recording_mbid in seen_recordings:
                continue
            info = _lookup_mbid(recording_mbid)
            time.sleep(1)
            if info:
                recommendations.append(info)
                seen_recordings.add(recording_mbid)
            if len(recommendations) >= count:
                return recommendations
    return recommendations


def _fetch_curated_recommendation_playlists():
    """Read ListenBrainz's published recommendation playlists as a fallback."""
    result = {"weekly": [], "monthly": []}
    try:
        response = requests.get(
            f"{LB_API}/1/user/{LISTENBRAINZ_USER}/playlists/recommendations",
            headers=_lb_headers(),
            timeout=30,
        )
        response.raise_for_status()
        playlists = response.json().get("playlists", [])
        for wrapper in playlists:
            playlist = wrapper.get("playlist", {})
            title = playlist.get("title", "").lower()
            tracks = []
            for track in playlist.get("track", []):
                artist = track.get("creator", "")
                name = track.get("title", "")
                if not artist:
                    for value in (track.get("extension", {}) or {}).values():
                        if isinstance(value, dict) and value.get("artist_credit_name"):
                            artist = value["artist_credit_name"]
                            break
                if artist and name:
                    tracks.append({"artist": artist, "title": name})
            if "exploration" in title or "weekly" in title:
                result["weekly"] = tracks
            elif "discoveries" in title or "monthly" in title or "top" in title:
                result["monthly"] = tracks
    except (requests.RequestException, TypeError, ValueError) as error:
        log.warning(f"[LB] Ошибка загрузки авторских плейлистов: {error}")
    return result


def fetch_lb_recommendation_playlists():
    """Return separate recommendation sets based on seven and thirty days of history."""
    result = {
        "weekly": _fetch_period_recommendations(7, WEEKLY_COUNT * 2),
        "monthly": _fetch_period_recommendations(30, MONTHLY_COUNT * 2),
    }
    curated = _fetch_curated_recommendation_playlists()
    if not result["weekly"]:
        result["weekly"] = curated["weekly"]
    if not result["monthly"]:
        result["monthly"] = curated["monthly"]
    if not result["weekly"]:
        result["weekly"] = _fetch_cf_recommendations(WEEKLY_COUNT * 2)
    if not result["monthly"]:
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
                time.sleep(1)  # MusicBrainz requests are rate-limited.
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
                raw = json.load(f)
                if isinstance(raw, list):
                    existing = [
                        item
                        for item in raw
                        if isinstance(item, dict) and item.get("artist") and item.get("title")
                    ]
                else:
                    log.error("[Queue] Existing queue is not a JSON list; preserving it")
                    return
        except (OSError, ValueError, TypeError) as error:
            log.error("[Queue] Cannot read existing queue: %s", error)
            return

    existing_keys = {f"{t['artist'].lower()} - {t['title'].lower()}" for t in existing}

    added = 0
    for t in tracks_to_download:
        key = f"{t['artist'].lower()} - {t['title'].lower()}"
        if key not in existing_keys:
            existing.append(t)
            existing_keys.add(key)
            added += 1

    temporary = f"{SYNC_QUEUE_FILE}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, SYNC_QUEUE_FILE)
    except OSError as error:
        log.error("[Queue] Cannot persist queue: %s", error)
        try:
            os.unlink(temporary)
        except OSError:
            pass

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
        return False

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
        if song_id and song_id not in found_ids:
            found_ids.append(song_id)
            log.info(f"  [✓] В библиотеке: {track['artist']} — {track['title']}")
        else:
            to_download.append(track)
            log.info(f"  [↓] Нет в библиотеке: {track['artist']} — {track['title']}")

    # Добавляем отсутствующие треки в очередь скачивания
    queue_for_download(to_download)

    protected_current_ids = [
        song.get("id")
        for song in current_songs
        if song.get("id") in protected_ids and song.get("id") not in found_ids
    ]
    playlist_ids = protected_current_ids + found_ids

    if not playlist_ids:
        log.info(f"  Нет треков для добавления в плейлист (ещё скачиваются)")
        return False

    # Очищаем плейлист
    if not navidrome.clear_playlist(playlist_id):
        log.error(f"  Не удалось безопасно очистить {playlist_name}")
        return False
    log.info(f"  Плейлист очищен")

    # Заполняем новыми треками
    if navidrome.add_songs_to_playlist(playlist_id, playlist_ids):
        log.info(f"  [✓] Добавлено {len(playlist_ids)} треков в {playlist_name}")
    else:
        log.error(f"  Не удалось заполнить {playlist_name}")
        return False
    return True


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

    # ListenBrainz history must be submitted by a real scrobbling client. The
    # Navidrome Subsonic API does not expose playback timestamps here.
    log.info("[LB] Skipping synthetic Navidrome listen import")

    need_weekly = should_rotate_weekly(state)
    need_monthly = should_rotate_monthly(state)

    if not need_weekly and not need_monthly:
        log.info("[*] Ротация не требуется")
        return

    # Загружаем рекомендации из ListenBrainz
    recommendations = fetch_lb_recommendation_playlists()

    if need_weekly:
        tracks = recommendations.get("weekly", [])
        if tracks and update_discovery_playlist(navidrome, WEEKLY_NAME, tracks, WEEKLY_COUNT):
            state["last_weekly"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        else:
            log.warning(
                "[Weekly] LB не вернул рекомендаций (недостаточно истории прослушиваний)"
            )

    if need_monthly:
        tracks = recommendations.get("monthly", [])
        if tracks and update_discovery_playlist(navidrome, MONTHLY_NAME, tracks, MONTHLY_COUNT):
            state["last_monthly"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        else:
            log.warning("[Monthly] LB не вернул рекомендаций")

    log.info("[*] Discovery обновление завершено.")


if __name__ == "__main__":
    main()
