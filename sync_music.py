#!/usr/bin/env python3
"""
sync_music.py v2.0
Автономный демон синхронизации музыкальных плейлистов.

Исправления и улучшения:
  - BUGFIX: Устранён duration-bypass (if diff<=5: artist_found=True)
  - BUGFIX: Матчинг по FILENAME а не полному пути (устранён album-folder contamination)
  - BUGFIX: Word-set matching вместо substring matching
  - NEW: Мониторинг загрузок slskd с retry-логикой (следующий пир при ошибке)
  - NEW: Качество FLAC как основной критерий сортировки (bitDepth/sampleRate/bitRate)
  - NEW: Обнаружение .lrc файла рядом с FLAC (бонус за лирику)
  - NEW: LLM-валидация через LiteLLM/Gemini для коротких/амбигуозных названий
  - NEW: LLM-нормализация как 4-я итерация поиска (если regex ничего не нашёл)
  - NEW: Recovery застрявших трансферов при старте
  - NEW: Backward-compatible история (формат v1-list → v2-dict)
"""

import requests
import time
import uuid
import subprocess
import json
import os
import re
import urllib.parse
import emoji
import mutagen
import logging
from datetime import datetime, timezone


def _state_is(state_str, *keywords):
    """slskd state — составная строка: 'Completed, Errored', 'Completed, Rejected' и т.п."""
    s = (state_str or "").lower()
    return any(kw.lower() in s for kw in keywords)


def _transfer_succeeded(state_str):
    return _state_is(state_str, "Completed") and not _state_is(
        state_str, "Errored", "Rejected", "TimedOut", "Cancelled", "Aborted"
    )


def _transfer_failed(state_str):
    return _state_is(
        state_str, "Errored", "Rejected", "TimedOut", "Cancelled", "Aborted"
    )


def _transfer_waiting(state_str):
    return _state_is(state_str, "Queued", "Requested", "Initializing")


# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
from config import (
    SLSKD_URL,
    SLSKD_USERNAME,
    SLSKD_PASSWORD,
    METUBE_URL,
    LITELLM_URL,
    DOWNLOAD_DIR,
    HISTORY_FILE,
    SYNC_LOG_FILE as LOG_FILE,
    PLAYLIST_URLS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    TOLERANCE_SEC,
    SEARCH_TIMEOUT,
    QUEUE_TIMEOUT_HOURS,
    MAX_RETRIES,
    LLM_SHORT_TITLE_WORDS,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    FALLBACK_MODELS,
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


# ─── ИСТОРИЯ (совместима с v1-list и v2-dict) ─────────────────────────────────
def load_history():
    """
    v1: ["artist - title", ...]  →  мигрируем в v2
    v2: {"version":2, "completed":[...], "pending":{...}, "failed":[...]}
    """
    if not os.path.exists(HISTORY_FILE):
        return {"version": 2, "completed": set(), "pending": {}, "failed": set()}

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        log.info(f"[History] Миграция v1→v2: {len(raw)} треков")
        return {
            "version": 2,
            "completed": set(raw),
            "pending": {},
            "failed": set(),
        }

    return {
        "version": 2,
        "completed": set(raw.get("completed", [])),
        "pending": raw.get("pending", {}),
        "failed": set(raw.get("failed", [])),
    }


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": 2,
                "completed": sorted(history["completed"]),
                "pending": history["pending"],
                "failed": sorted(history["failed"]),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────────────────────────────
def get_words(text):
    return set(re.findall(r"\w+", str(text).lower()))


def remove_emojis(text):
    return emoji.replace_emoji(text, replace="")


def normalize_text(text):
    return re.sub(r"[^\w]", "", str(text)).lower()


def super_clean_title(raw_title):
    """Удаляет технический мусор. Сохраняет remix/cover/live/feat."""
    text = remove_emojis(raw_title)
    junk = [
        r"official video",
        r"official music video",
        r"official audio",
        r"lyric video",
        r"lyrics",
        r"audio",
        r"remastered",
        r"remaster",
        r"4k",
        r"hd",
        r"hq",
        r"1080p",
        r"720p",
        r"music video",
        r"video",
        r"mv",
    ]
    for j in junk:
        text = re.sub(r"(?i)[\(\[\{【]\s*" + j + r"\s*[\)\]\}】]", "", text)
        text = re.sub(r"(?i)\-\s*" + j + r"\s*($|-)", "", text)
    text = re.sub(r"(?i)\s*[\(\[]\s*(feat\.?|ft\.?|featuring)[^\)\]]*[\)\]]", "", text)
    text = re.sub(r"(?i)\s+(feat\.?|ft\.?|featuring)\s+.*$", "", text)
    text = re.sub(r"[\(\[\{【]\s*[\)\]\}】]", "", text)
    text = re.sub(r"[^\w\s\-]", "", text)
    return " ".join(text.split()).strip(" -")


def parse_youtube_fallback(raw_title, uploader):
    text = remove_emojis(raw_title)
    if " - " in text:
        parts = text.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return uploader.replace(" - Topic", "").strip(), text.strip()


# ─── ПРОВЕРКА НАЛИЧИЯ ТРЕКА НА ДИСКЕ ────────────────────────────────────────
def is_track_in_library(artist, title):
    """
    ИСПРАВЛЕНО v2: Word-set matching вместо substring.
    Требует совпадения 85% слов названия в имени файла (не в полном пути).
    """
    clean_title = super_clean_title(title)
    clean_artist = super_clean_title(artist)
    title_words = get_words(clean_title)
    artist_norm = normalize_text(clean_artist)

    if not title_words:
        return False

    for root, _, files in os.walk(DOWNLOAD_DIR):
        for file in files:
            if not file.lower().endswith(
                (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav")
            ):
                continue

            filename_no_ext = os.path.splitext(file)[0]
            file_words = get_words(filename_no_ext)

            overlap = title_words & file_words
            if not title_words or len(overlap) / len(title_words) < 0.75:
                continue

            # Проверяем артиста в пути (включает папку артиста/альбома)
            path_norm = normalize_text(os.path.join(root, file))
            if not artist_norm or artist_norm in path_norm:
                return True

            # Фоллбэк: читаем теги
            try:
                audio = mutagen.File(os.path.join(root, file))
                if audio:
                    for key in ["artist", "\xa9ART", "TPE1", "aART", "TPE2"]:
                        if key in audio:
                            val = audio[key]
                            fa = normalize_text(
                                str(val[0]) if isinstance(val, list) else str(val)
                            )
                            if artist_norm in fa:
                                return True
                            break
            except Exception:
                pass
    return False


# ─── SLSKD API ────────────────────────────────────────────────────────────────
def get_slskd_token():
    resp = requests.post(
        f"{SLSKD_URL}/api/v0/session",
        json={"username": SLSKD_USERNAME, "password": SLSKD_PASSWORD},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"Ошибка авторизации slskd: {resp.text}")
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def get_all_transfers(token):
    """
    GET /api/v0/transfers/downloads
    Возвращает плоский список всех загрузок с полями:
    username, filename, id, state, size, bytesTransferred, enqueuedAt
    """
    try:
        resp = requests.get(
            f"{SLSKD_URL}/api/v0/transfers/downloads", headers=_h(token), timeout=15
        )
        if not resp.ok:
            return []
        result = []
        for user_entry in resp.json():
            username = user_entry.get("username", "")
            for directory in user_entry.get("directories", []):
                for f in directory.get("files", []):
                    result.append(
                        {
                            "username": username,
                            "filename": f.get("filename", ""),
                            "id": f.get("id", ""),
                            "state": f.get("state", ""),
                            "size": f.get("size", 0),
                            "bytesTransferred": f.get("bytesTransferred", 0),
                            "enqueuedAt": f.get("enqueuedAt", ""),
                        }
                    )
        return result
    except Exception as e:
        log.warning(f"Не удалось получить список загрузок slskd: {e}")
        return []


def cancel_transfer(token, username, transfer_id):
    try:
        encoded = urllib.parse.quote(username)
        requests.delete(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}/{transfer_id}",
            headers=_h(token),
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Не удалось отменить трансфер {transfer_id}: {e}")


def initiate_download(token, username, filename, size):
    encoded = urllib.parse.quote(username)
    payload = [{"filename": filename, "size": size}]
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}",
            headers=_h(token),
            json=payload,
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        log.warning(f"Ошибка инициации загрузки: {e}")
        return False


# ─── МАТЧИНГ ─────────────────────────────────────────────────────────────────
MODIFIERS = {
    "remix",
    "cover",
    "edit",
    "mashup",
    "instrumental",
    "acoustic",
    "live",
    "mix",
    "karaoke",
    "slowed",
    "reverb",
    "reprise",
}


def is_valid_match(file_path, raw_artist, raw_title, expected_dur, actual_dur):
    """
    ИСПРАВЛЕНО v2:
    1. Матчинг title → по FILENAME, не по полному пути (устраняет album-folder contamination)
    2. Word-set matching (не substring)
    3. Duration НЕ обходит проверку артиста
    """
    filename = os.path.basename(file_path)
    filename_stem = os.path.splitext(filename)[0]

    clean_title = super_clean_title(raw_title)
    clean_artist = super_clean_title(raw_artist)

    title_words = get_words(clean_title)
    artist_words = get_words(clean_artist)
    file_words = get_words(filename_stem)
    path_words = get_words(file_path)

    if not title_words:
        return False

    # 1. Совпадение названия в ИМЕНИ ФАЙЛА (word-set, порог 80%)
    overlap = title_words & file_words
    if len(overlap) / len(title_words) < 0.80:
        return False

    # 2. Модификаторы: если в пути есть remix/cover/etc. — брак
    for mod in MODIFIERS:
        if mod not in title_words and mod not in artist_words and mod in path_words:
            return False

    # 3. Длительность (без bypass-а артиста!)
    if expected_dur and actual_dur:
        if abs(expected_dur - actual_dur) > TOLERANCE_SEC:
            return False

    # 4. Артист: ищем в полном пути (включая папки Artist/Album)
    artist_norm = normalize_text(clean_artist)
    path_norm = normalize_text(file_path)
    artist_found = (not artist_norm) or (artist_norm in path_norm)

    return artist_found


# ─── LLM (OpenRouter Direct) ────────────────────────────────────────────────
def _llm_chat(prompt, max_tokens=20):
    """Универсальный вызов OpenRouter. Перебирает FALLBACK_MODELS по очереди."""
    if not OPENROUTER_API_KEY:
        log.warning("[LLM] OpenRouter API key не задан. Запрос пропущен.")
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cif/homeMusic",
        "X-Title": "homeMusic Sync",
    }

    # Уникальный список моделей для пробы
    models_to_try = []
    for m in FALLBACK_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    for model in models_to_try:
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15,
            )
            if resp.ok:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                if content:
                    return content
            else:
                log.warning(f"[LLM] Модель {model} вернула ошибку: HTTP {resp.status_code} — {resp.text[:150]}")
        except Exception as e:
            log.warning(f"[LLM] Ошибка при запросе к {model}: {e}")

    log.error("[LLM] Все модели OpenRouter вернули ошибку.")
    return None


