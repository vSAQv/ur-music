import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import sync_music
import delete_daemon
import listenbrainz_discovery


class MatchingTests(unittest.TestCase):
    def test_remote_basename_handles_windows_separator(self):
        self.assertEqual(
            sync_music.remote_basename(r"Music\Linkin Park\01 - Forgotten.flac"),
            "01 - Forgotten.flac",
        )

    def test_album_folder_cannot_supply_title_words(self):
        path = r"Music\Linkin Park\In The End\01 - Good People.flac"
        self.assertFalse(sync_music.is_valid_match(path, "Poor Man's Poison", "Good People", 0, 0))

    def test_short_title_rejects_longer_wrong_title(self):
        path = r"Music\Koji Kondo\Super Mario Odyssey\01 - Forgotten Isle.flac"
        self.assertFalse(sync_music.is_valid_match(path, "Linkin Park", "Forgotten", 0, 0))

    def test_valid_candidate_uses_artist_directory(self):
        path = r"Music\Linkin Park\Minutes to Midnight\01 - Forgotten.flac"
        self.assertTrue(sync_music.is_valid_match(path, "Linkin Park", "Forgotten", 0, 0))

    def test_duration_is_checked(self):
        path = r"Music\Linkin Park\01 - Forgotten.flac"
        self.assertFalse(sync_music.is_valid_match(path, "Linkin Park", "Forgotten", 180, 220))

    def test_quality_is_primary_and_lyrics_are_secondary(self):
        responses = [
            {
                "username": "peer",
                "queueLength": 0,
                "uploadSpeed": 100,
                "files": [
                    {
                        "filename": r"Linkin Park\01 - Forgotten.flac",
                        "length": 180,
                        "size": 10,
                        "bitDepth": 16,
                        "sampleRate": 44100,
                        "bitRate": 900,
                    },
                    {
                        "filename": r"Linkin Park\01 - Forgotten.lrc",
                        "size": 1,
                    },
                ],
            },
            {
                "username": "better-peer",
                "queueLength": 100,
                "uploadSpeed": 1,
                "files": [
                    {
                        "filename": r"Linkin Park\01 - Forgotten.flac",
                        "length": 180,
                        "size": 20,
                        "bitDepth": 24,
                        "sampleRate": 96000,
                        "bitRate": 1400,
                    }
                ],
            },
        ]
        candidates = sync_music.collect_candidates(responses, "Linkin Park", "Forgotten", 180)
        self.assertEqual(candidates[0]["username"], "better-peer")


