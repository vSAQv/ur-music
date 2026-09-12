#!/usr/bin/env python3
"""Reconcile playlist tracks with slskd and the local music library.

The synchronizer deliberately treats a submitted transfer as pending until a
matching local audio file has been found and validated.  Only one managed
track is submitted per run; existing slskd transfers that are not represented
in the state file are observed but never cancelled.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import mutagen
import requests

try:
    import emoji
except ImportError:
    emoji = None

from config import (
    DOWNLOAD_DIR,
    FALLBACK_MODELS,
    HISTORY_FILE,
    LLM_SHORT_TITLE_WORDS,
    MAX_RETRIES,
    METUBE_URL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    PLAYLIST_URLS,
    QUEUE_TIMEOUT_HOURS,
    SEARCH_TIMEOUT,
    SLSKD_PASSWORD,
    SLSKD_URL,
    SLSKD_USERNAME,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SYNC_QUEUE_FILE,
    TOLERANCE_SEC,
    SYNC_LOG_FILE as LOG_FILE,
)


class SlskdUnavailable(RuntimeError):
    """Raised when the transfer API cannot be read safely."""


AUDIO_EXTENSIONS = (".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav")
BAD_TRANSFER_STATES = ("errored", "rejected", "timedout", "cancelled", "aborted")
WAITING_TRANSFER_STATES = ("queued", "requested", "initializing")
ACTIVE_TRANSFER_STATES = ("inprogress", "downloading", "transferring")
MODIFIERS = {
    "acoustic",
    "alternative",
    "club",
    "cover",
    "edit",
    "instrumental",
    "karaoke",
    "live",
    "mashup",
    "mix",
    "reprise",
    "remix",
    "reverb",
    "slowed",
}
TITLE_STOPWORDS = {"a", "an", "and", "of", "on", "the", "to"}
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "900"))
METUBE_TIMEOUT_HOURS = int(os.getenv("METUBE_TIMEOUT_HOURS", "12"))

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def _state_is(state, *keywords):
    value = (state or "").lower()
    return any(keyword.lower() in value for keyword in keywords)


def _transfer_succeeded(state):
    return _state_is(state, "completed") and not _state_is(state, *BAD_TRANSFER_STATES)


def _transfer_failed(state):
    return _state_is(state, *BAD_TRANSFER_STATES)


def _transfer_waiting(state):
    return _state_is(state, *WAITING_TRANSFER_STATES)


def _transfer_active(state):
    return _state_is(state, *ACTIVE_TRANSFER_STATES)


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _retry_due(info, now=None):
    retry_at = _parse_time(info.get("retry_after"))
    return retry_at is None or (now or _utc_now()) >= retry_at


def _retry_at():
    return (_utc_now() + timedelta(seconds=RETRY_DELAY_SECONDS)).isoformat()


def load_history():
    """Load v1/v2 history without discarding unknown legacy entries."""
    empty = {"version": 3, "completed": set(), "pending": {}, "failed": set()}
    if not os.path.exists(HISTORY_FILE):
        return empty
    with open(HISTORY_FILE, encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, list):
        empty["completed"].update(raw)
        return empty
    empty["completed"].update(raw.get("completed", []))
    empty["pending"].update(raw.get("pending", {}))
    empty["failed"].update(raw.get("failed", []))
    return empty


def save_history(history):
    """Atomically persist state and retain one recoverable previous copy."""
    directory = os.path.dirname(HISTORY_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(HISTORY_FILE):
        backup = HISTORY_FILE + ".bak"
        with open(HISTORY_FILE, encoding="utf-8") as source, open(backup, "w", encoding="utf-8") as target:
            target.write(source.read())
    payload = {
        "version": 3,
        "completed": sorted(history["completed"]),
        "pending": history["pending"],
        "failed": sorted(history["failed"]),
    }
    fd, temporary = tempfile.mkstemp(prefix="sync_history.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, HISTORY_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _queue_key(track):
    return _track_id(track.get("artist", ""), track.get("title", ""))


def load_download_queue():
    """Load queued recommendation tracks without discarding malformed state."""
    if not os.path.exists(SYNC_QUEUE_FILE):
        return []
    try:
        with open(SYNC_QUEUE_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, TypeError, ValueError) as error:
        log.error("Cannot read download queue: %s", error)
        return []
    if not isinstance(raw, list):
        log.error("Download queue must contain a JSON list")
        return []
    return [
        track
        for track in raw
        if isinstance(track, dict) and track.get("artist") and track.get("title")
    ]


def save_download_queue(queue):
    """Atomically persist the recommendation queue."""
    directory = os.path.dirname(SYNC_QUEUE_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="download_queue.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(queue, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SYNC_QUEUE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _deduplicate_tracks(tracks):
    result = []
    seen = set()
    for track in tracks:
        key = _queue_key(track)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(track)
    return result


def get_words(text):
    return set(re.findall(r"[\w]+", str(text).casefold(), flags=re.UNICODE))


def remove_emojis(text):
    return emoji.replace_emoji(text, replace="") if emoji else str(text)


def normalize_text(text):
    return "".join(sorted(get_words(text)))


def super_clean_title(raw_title):
    """Remove publication noise while retaining version identifiers."""
    text = remove_emojis(raw_title)
    junk = (
        r"official\s+(?:music\s+)?video|official\s+audio|lyric\s+video|lyrics?"
        r"|music\s+video|remastered|remaster|\d{3,4}p|\b(?:4k|hd|hq|mv)\b"
    )
    text = re.sub(rf"[\(\[\{{【]\s*{junk}\s*[\)\]\}}】]", " ", text, flags=re.I)
    text = re.sub(rf"(?:-|–|—)\s*{junk}\s*$", " ", text, flags=re.I)
    text = re.sub(r"[\(\[\{【]\s*[\)\]\}】]", " ", text)
    text = re.sub(r"[^\w\s'&+\-]", " ", text, flags=re.UNICODE)
    return " ".join(text.split()).strip(" -")


def _title_words(title):
    cleaned = super_clean_title(title)
    cleaned = re.split(r"\b(?:feat\.?|ft\.?|featuring)\b", cleaned, maxsplit=1, flags=re.I)[0]
    return get_words(cleaned)


def _modifier_words(text):
    return get_words(text) & MODIFIERS


def _remote_components(path):
    return [part for part in re.split(r"[\\/]+", str(path)) if part]


def remote_basename(path):
    components = _remote_components(path)
    return components[-1] if components else ""


def _component_contains_words(component, words):
    return bool(words) and words.issubset(get_words(os.path.splitext(component)[0]))


def _artist_in_path(path, artist):
    artist_words = get_words(super_clean_title(artist))
    if not artist_words:
        return True
    components = _remote_components(path)
    return any(_component_contains_words(component, artist_words) for component in components)


def _filename_words(path):
    stem = os.path.splitext(remote_basename(path))[0]
    words = get_words(stem)
    return {word for word in words if not word.isdecimal()}


def _audio_artist(audio):
    if not audio:
        return ""
    for key in ("artist", "\xa9ART", "TPE1", "aART", "TPE2"):
        try:
            value = audio.get(key)
        except (AttributeError, KeyError):
            value = None
        if value:
            if isinstance(value, (list, tuple)):
                return str(value[0])
            return str(value)
    return ""


def _audio_title(audio):
    if not audio:
        return ""
    for key in ("title", "\xa9nam", "TIT2"):
        try:
            value = audio.get(key)
        except (AttributeError, KeyError):
            value = None
        if value:
            if isinstance(value, (list, tuple)):
                return str(value[0])
            return str(value)
    return ""


def _audio_duration(audio):
    try:
        return float(audio.info.length)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def is_valid_match(file_path, raw_artist, raw_title, expected_dur=0, actual_dur=0):
    """Validate identity using filename title, path-component artist, and duration."""
    filename_words = _filename_words(file_path)
    target_title_words = _title_words(raw_title)
    artist_words = get_words(super_clean_title(raw_artist))
    if not target_title_words or not target_title_words.issubset(filename_words):
        return False

    candidate_modifiers = filename_words & MODIFIERS
    target_modifiers = _modifier_words(raw_title)
    if not target_modifiers.issubset(filename_words) or candidate_modifiers - target_modifiers:
        return False

    # Short titles are especially vulnerable to album-folder contamination.
    if len(target_title_words) <= 2:
        allowed = target_title_words | artist_words | MODIFIERS
        unexpected = {word for word in filename_words - allowed if not word.isdecimal()}
        if unexpected:
            return False

    if expected_dur and actual_dur and abs(float(expected_dur) - float(actual_dur)) > TOLERANCE_SEC:
        return False
    return _artist_in_path(file_path, raw_artist)


def is_track_in_library(artist, title, duration=0):
    """Return true only when a local audio file passes identity validation."""
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for filename in files:
            if not filename.casefold().endswith(AUDIO_EXTENSIONS):
                continue
            path = os.path.join(root, filename)
            audio = None
            try:
                audio = mutagen.File(path)
            except Exception:
                pass
            actual_duration = _audio_duration(audio)
            if is_valid_match(path, artist, title, duration, actual_duration):
                return True
            tagged_artist = _audio_artist(audio)
            tagged_title = _audio_title(audio)
            if tagged_artist and tagged_title:
                expected_title_words = _title_words(title)
                actual_title_words = _title_words(tagged_title)
                title_matches = expected_title_words.issubset(actual_title_words)
                if len(expected_title_words) <= 2:
                    title_matches = expected_title_words == actual_title_words
                if title_matches and expected_title_words and _title_words(artist).issubset(
                    get_words(super_clean_title(tagged_artist))
                ):
                    if not duration or not actual_duration or abs(float(duration) - actual_duration) <= TOLERANCE_SEC:
                        return True
    return False


def get_slskd_token():
    response = requests.post(
        f"{SLSKD_URL}/api/v0/session",
        json={"username": SLSKD_USERNAME, "password": SLSKD_PASSWORD},
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError(f"slskd authentication failed with HTTP {response.status_code}")
    try:
        return response.json()["token"]
    except (ValueError, KeyError) as error:
        raise RuntimeError("slskd authentication returned invalid JSON") from error


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def get_all_transfers(token):
    """Return all transfers or raise; an API failure must not look like an empty queue."""
    response = requests.get(
        f"{SLSKD_URL}/api/v0/transfers/downloads", headers=_headers(token), timeout=20
    )
    if not response.ok:
        raise SlskdUnavailable(f"transfer API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise SlskdUnavailable("transfer API returned invalid JSON") from error
    transfers = []
    for user_entry in payload:
        username = user_entry.get("username", "")
        for directory in user_entry.get("directories", []):
            for file_info in directory.get("files", []):
                transfers.append(
                    {
                        "username": username,
                        "filename": file_info.get("filename", ""),
                        "id": file_info.get("id", ""),
                        "state": file_info.get("state", ""),
                        "size": file_info.get("size", 0) or 0,
                        "bytesTransferred": file_info.get("bytesTransferred", 0) or 0,
                        "enqueuedAt": file_info.get("enqueuedAt", ""),
                        "bitRate": file_info.get("bitRate", 0) or 0,
                        "bitDepth": file_info.get("bitDepth", 0) or 0,
                        "sampleRate": file_info.get("sampleRate", 0) or 0,
                    }
                )
    return transfers


def cancel_transfer(token, username, transfer_id):
    if not transfer_id:
        return False
    encoded = urllib.parse.quote(username, safe="")
    response = requests.delete(
        f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}/{transfer_id}",
        headers=_headers(token),
        timeout=15,
    )
    if not response.ok:
        log.warning("Could not cancel managed transfer %s: HTTP %s", transfer_id, response.status_code)
    return response.ok


def initiate_download(token, username, filename, size):
    encoded = urllib.parse.quote(username, safe="")
    try:
        response = requests.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}",
            headers=_headers(token),
            json=[{"filename": filename, "size": size}],
            timeout=15,
        )
        return response.ok
    except requests.RequestException as error:
        log.warning("Download initiation failed: %s", error)
        return False


def check_has_lrc(candidate_filename, all_user_files):
    candidate_parts = _remote_components(candidate_filename)
    candidate_base = os.path.splitext(remote_basename(candidate_filename))[0].casefold()
    candidate_dir = [part.casefold() for part in candidate_parts[:-1]]
    for file_info in all_user_files:
        filename = file_info.get("filename", "")
        if not filename.casefold().endswith(".lrc"):
            continue
        parts = _remote_components(filename)
        if [part.casefold() for part in parts[:-1]] == candidate_dir and os.path.splitext(
            remote_basename(filename)
        )[0].casefold() == candidate_base:
            return True
    return False


def _quality_values(file_info, all_user_files):
    return (
        int(file_info.get("bitRate", 0) or 0),
        int(file_info.get("bitDepth", 0) or 0),
        int(file_info.get("sampleRate", 0) or 0),
        int(check_has_lrc(file_info.get("filename", ""), all_user_files)),
        int(file_info.get("size", 0) or 0),
    )


def score_candidate(file_info, all_user_files):
    """Return a sortable quality tuple; lyrics are intentionally secondary."""
    return _quality_values(file_info, all_user_files)


def run_slskd_search(token, query):
    search_id = str(uuid.uuid4())
    try:
        response = requests.post(
            f"{SLSKD_URL}/api/v0/searches",
            headers=_headers(token),
            json={"id": search_id, "searchText": query},
            timeout=15,
        )
        if not response.ok:
            log.warning("Search request failed for %r: HTTP %s", query, response.status_code)
            return []
        deadline = time.monotonic() + SEARCH_TIMEOUT
        while time.monotonic() < deadline:
            status = requests.get(
                f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=_headers(token), timeout=15
            )
            if status.ok:
                try:
                    if status.json().get("isComplete"):
                        break
                except ValueError:
                    pass
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        results = requests.get(
            f"{SLSKD_URL}/api/v0/searches/{search_id}/responses", headers=_headers(token), timeout=20
        )
        if not results.ok:
            return []
        return results.json()
    except (requests.RequestException, ValueError) as error:
        log.warning("Search failed for %r: %s", query, error)
        return []
    finally:
        try:
            requests.delete(
                f"{SLSKD_URL}/api/v0/searches/{search_id}", headers=_headers(token), timeout=10
            )
        except requests.RequestException:
            pass


def collect_candidates(responses, raw_artist, raw_title, duration):
    candidates = []
    seen = set()
    for user_response in responses:
        username = user_response.get("username", "")
        all_files = user_response.get("files", [])
        for file_info in all_files:
            filename = file_info.get("filename", "")
            if not filename.casefold().endswith(".flac"):
                continue
            if not is_valid_match(
                filename,
                raw_artist,
                raw_title,
                duration,
                file_info.get("length", 0) or 0,
            ):
                continue
            key = (username, filename)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "username": username,
                    "filename": filename,
                    "size": file_info.get("size", 0) or 0,
                    "queue": user_response.get("queueLength", 999999) or 999999,
                    "speed": user_response.get("uploadSpeed", 0) or 0,
                    "quality": score_candidate(file_info, all_files),
                }
            )
    candidates.sort(key=lambda item: (tuple(-value for value in item["quality"]), item["queue"], -item["speed"]))
    return candidates


def _llm_chat(prompt, max_tokens=80):
    if not OPENROUTER_API_KEY:
        return None
    models = []
    for model in [OPENROUTER_MODEL, *FALLBACK_MODELS]:
        if model and model not in models:
            models.append(model)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cif/homeMusic",
        "X-Title": "homeMusic Sync",
    }
    for model in models:
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
                timeout=20,
            )
            if response.ok:
                content = response.json()["choices"][0]["message"]["content"].strip()
                if content:
                    return content
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            log.warning("Optional OpenRouter normalization failed for %s: %s", model, error)
    return None


def llm_normalize_title(raw_title, raw_artist):
    """Optional search expansion only; LLM output never accepts a file."""
    answer = _llm_chat(
        f'Extract artist and title from this music metadata. Respond only as JSON with string keys "artist" and "title". Raw title: {raw_title!r}. Uploader: {raw_artist!r}',
        max_tokens=80,
    )
    if not answer:
        return None, None
    answer = re.sub(r"^```(?:json)?|```$", "", answer.strip(), flags=re.I).strip()
    try:
        payload = json.loads(answer)
        artist = payload.get("artist")
        title = payload.get("title")
        return (artist, title) if artist and title else (None, None)
    except (TypeError, ValueError):
        return None, None


def find_best_candidates(token, raw_artist, raw_title, duration):
    queries = []
    for query in (
        f"{raw_artist} {raw_title}".strip(),
        f"{super_clean_title(raw_artist)} {super_clean_title(raw_title)}".strip(),
        super_clean_title(raw_title),
    ):
        if query and query not in queries:
            queries.append(query)
    candidates = []
    seen = set()
    for query in queries:
        log.info('[P2P] Searching "%s"', query)
        for candidate in collect_candidates(run_slskd_search(token, query), raw_artist, raw_title, duration):
            key = (candidate["username"], candidate["filename"])
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
        if len(candidates) >= 5:
            break
    if not candidates:
        llm_artist, llm_title = llm_normalize_title(raw_title, raw_artist)
        if llm_artist and llm_title:
            query = f"{llm_artist} {llm_title}"
            log.info('[P2P] Optional normalized search "%s"', query)
            candidates.extend(collect_candidates(run_slskd_search(token, query), raw_artist, raw_title, duration))
    candidates.sort(key=lambda item: (tuple(-value for value in item["quality"]), item["queue"], -item["speed"]))
    return candidates


def _same_source(left, right):
    return left.get("username") == right.get("username") and left.get("filename") == right.get("filename")


def _transfer_for_pending(transfers, info):
    transfer_id = str(info.get("transfer_id", ""))
    if transfer_id:
        for transfer in transfers:
            if str(transfer.get("id", "")) == transfer_id:
                return transfer
    username = info.get("username", "")
    filename = info.get("filename", "")
    for transfer in transfers:
        if transfer.get("username") == username and transfer.get("filename") == filename:
            return transfer
    return None


def _has_unmanaged_active_transfer(transfers, pending):
    managed_keys = {(info.get("username"), info.get("filename")) for info in pending.values()}
    return any(
        _transfer_active(transfer.get("state"))
        and (transfer.get("username"), transfer.get("filename")) not in managed_keys
        for transfer in transfers
    )


def _find_new_candidate(info):
    tried = {(source.get("username"), source.get("filename")) for source in info.get("tried_sources", [])}
    current = (info.get("username"), info.get("filename"))
    tried.add(current)
    for candidate in info.get("candidates", []):
        if (candidate.get("username"), candidate.get("filename")) not in tried:
            return candidate
    return None


def _queue_metube(history, track_id, info, reason):
    artist = info.get("artist", "")
    title = info.get("title", "")
    url = info.get("url") or f"ytsearch1:{artist} {title}"
    log.info("[MeTube] Queueing fallback for %s - %s (%s)", artist, title, reason)
    try:
        response = requests.post(
            f"{METUBE_URL}/add", json={"url": url, "quality": "audio"}, timeout=15
        )
        if not response.ok:
            log.warning("MeTube rejected fallback with HTTP %s", response.status_code)
            return False
    except requests.RequestException as error:
        log.warning("MeTube unavailable: %s", error)
        return False
    history["pending"][track_id] = {
        **info,
        "backend": "metube",
        "submitted_at": _utc_now().isoformat(),
        "retry_after": None,
        "last_error": reason,
    }
    return True


def _start_candidate(history, track_id, info, candidate, token):
    if not initiate_download(token, candidate["username"], candidate["filename"], candidate["size"]):
        return False
    tried_sources = list(info.get("tried_sources", []))
    history["pending"][track_id] = {
        **info,
        "backend": "slskd",
        "username": candidate["username"],
        "filename": candidate["filename"],
        "size": candidate.get("size", 0),
        "started_at": _utc_now().isoformat(),
        "retry_after": None,
        "last_error": None,
        "candidates": [item for item in info.get("candidates", []) if not _same_source(item, candidate)],
        "tried_sources": tried_sources,
    }
    try:
        transfers = get_all_transfers(token)
        transfer = next(
            (
                item
                for item in transfers
                if item.get("username") == candidate["username"] and item.get("filename") == candidate["filename"]
            ),
            None,
        )
        if transfer and transfer.get("id"):
            history["pending"][track_id]["transfer_id"] = transfer["id"]
    except SlskdUnavailable:
        log.warning("Could not attach slskd transfer ID; source matching will be used")
    return True


def _retry_or_fallback(token, history, track_id, info, reason, transfers):
    if not _retry_due(info):
        return False
    retry_count = int(info.get("retry_count", 0) or 0) + 1
    tried_sources = list(info.get("tried_sources", []))
    current = {"username": info.get("username", ""), "filename": info.get("filename", ""), "state": reason}
    if current["username"] or current["filename"]:
        tried_sources.append(current)

    if retry_count > MAX_RETRIES:
        fallback_info = {**info, "retry_count": retry_count, "tried_sources": tried_sources}
        history["pending"].pop(track_id, None)
        return _queue_metube(history, track_id, fallback_info, reason)

    if _has_unmanaged_active_transfer(transfers, history["pending"]):
        info["retry_after"] = _retry_at()
        info["last_error"] = "another slskd transfer is active"
        return False

    candidate = _find_new_candidate(info)
    if candidate is None:
        candidates = find_best_candidates(token, info.get("artist", ""), info.get("title", ""), info.get("duration", 0))
        tried = {(source.get("username"), source.get("filename")) for source in tried_sources}
        candidate = next(
            (item for item in candidates if (item["username"], item["filename"]) not in tried), None
        )
        info["candidates"] = candidates
    if candidate is None:
        info["retry_count"] = retry_count
        info["tried_sources"] = tried_sources
        info["retry_after"] = _retry_at()
        info["last_error"] = "no validated alternative peer"
        return False

    cancel_transfer(token, info.get("username", ""), info.get("transfer_id", ""))
    new_info = {
        **info,
        "retry_count": retry_count,
        "tried_sources": tried_sources,
        "candidates": info.get("candidates", []),
    }
    history["pending"].pop(track_id, None)
    if not _start_candidate(history, track_id, new_info, candidate, token):
        history["pending"][track_id] = {
            **new_info,
            "retry_after": _retry_at(),
            "last_error": "download initiation failed",
        }
        return False
    log.info("Retry %s/%s started for %s", retry_count, MAX_RETRIES, track_id)
    return True


def monitor_and_retry(token, history):
    """Reconcile managed transfers; API failures leave state untouched."""
    if not history["pending"]:
        return 0
    try:
        transfers = get_all_transfers(token)
    except SlskdUnavailable as error:
        log.error("Cannot reconcile transfers: %s", error)
        return 0
    events = 0
    now = _utc_now()
    for track_id, info in list(history["pending"].items()):
        artist = info.get("artist", "")
        title = info.get("title", "")
        backend = info.get("backend", "slskd")
        if backend == "metube":
            if is_track_in_library(artist, title, info.get("duration", 0)):
                history["completed"].add(track_id)
                history["pending"].pop(track_id, None)
                events += 1
            elif _parse_time(info.get("submitted_at")) and now - _parse_time(info["submitted_at"]) > timedelta(hours=METUBE_TIMEOUT_HOURS):
                history["pending"].pop(track_id, None)
                if _queue_metube(history, track_id, info, "MeTube output timeout"):
                    events += 1
            continue

        transfer = _transfer_for_pending(transfers, info)
        if transfer is None:
            if is_track_in_library(artist, title, info.get("duration", 0)):
                history["completed"].add(track_id)
                history["pending"].pop(track_id, None)
                events += 1
            elif info.get("transfer_id") and not info.get("retry_after"):
                info["retry_after"] = _retry_at()
                info["last_error"] = "transfer disappeared"
            elif info.get("transfer_id") and _retry_due(info):
                if _retry_or_fallback(token, history, track_id, info, "transfer disappeared", transfers):
                    events += 1
            continue

        state = transfer.get("state", "")
        info["transfer_id"] = transfer.get("id") or info.get("transfer_id")
        if _transfer_succeeded(state):
            if is_track_in_library(artist, title, info.get("duration", 0)):
                history["completed"].add(track_id)
                history["pending"].pop(track_id, None)
                events += 1
            else:
                reason = "slskd completed but local identity validation failed"
                if info.get("retry_after") and _retry_due(info):
                    if _retry_or_fallback(token, history, track_id, info, reason, transfers):
                        events += 1
                else:
                    info["retry_after"] = _retry_at()
                    info["last_error"] = reason
        elif _transfer_failed(state):
            if not info.get("retry_after"):
                info["retry_after"] = _retry_at()
                info["last_error"] = state
            elif _retry_due(info) and _retry_or_fallback(token, history, track_id, info, state, transfers):
                events += 1
        elif _transfer_waiting(state):
            started = _parse_time(info.get("started_at"))
            if started and now - started > timedelta(hours=QUEUE_TIMEOUT_HOURS) and _retry_due(info):
                if not info.get("retry_after"):
                    info["retry_after"] = _retry_at()
                    info["last_error"] = "queue timeout"
                elif _retry_or_fallback(token, history, track_id, info, "queue timeout", transfers):
                    events += 1

    save_history(history)
    return events


def download_fallback_metube(artist, title, track_url=None):
    """Compatibility helper; callers must store pending state separately."""
    url = track_url or f"ytsearch1:{artist} {title}"
    try:
        response = requests.post(
            f"{METUBE_URL}/add", json={"url": url, "quality": "audio"}, timeout=15
        )
        return response.ok
    except requests.RequestException:
        return False


def _spotify_playlist_id(url):
    match = re.search(r"(?:playlist(?:s)?/|spotify:playlist:)([A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def fetch_spotify_playlist(url):
    """Fetch Spotify tracks through the client-credentials Web API."""
    playlist_id = _spotify_playlist_id(url)
    if not playlist_id or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        log.error("Spotify URL or client credentials are missing")
        return []
    basic = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        token_response = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        if not token_response.ok:
            log.error("Spotify token request failed with HTTP %s", token_response.status_code)
            return []
        token = token_response.json().get("access_token")
        if not token:
            log.error("Spotify token response did not contain an access token")
            return []
        endpoint = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100"
        tracks = []
        while endpoint:
            response = requests.get(endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            if not response.ok:
                log.error("Spotify playlist request failed with HTTP %s", response.status_code)
                return tracks
            payload = response.json()
            for item in payload.get("items", []):
                track = item.get("track") or {}
                artists = ", ".join(artist.get("name", "") for artist in track.get("artists", []))
                title = track.get("name", "")
                if artists and title:
                    tracks.append(
                        {
                            "artist": artists,
                            "title": title,
                            "duration": (track.get("duration_ms", 0) or 0) // 1000,
                            "url": track.get("external_urls", {}).get("spotify"),
                        }
                    )
            endpoint = payload.get("next")
        log.info("Spotify: received %s tracks", len(tracks))
        return tracks
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        log.error("Spotify API error: %s", error)
        return []


def fetch_yandex_playlist(url):
    log.info("Parsing Yandex Music playlist: %s", url)
    match = re.search(r"users/([^/?]+)/playlists/(\d+)", url)
    if not match:
        log.error("Unsupported Yandex playlist URL")
        return []
    user, playlist_id = match.groups()
    endpoint = f"https://api.music.yandex.net/users/{urllib.parse.quote(user)}/playlists/{playlist_id}"
    try:
        response = requests.get(endpoint, headers={"User-Agent": "ur-music/1.0"}, timeout=20)
        if not response.ok:
            log.error("Yandex playlist request failed with HTTP %s", response.status_code)
            return []
        tracks = []
        for item in response.json().get("result", {}).get("tracks", []):
            track = item.get("track") or {}
            artist = ", ".join(a.get("name", "") for a in track.get("artists", []))
            title = track.get("title", "")
            if artist and title:
                tracks.append(
                    {
                        "artist": artist,
                        "title": title,
                        "duration": (track.get("durationMs", 0) or 0) // 1000,
                        "url": None,
                    }
                )
        log.info("Yandex Music: received %s tracks", len(tracks))
        return tracks
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        log.error("Yandex Music API error: %s", error)
        return []


def parse_youtube_fallback(raw_title, uploader):
    text = remove_emojis(raw_title)
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return artist.strip(), title.strip()
    return uploader.replace(" - Topic", "").strip(), text.strip()


def fetch_ytdlp_playlist(url):
    log.info("Parsing YouTube playlist: %s", url)
    command = [
        "yt-dlp",
        "-J",
        "--flat-playlist",
        "--no-warnings",
        "--extractor-args",
        "youtube:player_client=ios,android,web",
        url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode:
            log.error("yt-dlp failed: %s", result.stderr[:300])
            return []
        payload = json.loads(result.stdout)
        tracks = []
        for entry in payload.get("entries", []):
            if entry.get("artist") and entry.get("track"):
                artist, title = entry["artist"], entry["track"]
            else:
                artist, title = parse_youtube_fallback(entry.get("title", ""), entry.get("uploader", ""))
            if artist and title:
                tracks.append(
                    {
                        "artist": artist,
                        "title": title,
                        "duration": entry.get("duration", 0) or 0,
                        "url": entry.get("webpage_url") or entry.get("url"),
                    }
                )
        return tracks
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        log.error("YouTube playlist error: %s", error)
        return []


def _track_id(artist, title):
    return f"{artist.strip()} - {title.strip()}".casefold()


def _track_info(track):
    return {
        "artist": track["artist"],
        "title": track["title"],
        "duration": track.get("duration", 0) or 0,
        "url": track.get("url"),
        "retry_count": 0,
        "candidates": [],
        "tried_sources": [],
        "backend": "slskd",
    }


def _playlist_tracks():
    tracks = []
    for url in PLAYLIST_URLS:
        if "spotify.com" in url or url.startswith("spotify:"):
            tracks.extend(fetch_spotify_playlist(url))
        elif "yandex." in url:
            tracks.extend(fetch_yandex_playlist(url))
        else:
            tracks.extend(fetch_ytdlp_playlist(url))
    return tracks


def main():
    log.info("ur-music reconciliation started")
    history = load_history()
    download_queue = load_download_queue()
    try:
        token = get_slskd_token()
    except RuntimeError as error:
        log.critical("%s", error)
        return

    monitor_and_retry(token, history)
    if history["pending"]:
        log.info("One managed operation remains pending; no new track will be submitted")
        return
    try:
        transfers = get_all_transfers(token)
    except SlskdUnavailable as error:
        log.error("Cannot inspect slskd before submission: %s", error)
        return
    if any(_transfer_active(transfer.get("state")) for transfer in transfers):
        log.info("An existing slskd transfer is active; unmanaged transfers were left untouched")
        return

    # User playlists retain priority; recommendation entries are processed after
    # them and remain queued until the local file is validated.
    tracks = _deduplicate_tracks(_playlist_tracks() + download_queue)
    queue_keys = {_queue_key(track) for track in download_queue}
    queue_changed = False
    for track in tracks:
        artist, title = track["artist"].strip(), track["title"].strip()
        track_id = _track_id(artist, title)
        if track_id in history["completed"] or track_id in history["pending"]:
            if track_id in history["completed"] and track_id in queue_keys:
                download_queue = [item for item in download_queue if _queue_key(item) != track_id]
                queue_keys.discard(track_id)
                queue_changed = True
            continue
        if is_track_in_library(artist, title, track.get("duration", 0)):
            history["completed"].add(track_id)
            save_history(history)
            if track_id in queue_keys:
                download_queue = [item for item in download_queue if _queue_key(item) != track_id]
                queue_keys.discard(track_id)
                queue_changed = True
            continue
        candidates = find_best_candidates(token, artist, title, track.get("duration", 0))
        info = _track_info(track)
        info["candidates"] = candidates
        if candidates:
            if _start_candidate(history, track_id, info, candidates[0], token):
                log.info("Started slskd transfer for %s", track_id)
            else:
                log.warning("Could not start slskd transfer for %s; leaving it retryable", track_id)
                history["pending"][track_id] = {**info, "retry_after": _retry_at(), "last_error": "initiation failed"}
        else:
            _queue_metube(history, track_id, info, "no validated FLAC candidate")
        if queue_changed:
            save_download_queue(download_queue)
        save_history(history)
        return
    if queue_changed:
        save_download_queue(download_queue)
    save_history(history)
    log.info("No new playlist track requires submission")


if __name__ == "__main__":
    main()