def needs_llm_validation(raw_title):
    """Короткое/амбигуозное название → нужна LLM-валидация."""
    return len(get_words(super_clean_title(raw_title))) <= LLM_SHORT_TITLE_WORDS


def llm_is_match(artist, title, candidate_filename):
    """
    Спрашивает Gemini: является ли файл-кандидат правильным треком.
    При недоступности LLM → True (не блокируем).
    """
    prompt = (
        f"You are a music file validator. Answer ONLY with YES or NO.\n"
        f'Searching for: Artist="{artist}", Song="{title}"\n'
        f'Candidate file: "{os.path.basename(candidate_filename)}"\n'
        f"Is this the exact song? Covers, remixes, or songs with similar-but-different "
        f"titles are NOT correct. Answer YES only if artist AND title match."
    )
    answer = _llm_chat(prompt, max_tokens=5)
    if answer is None:
        return True  # LLM недоступен — не блокируем
    return "YES" in answer.upper()


def llm_normalize_title(raw_title, raw_artist):
    """
    Извлекает artist+title из «грязного» YouTube-заголовка через Gemini.
    Возвращает (artist, title) или (None, None).
    """
    prompt = (
        f"Extract music artist and song title.\n"
        f'Raw title: "{raw_title}"\n'
        f'Channel/uploader: "{raw_artist}"\n'
        f"Respond ONLY with JSON (no markdown, no explanation): "
        f'{{"artist": "...", "title": "..."}}'
    )
    answer = _llm_chat(prompt, max_tokens=80)
    if not answer:
        return None, None
    try:
        answer = re.sub(r"```[a-z]*", "", answer).strip()
        data = json.loads(answer)
        return data.get("artist"), data.get("title")
    except Exception:
        return None, None


