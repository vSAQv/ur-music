#!/usr/bin/env python3
"""
recover_stuck.py — one-shot recovery for stalled / rejected downloads.

Problem: tracks in slskd may be stuck in Errored / Rejected / TimedOut states
while history still marks them as completed or pending with a bad peer.
sync_music.py would skip them forever.

Solution:
  1. Find all stuck slskd transfers and match them back to track IDs in history.
  2. Cancel each bad transfer.
  3. Run a fresh P2P search for the track, excluding all previously tried sources.
  4. Immediately re-queue the download with the best available new peer.
  5. Save the updated history so sync_music.py can monitor progress normally.

Tracks with no available new peer are moved to the "failed" set so that
the next sync_music.py run will attempt them again via its normal flow.
"""

import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests

from config import (
    SLSKD_URL,
    SLSKD_USERNAME,
    SLSKD_PASSWORD,
    HISTORY_FILE,
    MUSIC_ROOT,
    SEARCH_TIMEOUT,
    MAX_RETRIES,
)

# Transfer states that indicate a failed / stuck download
STUCK_STATES = ("errored", "rejected", "cancelled", "timedout", "aborted")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize(text):
    """Strip non-alphanumeric characters and lowercase."""
    return re.sub(r"[^\w]", "", str(text).lower())


def is_on_disk(artist, title):
    """Quick scan of MUSIC_ROOT to check if a track already exists on disk."""
    title_norm = normalize(title)
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


# ─── SLSKD API ────────────────────────────────────────────────────────────────

def slskd_auth():
    resp = requests.post(
        f"{SLSKD_URL}/api/v0/session",
        json={"username": SLSKD_USERNAME, "password": SLSKD_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def get_all_transfers(token):
    """Return a flat list of all downloads with their state info."""
    resp = requests.get(
        f"{SLSKD_URL}/api/v0/transfers/downloads",
        headers=_headers(token),
        timeout=15,
    )
    resp.raise_for_status()
    result = []
    for user_entry in resp.json():
        username = user_entry.get("username", "")
        for directory in user_entry.get("directories", []):
            for f in directory.get("files", []):
                result.append({
                    "username": username,
                    "filename": f.get("filename", ""),
                    "id":       f.get("id", ""),
                    "state":    f.get("state", ""),
                    "size":     f.get("size", 0),
                })
    return result


def cancel_transfer(token, username, transfer_id):
    try:
        encoded = urllib.parse.quote(username)
        requests.delete(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}/{transfer_id}",
            headers=_headers(token),
            timeout=10,
        )
    except Exception as e:
        print(f"  ⚠  Could not cancel transfer {transfer_id}: {e}")


def search_slskd(token, query):
    """
    Run a P2P search and wait for results (capped at 30 s for interactive use).
    Returns the list of response objects from slskd.
    """
    search_id = None
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/searches",
            headers=_headers(token),
            json={"searchText": query},
            timeout=10,
        )
        if not resp.ok:
            return []
        search_id = resp.json().get("id")
        if not search_id:
            return []

        deadline = time.time() + min(SEARCH_TIMEOUT, 30)
        while time.time() < deadline:
            time.sleep(3)
            r = requests.get(
                f"{SLSKD_URL}/api/v0/searches/{search_id}",
                headers=_headers(token),
                timeout=10,
            )
            if r.ok and r.json().get("isComplete", False):
                break

        r = requests.get(
            f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
            headers=_headers(token),
            timeout=15,
        )
        return r.json() if r.ok else []
    except Exception as e:
        print(f"  ⚠  Search error for {query!r}: {e}")
        return []
    finally:
        if search_id:
            try:
                requests.delete(
                    f"{SLSKD_URL}/api/v0/searches/{search_id}",
                    headers=_headers(token),
                    timeout=5,
                )
            except Exception:
                pass


