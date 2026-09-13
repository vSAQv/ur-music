# ur-music synchronizer

The synchronizer reconciles public YouTube, Spotify, and Yandex Music playlists with the shared music directory. It submits at most one managed slskd transfer per run and does not cancel transfers that are absent from `sync_history.json`.

## Safety model

- A transfer submission remains `pending` until a local audio file passes artist, title, version, and duration checks.
- Remote Soulseek paths are split on both `/` and `\\`; album folders cannot satisfy title matching.
- FLAC candidates are sorted by bitrate first, then bit depth, sample rate, lyrics, and file size.
- Failed or timed-out managed transfers retry after `RETRY_DELAY_SECONDS` with a different peer.
- OpenRouter is optional search normalization only. It cannot approve a download.
- MeTube HTTP success is recorded as pending until the output appears and passes the same identity checks.
- `Delete Queue` is a user-facing Navidrome playlist. The deletion daemon creates it if missing, scans it periodically, moves validated files to trash, removes processed entries, and starts a Navidrome rescan.
- Explicit audio tags take precedence over filenames: artist/title mismatches and unexpected version modifiers such as `Remix` are rejected.
- A successful slskd transfer gets a short local settle window before retry logic runs, preventing duplicate files while the completed output is still being flushed.

## Local validation

Run the tests inside the project development environment:

```sh
devenv shell -- python -m unittest discover -s tests -v
```

## Kubernetes deployment

`homelab-k8s/apps/custom/ur-music.yaml` contains:

- An hourly serialized synchronization CronJob.
- A six-hour ListenBrainz discovery CronJob, suspended until the synchronizer is validated.
- A Navidrome deletion Deployment, scaled to zero until path mapping is validated.
- A public playlist `ConfigMap`; credentials remain in `homelab-secrets`.

`NAVIDROME_URL` is plain service configuration. `NAVIDROME_USER`, `NAVIDROME_PASS`, `LISTENBRAINZ_TOKEN`, and `LISTENBRAINZ_USER` are read from the existing SOPS-backed `homelab-secrets`. `OPENROUTER_API_KEY` remains optional and is never used as a download acceptance gate.

All workloads mount `/home/cif/homelab/config/sync_music` for durable state. The synchronizer and deletion workload also mount `/home/cif/homelab/data/music` for the shared library. The synchronizer does not alter the existing slskd Deployment or cancel unmanaged transfers.

The workloads use a root init container only to set ownership on the shared state directory; application containers continue running as UID `1000:100`.

ListenBrainz recommendations require real scrobbles from a client that submits playback events. The weekly Discovery playlist uses listens from the last seven days and the monthly playlist uses listens from the last thirty days as artist seeds for ListenBrainz radio recommendations. Previously heard recordings are excluded from the corresponding window. If the window has insufficient MBIDs, published ListenBrainz recommendation playlists and then collaborative-filtering recommendations are used as fallbacks.

`recover_stuck.py` remains a manual recovery tool and is not invoked by the CronJob. The deployed synchronizer automatically retries managed transfers tracked in `sync_history.json`; it deliberately does not cancel or infer ownership of unknown existing slskd transfers. The historical approximately 20 unmanaged transfers therefore require an explicit, reviewed recovery operation rather than automatic deployment.