# ─── КАЧЕСТВО И ЛИРИКА ───────────────────────────────────────────────────────
def check_has_lrc(candidate_filename, all_user_files):
    """Ищет .lrc файл в той же папке у того же пира."""
    folder = os.path.dirname(candidate_filename).lower()
    base = os.path.splitext(os.path.basename(candidate_filename))[0].lower()
    for f in all_user_files:
        fn = f.get("filename", "")
        if fn.lower().endswith(".lrc"):
            if os.path.dirname(fn).lower() == folder:
                if os.path.splitext(os.path.basename(fn))[0].lower() == base:
                    return True
    return False


def score_candidate(file_info, all_user_files):
    """
    Качество трека — главный критерий.
    Порядок: BitDepth > SampleRate > BitRate > Size > LRC-бонус.
    """
    score = 0

    # Глубина бит (24-bit >> 16-bit → разница 800 очков)
    bit_depth = file_info.get("bitDepth", 16) or 16
    score += bit_depth * 50

    # Частота дискретизации
    sample_rate = file_info.get("sampleRate", 44100) or 44100
    score += sample_rate // 1000 * 8  # 44kHz→352, 96kHz→768

    # Битрейт
    bit_rate = file_info.get("bitRate", 0) or 0
    score += bit_rate // 50  # 1000kbps → 20 очков

    # Размер файла (косвенный показатель)
    size_mb = (file_info.get("size", 0) or 0) / 1_000_000
    score += int(size_mb * 2)

    # Бонус за лирику
    if check_has_lrc(file_info.get("filename", ""), all_user_files):
        score += 300

    return score


# ─── ПОИСК В P2P ─────────────────────────────────────────────────────────────
def run_slskd_search(token, query):
    """Запускает поиск, ждёт SEARCH_TIMEOUT, возвращает сырые ответы."""
    search_id = str(uuid.uuid4())
    try:
        requests.post(
            f"{SLSKD_URL}/api/v0/searches",
            headers=_h(token),
            json={"id": search_id, "searchText": query},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"[P2P] Ошибка запроса поиска: {e}")
        return []

    log.info(f'      → Ожидание {SEARCH_TIMEOUT}с (P2P: "{query}")...')
    time.sleep(SEARCH_TIMEOUT)

    try:
        resp = requests.get(
            f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
            headers=_h(token),
            timeout=15,
        )
        return resp.json() if resp.ok else []
    except Exception as e:
        log.warning(f"[P2P] Ошибка получения ответов: {e}")
        return []