class StateAndApiTests(unittest.TestCase):
    def test_download_queue_round_trip_is_atomic_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = os.path.join(directory, "queue.json")
            queue = [{"artist": "Linkin Park", "title": "Forgotten"}]
            with patch.object(sync_music, "SYNC_QUEUE_FILE", queue_path):
                sync_music.save_download_queue(queue)
                self.assertEqual(sync_music.load_download_queue(), queue)

    def test_recommendation_queue_does_not_overwrite_malformed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = os.path.join(directory, "queue.json")
            with open(queue_path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            with patch.object(listenbrainz_discovery, "SYNC_QUEUE_FILE", queue_path):
                listenbrainz_discovery.queue_for_download(
                    [{"artist": "Linkin Park", "title": "Forgotten"}]
                )
            with open(queue_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "not json")

    def test_navidrome_search_rejects_partial_match(self):
        client = listenbrainz_discovery.NavidromeClient("http://navidrome", "user", "pass")
        with patch.object(
            client,
            "_get",
            return_value={
                "searchResult3": {
                    "song": [
                        {"id": "wrong", "artist": "Poor Man's Poison", "title": "Good People"}
                    ]
                }
            },
        ):
            self.assertIsNone(client.search_track("Linkin Park", "Good"))

    def test_listenbrainz_seed_extracts_artist_and_recording_mbids(self):
        listen = {
            "track_metadata": {
                "artist_name": "Linkin Park",
                "track_name": "Forgotten",
                "additional_info": {
                    "artist_mbids": ["artist-mbid"],
                    "recording_mbid": "recording-mbid",
                },
            }
        }
        seed = listenbrainz_discovery._listen_seed(listen)
        self.assertEqual(seed["artist_mbid"], "artist-mbid")
        self.assertEqual(seed["recording_mbid"], "recording-mbid")

    def test_period_recommendations_use_recent_artist_radio_and_exclude_listens(self):
        listens = [
            {
                "track_metadata": {
                    "artist_name": "Linkin Park",
                    "track_name": "Forgotten",
                    "additional_info": {
                        "artist_mbids": ["artist-mbid"],
                        "recording_mbid": "already-heard",
                    },
                }
            }
        ]
        with patch.object(listenbrainz_discovery, "_fetch_listens_since", return_value=listens):
            with patch.object(
                listenbrainz_discovery,
                "_fetch_radio_recordings",
                return_value=[
                    {"recording_mbid": "already-heard"},
                    {"recording_mbid": "new-recording"},
                ],
            ):
                with patch.object(
                    listenbrainz_discovery,
                    "_lookup_mbid",
                    return_value={"artist": "Muse", "title": "New Song"},
                ):
                    with patch.object(listenbrainz_discovery.time, "sleep"):
                        recommendations = listenbrainz_discovery._fetch_period_recommendations(7, 1)
        self.assertEqual(recommendations, [{"artist": "Muse", "title": "New Song"}])

    def test_delete_path_cannot_escape_music_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(delete_daemon, "MUSIC_ROOT_HOST", directory), patch.object(
                delete_daemon, "MUSIC_ROOT_CONTAINER", "/music"
            ):
                self.assertEqual(
                    delete_daemon.container_path_to_host("/music/Artist/track.flac"),
                    os.path.join(directory, "Artist", "track.flac"),
                )
                self.assertIsNone(
                    delete_daemon.container_path_to_host("/music/../etc/passwd")
                )
                self.assertIsNone(delete_daemon.container_path_to_host("/etc/passwd"))

    def test_delete_keeps_queue_entry_when_song_metadata_is_unavailable(self):
        client = Mock()
        client.get_or_create_playlist.return_value = "delete-queue"
        client.get_playlist_songs.return_value = [{"id": "missing", "artist": "A", "title": "B"}]
        client.get_starred_ids.return_value = set()
        client.get_song.return_value = None
        delete_daemon.process_delete_queue(client)
        client.remove_from_playlist.assert_not_called()

    def test_transfer_api_error_is_not_an_empty_queue(self):
        response = Mock(ok=False, status_code=503)
        with patch.object(sync_music.requests, "get", return_value=response):
            with self.assertRaises(sync_music.SlskdUnavailable):
                sync_music.get_all_transfers("token")

    def test_retry_uses_a_different_candidate(self):
        track_id = "artist - title"
        candidate = {
            "username": "peer-two",
            "filename": r"Artist\01 - Title.flac",
            "size": 10,
            "quality": (1000, 16, 44100, 0, 10),
        }
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        history = {
            "version": 3,
            "completed": set(),
            "failed": set(),
            "pending": {
                track_id: {
                    "artist": "Artist",
                    "title": "Title",
                    "duration": 180,
                    "username": "peer-one",
                    "filename": r"Artist\01 - Title.flac",
                    "transfer_id": "bad-transfer",
                    "retry_count": 0,
                    "retry_after": old_time,
                    "candidates": [candidate],
                    "tried_sources": [],
                }
            },
        }
        with patch.object(sync_music, "cancel_transfer", return_value=True):
            with patch.object(sync_music, "initiate_download", return_value=True):
                with patch.object(sync_music, "get_all_transfers", return_value=[]):
                    self.assertTrue(
                        sync_music._retry_or_fallback(
                            "token", history, track_id, history["pending"][track_id], "Errored", []
                        )
                    )
        self.assertEqual(history["pending"][track_id]["username"], "peer-two")
        self.assertEqual(history["pending"][track_id]["retry_count"], 1)

    def test_history_save_and_load_preserves_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = os.path.join(directory, "history.json")
            history = {"version": 3, "completed": {"done"}, "pending": {"pending": {"backend": "slskd"}}, "failed": set()}
            with patch.object(sync_music, "HISTORY_FILE", history_path):
                sync_music.save_history(history)
                loaded = sync_music.load_history()
            self.assertEqual(loaded["completed"], {"done"})
            self.assertEqual(loaded["pending"]["pending"]["backend"], "slskd")

    def test_spotify_uses_client_credentials_and_pagination(self):
        token_response = Mock(ok=True)
        token_response.json.return_value = {"access_token": "token"}
        page_one = Mock(ok=True)
        page_one.json.return_value = {
            "items": [
                {
                    "track": {
                        "name": "Forgotten",
                        "artists": [{"name": "Linkin Park"}],
                        "duration_ms": 180000,
                        "external_urls": {"spotify": "https://open.spotify.com/track/x"},
                    }
                }
            ],
            "next": "https://api.spotify.com/v1/playlists/p/tracks?offset=100",
        }
        page_two = Mock(ok=True)
        page_two.json.return_value = {"items": [], "next": None}
        with patch.object(sync_music.requests, "post", return_value=token_response) as post:
            with patch.object(sync_music.requests, "get", side_effect=[page_one, page_two]):
                with patch.object(sync_music, "SPOTIFY_CLIENT_ID", "id"), patch.object(
                    sync_music, "SPOTIFY_CLIENT_SECRET", "secret"
                ):
                    tracks = sync_music.fetch_spotify_playlist("https://open.spotify.com/playlist/p")
        self.assertEqual(tracks[0]["title"], "Forgotten")
        self.assertEqual(tracks[0]["duration"], 180)
        self.assertIn("grant_type", post.call_args.kwargs["data"])


if __name__ == "__main__":
    unittest.main()