def _file_score(f_info):
    """Quality score: FLAC > MP3/other, higher size = better."""
    fname = (f_info.get("filename") or "").lower()
    size = f_info.get("size", 0)
    score = 0
    if fname.endswith(".flac"):
        score += 1000
    elif fname.endswith(".mp3"):
        score += 500
    score += min(size // 1_000_000, 100)  # up to 100 pts for file size
    return score


def find_new_peer(token, artist, title, exclude_sources):
    """
    Search slskd for the track and return the best candidate whose
    (username, filename) pair is NOT in exclude_sources.

    Tries two queries in order: "artist title", then "title" alone.
    Returns a dict {username, filename, size} or None.
    """
    audio_exts = (".flac", ".mp3", ".m4a", ".ogg")
    title_norm = normalize(title)
    queries = [f"{artist} {title}".strip(), title.strip()]

    for query in queries:
        responses = search_slskd(token, query)
        candidates = []
        for resp in responses:
            username = resp.get("username", "")
            for f_info in resp.get("files", []):
                fname = f_info.get("filename", "")
                if not fname.lower().endswith(audio_exts):
                    continue
                if (username, fname) in exclude_sources:
                    continue
                fname_norm = normalize(os.path.splitext(os.path.basename(fname))[0])
                # Require that the track title appears in the filename
                if title_norm and len(title_norm) >= 3 and title_norm not in fname_norm:
                    continue
                candidates.append({
                    "username": username,
                    "filename": fname,
                    "size":     f_info.get("size", 0),
                    "score":    _file_score(f_info),
                })
        if candidates:
            candidates.sort(key=lambda x: x["score"], reverse=True)
            return candidates[0]

    return None


def initiate_download(token, username, filename, size):
    encoded = urllib.parse.quote(username)
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/transfers/downloads/{encoded}",
            headers=_headers(token),
            json=[{"filename": filename, "size": size}],
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        print(f"  ⚠  Download initiation error: {e}")
        return False


# ─── History helpers ───────────────────────────────────────────────────────────

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"version": 2, "completed": set(), "pending": {}, "failed": set()}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        # v1 format migration
        return {"version": 2, "completed": set(raw), "pending": {}, "failed": set()}
    return {
        "version":   2,
        "completed": set(raw.get("completed", [])),
        "pending":   dict(raw.get("pending", {})),
        "failed":    set(raw.get("failed", [])),
    }


def save_history(history):
    backup = HISTORY_FILE + ".bak"
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            content = f.read()
        with open(backup, "w") as f:
            f.write(content)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version":   2,
                "completed": sorted(history["completed"]),
                "pending":   history["pending"],
                "failed":    sorted(history["failed"]),
            },
            f, ensure_ascii=False, indent=2,
        )


# ─── Transfer → history matching ─────────────────────────────────────────────