def collect_candidates(responses, raw_artist, raw_title, duration):
    """
    Фильтрует P2P ответы: только FLAC, только валидные совпадения.
    Для коротких названий добавляет LLM-валидацию.
    Возвращает список кандидатов, отсортированный по качеству.
    """
    use_llm = needs_llm_validation(raw_title)
    seen = set()
    candidates = []

    for user_resp in responses:
        username = user_resp.get("username", "")
        queue_len = user_resp.get("queueLength", 99999)
        up_speed = user_resp.get("uploadSpeed", 0)
        all_files = user_resp.get("files", [])

        for file in all_files:
            fname = file.get("filename", "")
            if not fname.lower().endswith(".flac"):
                continue

            actual_dur = file.get("length", 0)
            if not is_valid_match(fname, raw_artist, raw_title, duration, actual_dur):
                continue

            # LLM-валидация для коротких/амбигуозных названий
            if use_llm and not llm_is_match(raw_artist, raw_title, fname):
                log.info(f"        [LLM ✗] Отклонён: {os.path.basename(fname)}")
                continue

            key = (username, fname)
            if key in seen:
                continue
            seen.add(key)

            quality = score_candidate(file, all_files)
            candidates.append(
                {
                    "username": username,
                    "filename": fname,
                    "size": file.get("size", 0),
                    "queue": queue_len,
                    "speed": up_speed,
                    "quality": quality,
                }
            )

    # Качество DESC, очередь ASC, скорость DESC
    candidates.sort(key=lambda x: (-x["quality"], x["queue"], -x["speed"]))
    return candidates


def find_best_candidates(token, raw_artist, raw_title, duration):
    """
    4 итерации поиска. Возвращает полный список кандидатов (лучшие первые).
    Итерация 4 (LLM-нормализация) используется только если 1-3 ничего не нашли.
    """
    all_candidates = []
    seen_keys = set()

    def merge(new_list):
        for c in new_list:
            k = (c["username"], c["filename"])
            if k not in seen_keys:
                seen_keys.add(k)
                all_candidates.append(c)

    raw_query = f"{raw_artist} {raw_title}".strip()

    # Итерация 1: сырой запрос
    log.info(f"      [P2P iter 1] {raw_query}")
    merge(
        collect_candidates(
            run_slskd_search(token, raw_query), raw_artist, raw_title, duration
        )
    )
    if len(all_candidates) >= 5:
        return all_candidates

    # Итерация 2: очищенный запрос
    clean_q = f"{super_clean_title(raw_artist)} {super_clean_title(raw_title)}".strip()
    if clean_q and clean_q != raw_query:
        log.info(f"      [P2P iter 2] {clean_q}")
        merge(
            collect_candidates(
                run_slskd_search(token, clean_q), raw_artist, raw_title, duration
            )
        )
        if len(all_candidates) >= 5:
            return all_candidates

    # Итерация 3: только название
    title_only = super_clean_title(raw_title)
    if title_only and title_only not in (raw_query, clean_q):
        log.info(f"      [P2P iter 3] {title_only}")
        merge(
            collect_candidates(
                run_slskd_search(token, title_only), raw_artist, raw_title, duration
            )
        )

    # Итерация 4: LLM-нормализация (только если ничего не найдено)
    if not all_candidates:
        log.info("      [LLM] Нормализация названия через Gemini...")
        llm_artist, llm_title = llm_normalize_title(raw_title, raw_artist)
        if llm_artist and llm_title:
            llm_q = f"{llm_artist} {llm_title}"
            if llm_q not in (raw_query, clean_q):
                log.info(f"      [P2P iter 4/LLM] {llm_q}")
                merge(
                    collect_candidates(
                        run_slskd_search(token, llm_q), llm_artist, llm_title, duration
                    )
                )

    return all_candidates


# ─── МОНИТОРИНГ И RETRY ───────────────────────────────────────────────────────
def _retry_or_fallback(token, history, track_id, info, reason):
    """Пробует следующего пира. Если пиры кончились → MeTube."""
    retry_count = info.get("retry_count", 0) + 1
    candidates = info.get("candidates", [])

    # Убираем только что неудачного пира
    used_key = (info.get("username", ""), info.get("filename", ""))
    candidates = [c for c in candidates if (c["username"], c["filename"]) != used_key]

    artist = info.get("artist", track_id.split(" - ")[0] if " - " in track_id else "")
    title = info.get(
        "title", track_id.split(" - ", 1)[1] if " - " in track_id else track_id
    )

    if candidates and retry_count <= MAX_RETRIES:
        best = candidates[0]
        log.info(
            f"    → Ретрай {retry_count}/{MAX_RETRIES}: {best['username']} / {os.path.basename(best['filename'])}"
        )

        if initiate_download(token, best["username"], best["filename"], best["size"]):
            history["pending"][track_id] = {
                "username": best["username"],
                "filename": best["filename"],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "retry_count": retry_count,
                "candidates": candidates[1:],
                "url": info.get("url"),
                "artist": artist,
                "title": title,
            }
            log.info(f"    [✓] Ретрай инициирован ({reason})")
        else:
            log.error(f"    [✗] Не удалось инициировать ретрай")
            history["failed"].add(track_id)
            history["pending"].pop(track_id, None)
    else:
        log.info(f"    → Пиры исчерпаны / MAX_RETRIES → MeTube фоллбэк")
        if download_fallback_metube(artist, title, info.get("url")):
            history["completed"].add(track_id)
        else:
            history["failed"].add(track_id)
        history["pending"].pop(track_id, None)


def monitor_and_retry(token, history):
    """
    Проверяет статус всех pending загрузок.
    Обрабатывает: Completed, Errored, Cancelled, TimedOut, Rejected пропавшие трансферы.
    """
    pending = history.get("pending", {})
    if not pending:
        return 0

    log.info(f"[Monitor] Проверяем {len(pending)} pending загрузок...")
    active = get_all_transfers(token)
    xfer_map = {(t["username"], t["filename"]): t for t in active}
    now = datetime.now(timezone.utc)
    events = 0
    completed = []

    for track_id, info in list(pending.items()):
        username = info.get("username", "")
        filename = info.get("filename", "")
        xfer = xfer_map.get((username, filename))

        if xfer is None:
            # Трансфер исчез — проверяем по факту наличия файла
            artist = info.get("artist", "")
            title = info.get("title", "")
            if is_track_in_library(artist, title):
                log.info(f"[✓] Завершён (файл найден): {track_id}")
                history["completed"].add(track_id)
                completed.append(track_id)
                events += 1
            else:
                log.warning(f"[!] Трансфер пропал, файла нет → ретрай: {track_id}")
                _retry_or_fallback(token, history, track_id, info, reason="disappeared")
                events += 1
            continue

        state = xfer.get("state", "")

        if _transfer_succeeded(state):
            log.info(f"[✓] Загружено: {track_id}")
            history["completed"].add(track_id)
            completed.append(track_id)
            events += 1

        elif _transfer_failed(state):
            log.warning(f"[!] Ошибка ({state}): {track_id}")
            xfer_id = xfer.get("id", "")
            if xfer_id:
                cancel_transfer(token, username, xfer_id)
            _retry_or_fallback(token, history, track_id, info, reason=state)
            events += 1

        elif _transfer_waiting(state):
            started_str = info.get("started_at", "")
            if started_str:
                try:
                    started_at = datetime.fromisoformat(started_str)
                    hours_waiting = (now - started_at).total_seconds() / 3600
                    if hours_waiting > QUEUE_TIMEOUT_HOURS:
                        log.warning(
                            f"[!] Таймаут очереди {hours_waiting:.1f}ч: {track_id}"
                        )
                        xfer_id = xfer.get("id", "")
                        if xfer_id:
                            cancel_transfer(token, username, xfer_id)
                        _retry_or_fallback(
                            token, history, track_id, info, reason="queue_timeout"
                        )
                        events += 1
                except ValueError:
                    pass
        # InProgress — нормально, ждём

    for track_id in completed:
        history["pending"].pop(track_id, None)

    save_history(history)
    return events