def match_transfer_to_track(transfer, history):
    """
    Identify which track_id in history corresponds to a stuck slskd transfer.

    Strategy (in order of reliability):
    1. Exact filename match against 'pending' entries.
    2. Title substring match against 'completed' and 'failed' entries.

    Returns (track_id, section) where section is 'completed', 'pending', or
    'failed', or (None, None) if no match is found.
    """
    fname_base = normalize(os.path.basename(transfer["filename"]))
    fname_stem = normalize(os.path.splitext(os.path.basename(transfer["filename"]))[0])

    if not fname_stem or len(fname_stem) < 3:
        return None, None

    # 1. Exact pending match (we stored the filename when we initiated the download)
    for track_id, info in history["pending"].items():
        stored = normalize(os.path.basename(info.get("filename", "")))
        if stored and stored == fname_base:
            return track_id, "pending"

    # 2. Title substring match on completed / failed
    for section in ("completed", "failed"):
        for track_id in history[section]:
            parts = track_id.split(" - ", 1)
            if len(parts) != 2:
                continue
            _, title = parts
            title_norm = normalize(title)
            if len(title_norm) >= 4 and title_norm in fname_stem:
                return track_id, section

    return None, None


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("recover_stuck.py")
    print("=" * 60)

    # 1. Authenticate with slskd
    try:
        token = slskd_auth()
        print("✅ slskd: authenticated")
    except Exception as e:
        print(f"❌ slskd unavailable: {e}")
        return

    # 2. Load history
    if not os.path.exists(HISTORY_FILE):
        print(f"❌ History file not found: {HISTORY_FILE}")
        return
    history = load_history()
    print(
        f"ℹ  History: completed={len(history['completed'])}, "
        f"pending={len(history['pending'])}, failed={len(history['failed'])}"
    )

    # 3. Get all transfers from slskd
    try:
        all_transfers = get_all_transfers(token)
        print(f"ℹ  slskd: {len(all_transfers)} total transfers")
    except Exception as e:
        print(f"❌ Failed to fetch transfers: {e}")
        return

    # 4. Identify stuck and in-progress transfers
    stuck = [
        t for t in all_transfers
        if any(s in (t.get("state") or "").lower() for s in STUCK_STATES)
        and "inprogress" not in (t.get("state") or "").lower()
    ]
    in_progress = [
        t for t in all_transfers
        if "inprogress" in (t.get("state") or "").lower()
    ]

    print(f"ℹ  Stuck: {len(stuck)}, in-progress: {len(in_progress)}")
    if in_progress:
        print("\n⚠  Currently downloading (will not be touched):")
        for t in in_progress:
            print(f"   [InProgress] {os.path.basename(t['filename'])}")

    # 5. Match stuck transfers to history entries
    to_recover  = []  # (track_id, section, transfer)
    already_done = [] # on disk, just slskd hasn't cleaned up yet
    unmatched   = []  # cannot link to any track_id

    for t in stuck:
        track_id, section = match_transfer_to_track(t, history)
        if track_id is None:
            unmatched.append(t)
            continue
        parts = track_id.split(" - ", 1)
        artist = parts[0] if len(parts) == 2 else ""
        title  = parts[1] if len(parts) == 2 else track_id
        if is_on_disk(artist, title):
            already_done.append((track_id, t))
        else:
            to_recover.append((track_id, section, t))

    print()
    if already_done:
        print(f"✅ Already on disk — no action needed ({len(already_done)}):")
        for tid, t in already_done:
            print(f"   [{t['state']:30s}] {tid}")

    if unmatched:
        print(f"⚠  {len(unmatched)} stuck transfer(s) could not be matched to any track in history (skipped):")
        for t in unmatched:
            print(f"   [{t['state']:30s}] {os.path.basename(t['filename'])}")

    if not to_recover:
        print("✅ No stuck tracks require recovery.")
        return

    print(f"\n⚠  {len(to_recover)} track(s) need recovery:")
    for track_id, section, t in to_recover:
        print(f"   [{t['state']:30s}] {track_id}")
        print(f"   {'':30s}  ↳ peer: {t['username']}  file: {os.path.basename(t['filename'])}")

    print()
    answer = input("Re-download with new peers? (yes/no): ").strip().lower()
    if answer != "yes":
        print("Cancelled.")
        return

    # 6. Cancel bad transfers, search for new peers, re-queue
    requeued = 0
    no_peer  = 0

    for track_id, section, t in to_recover:
        parts  = track_id.split(" - ", 1)
        artist = parts[0] if len(parts) == 2 else ""
        title  = parts[1] if len(parts) == 2 else track_id

        print(f"\n--- Recovering: {track_id} ---")
        print(f"    Bad source: [{t['state']}] {t['username']} / {os.path.basename(t['filename'])}")

        # Cancel the stuck transfer in slskd
        if t["id"]:
            cancel_transfer(token, t["username"], t["id"])
            print(f"  ✅ Cancelled bad transfer")

        # Build exclusion list: current bad source + all previously tried sources
        pending_info = history["pending"].get(track_id, {})
        exclude: set = {(t["username"], t["filename"])}
        for src in pending_info.get("tried_sources", []):
            exclude.add((src.get("username", ""), src.get("filename", "")))
        if pending_info.get("username") and pending_info.get("filename"):
            exclude.add((pending_info["username"], pending_info["filename"]))

        # Record the bad source in the tried_sources log
        tried_sources = list(pending_info.get("tried_sources", []))
        tried_sources.append({
            "username": t["username"],
            "filename": t["filename"],
            "state":    t["state"],
        })

        # Remove from its current history section
        if section == "completed":
            history["completed"].discard(track_id)
        elif section == "failed":
            history["failed"].discard(track_id)
        elif section == "pending":
            history["pending"].pop(track_id, None)

        # Search for a fresh peer
        print(f"  🔍 Searching for new peer (excluding {len(exclude)} known bad source(s))...")
        new_peer = find_new_peer(token, artist, title, exclude)

        if new_peer:
            print(f"  ✅ New candidate: {new_peer['username']} / {os.path.basename(new_peer['filename'])}")
            if initiate_download(token, new_peer["username"], new_peer["filename"], new_peer["size"]):
                history["pending"][track_id] = {
                    "username":     new_peer["username"],
                    "filename":     new_peer["filename"],
                    "started_at":   datetime.now(timezone.utc).isoformat(),
                    "retry_count":  pending_info.get("retry_count", 0) + 1,
                    "candidates":   [],
                    "url":          pending_info.get("url"),
                    "artist":       artist,
                    "title":        title,
                    "tried_sources": tried_sources,
                }
                print(f"  ✅ Download re-queued from new peer")
                requeued += 1
            else:
                print(f"  ❌ Could not initiate download — marking as failed")
                history["failed"].add(track_id)
                no_peer += 1
        else:
            print(f"  ❌ No new peer found — marking as failed")
            print(f"     (sync_music.py will retry this track on its next run)")
            history["failed"].add(track_id)
            no_peer += 1

    # 7. Save updated history
    save_history(history)

    print(f"\n{'='*60}")
    print(f"Done. Processed {len(to_recover)} track(s):")
    print(f"  ✅ Re-queued with new peer : {requeued}")
    print(f"  ❌ No peer found (failed)  : {no_peer}")
    if requeued:
        print("\n  Monitor progress with sync_music.py or check slskd directly.")
    if no_peer:
        print("\n  Failed tracks will be retried automatically on the next sync_music.py run.")


if __name__ == "__main__":
    main()