def startup_slskd_recovery(token, history):
    """
    При старте: ищет Errored/TimedOut трансферы в slskd,
    которые помечены как completed в истории, но файла нет.
    Удаляет из completed → трек будет переобработан.
    """
    active = get_all_transfers(token)
    errored = [t for t in active if _transfer_failed(t["state"])]
    if not errored:
        return

    log.info(f"[Recovery] Найдено {len(errored)} проблемных трансферов в slskd")
    recovered = 0

    for xfer in errored:
        fname_stem = normalize_text(
            os.path.splitext(os.path.basename(xfer["filename"]))[0]
        )

        for track_id in list(history["completed"]):
            parts = track_id.split(" - ", 1)
            if len(parts) != 2:
                continue
            t_artist, t_title = parts
            title_norm = normalize_text(super_clean_title(t_title))

            # Совпадение имени файла с названием трека в истории
            if title_norm and len(title_norm) >= 4 and title_norm in fname_stem:
                if not is_track_in_library(t_artist, t_title):
                    log.info(f"  [Recovery] Возвращаем в очередь: {track_id}")
                    history["completed"].discard(track_id)
                    xfer_id = xfer.get("id", "")
                    if xfer_id:
                        cancel_transfer(token, xfer.get("username", ""), xfer_id)
                    recovered += 1
                break

    if recovered:
        log.info(f"[Recovery] {recovered} треков восстановлено для повторной обработки")
        save_history(history)


# ─── METUBE FALLBACK ─────────────────────────────────────────────────────────
def download_fallback_metube(artist, title, track_url=None):
    log.info(f"      [MeTube] Фоллбэк: {artist} - {title}")
    url = track_url if track_url else f"ytsearch1:{artist} {title}"
    try:
        resp = requests.post(
            f"{METUBE_URL}/add", json={"url": url, "quality": "audio"}, timeout=10
        )
        if resp.ok:
            log.info("      [+] Добавлено в очередь MeTube")
            return True
    except Exception as e:
        log.error(f"      [✗] Ошибка MeTube: {e}")
    return False


# ─── ПАРСЕРЫ ПЛЕЙЛИСТОВ ───────────────────────────────────────────────────────
def fetch_spotify_playlist(url):
    """Парсит Spotify плейлист через yt-dlp — не требует Premium и API credentials."""
    log.info(f"Парсинг Spotify (yt-dlp): {url}")
    cmd = ["yt-dlp", "-J", "--flat-playlist", "--no-warnings", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.error(f"yt-dlp Spotify ошибка: {result.stderr[:300]}")
            return []
        data = json.loads(result.stdout)
        tracks = []
        for entry in data.get("entries", []):
            artist = (entry.get("artist") or entry.get("uploader") or "").strip()
            title  = (entry.get("track")  or entry.get("title")    or "").strip()
            dur    = entry.get("duration", 0) or 0
            if artist and title:
                tracks.append({"artist": artist, "title": title, "duration": dur, "url": None})
        log.info(f"Spotify: получено {len(tracks)} треков")
        return tracks
    except Exception as e:
        log.error(f"Ошибка Spotify (yt-dlp): {e}")
        return []


def fetch_yandex_playlist(url):
    log.info(f"Парсинг Yandex Music: {url}")
    match = re.search(r"users/([^/]+)/playlists/(\d+)", url)
    if not match:
        return []
    user, kind = match.groups()

    try:
        resp = requests.get(
            f"https://api.music.yandex.net/users/{user}/playlists/{kind}", timeout=10
        )
        resp.raise_for_status()
        tracks = []
        for item in resp.json().get("result", {}).get("tracks", []):
            t = item.get("track", {})
            artist = ", ".join(a["name"] for a in t.get("artists", []))
            title = t.get("title", "")
            dur = t.get("durationMs", 0) // 1000
            if artist and title:
                tracks.append(
                    {"artist": artist, "title": title, "duration": dur, "url": None}
                )
        return tracks
    except Exception as e:
        log.error(f"Ошибка Yandex Music: {e}")
        return []


def fetch_ytdlp_playlist(url):
    log.info(f"Парсинг YouTube: {url}")
    cmd = [
        "yt-dlp",
        "-J",
        "--flat-playlist",
        "--extractor-args",
        "youtube:player_client=ios,android,web",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"Ошибка yt-dlp: {result.stderr[:300]}")
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    tracks = []
    for entry in data.get("entries", []):
        if entry.get("artist") and entry.get("track"):
            artist, title = entry["artist"], entry["track"]
        else:
            artist, title = parse_youtube_fallback(
                entry.get("title", ""), entry.get("uploader", "")
            )
        dur = entry.get("duration", 0)
        e_url = entry.get("url")
        if artist and title:
            tracks.append(
                {"artist": artist, "title": title, "duration": dur, "url": e_url}
            )
    return tracks


# ─── ГЛАВНЫЙ ЦИКЛ ────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("sync_music.py v2.0 запущен")
    log.info(f"Время: {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 60)

    history = load_history()

    try:
        token = get_slskd_token()
    except RuntimeError as e:
        log.critical(e)
        return

    # 1. Восстановление: ищем застрявшие трансферы от предыдущих запусков
    startup_slskd_recovery(token, history)

    # 2. Мониторинг текущих pending загрузок
    events = monitor_and_retry(token, history)
    log.info(f"[Monitor] Обработано событий: {events}")

    # 3. Парсинг плейлистов
    all_tracks = []
    for url in PLAYLIST_URLS:
        if "spotify.com" in url:
            all_tracks.extend(fetch_spotify_playlist(url))
        elif "yandex.ru" in url or "yandex.com" in url:
            all_tracks.extend(fetch_yandex_playlist(url))
        else:
            all_tracks.extend(fetch_ytdlp_playlist(url))

    log.info(f"\n[*] Треков для синхронизации: {len(all_tracks)}")

    # 4. Обработка новых треков
    for track_data in all_tracks:
        artist = track_data["artist"]
        title = track_data["title"]
        duration = track_data["duration"]
        url = track_data["url"]
        track_id = f"{artist} - {title}".lower()

        # Уже в очереди или завершён
        if track_id in history["completed"] or track_id in history["pending"]:
            log.info(f"[Skip] {artist} - {title}")
            continue

        # На диске
        if is_track_in_library(artist, title):
            log.info(f"[Skip/Disk] {artist} - {title}")
            history["completed"].add(track_id)
            save_history(history)
            continue

        # Сбрасываем failed — попробуем снова
        history["failed"].discard(track_id)

        log.info(f"\n--- Обработка: {artist} - {title} ---")
        candidates = find_best_candidates(token, artist, title, duration)

        if candidates:
            best = candidates[0]
            log.info(
                f"      [+] Лучший: {best['username']} | "
                f"{os.path.basename(best['filename'])} | "
                f"quality={best['quality']} queue={best['queue']}"
            )
            if initiate_download(
                token, best["username"], best["filename"], best["size"]
            ):
                history["pending"][track_id] = {
                    "username": best["username"],
                    "filename": best["filename"],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "retry_count": 0,
                    "candidates": candidates[1:],
                    "url": url,
                    "artist": artist,
                    "title": title,
                }
                log.info("      [✓] Загрузка инициирована")
            else:
                log.warning("      [!] Ошибка инициации → MeTube")
                if download_fallback_metube(artist, title, url):
                    history["completed"].add(track_id)
        else:
            log.info("      [-] FLAC не найден → MeTube")
            if download_fallback_metube(artist, title, url):
                history["completed"].add(track_id)

        save_history(history)

    log.info("\n[*] Синхронизация завершена.")
    save_history(history)


if __name__ == "__main__":
    main()
