import os
from pathlib import Path
from dotenv import load_dotenv

# Get the directory of config.py and load .env from it
base_dir = Path(__file__).resolve().parent
env_path = base_dir / ".env"

load_dotenv(dotenv_path=env_path)

# Helper to get boolean values
def get_bool(key, default):
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")

# Helper to get integer values
def get_int(key, default):
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default

# Navidrome Configuration
NAVIDROME_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533")
NAVIDROME_USER = os.getenv("NAVIDROME_USER", "admin")
NAVIDROME_PASS = os.getenv("NAVIDROME_PASS")

# ListenBrainz Configuration
LISTENBRAINZ_TOKEN = os.getenv("LISTENBRAINZ_TOKEN")
LISTENBRAINZ_USER = os.getenv("LISTENBRAINZ_USER")

# SLSKD Configuration
SLSKD_URL = os.getenv("SLSKD_URL", "http://localhost:5030")
SLSKD_USERNAME = os.getenv("SLSKD_USERNAME", "admin")
SLSKD_PASSWORD = os.getenv("SLSKD_PASSWORD")

# MeTube & LiteLLM URLs
METUBE_URL = os.getenv("METUBE_URL", "http://localhost:8081")
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")

# OpenRouter Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

fallback_models_raw = os.getenv("FALLBACK_MODELS")
if fallback_models_raw:
    FALLBACK_MODELS = [m.strip() for m in fallback_models_raw.split(",") if m.strip()]
else:
    FALLBACK_MODELS = [
        "openrouter/owl-alpha",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-120b:free",
        "poolside/laguna-m.1:free",
        "z-ai/glm-4.5-air:free",
        "openrouter/free",
    ]

if OPENROUTER_MODEL and OPENROUTER_MODEL not in FALLBACK_MODELS:
    FALLBACK_MODELS = [OPENROUTER_MODEL] + FALLBACK_MODELS

# Spotify Credentials
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# PLAYLIST_URLS (parsed from comma-separated string)
urls_raw = os.getenv("PLAYLIST_URLS")
if urls_raw:
    PLAYLIST_URLS = [u.strip() for u in urls_raw.split(",") if u.strip()]
else:
    PLAYLIST_URLS = [
        "https://www.youtube.com/playlist?list=PLFS0A3AYl_QzBCoMuLQAaioq9FhaKeCFN",
        "https://music.yandex.com/users/gaylord24/playlists/1013?ref_id=900C6D28-04B5-4BC5-A667-D947FEDFD0A8&utm_medium=copy_link",
        "https://open.spotify.com/playlist/4I2U62HaUvxTlljOXj2d7g?si=RDm-INhvTRKur5uqT85Ppw",
    ]

# Paths
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/home/cif/homelab/data/music")
MUSIC_ROOT = os.getenv("MUSIC_ROOT", DOWNLOAD_DIR)
MUSIC_ROOT_HOST = os.getenv("MUSIC_ROOT_HOST", DOWNLOAD_DIR)
MUSIC_ROOT_CONTAINER = os.getenv("MUSIC_ROOT_CONTAINER", "/music")
TRASH_DIR = os.getenv("TRASH_DIR", "/home/cif/homelab/data/music/.trash")
TRASH_DAYS = get_int("TRASH_DAYS", 7)

# Configurations / State Paths
HISTORY_FILE = os.getenv("HISTORY_FILE", "/home/cif/homelab/config/sync_music/sync_history.json")
STATE_FILE = os.getenv("STATE_FILE", "/home/cif/homelab/config/sync_music/discovery_state.json")
SYNC_QUEUE_FILE = os.getenv("SYNC_QUEUE_FILE", "/home/cif/homelab/config/sync_music/download_queue.json")

# Log Paths
SYNC_LOG_FILE = os.getenv("SYNC_LOG_FILE", "/home/cif/homelab/config/sync_music/sync_music.log")
DISCOVERY_LOG_FILE = os.getenv("DISCOVERY_LOG_FILE", "/home/cif/homelab/config/sync_music/discovery.log")
DELETE_DAEMON_LOG_FILE = os.getenv("DELETE_DAEMON_LOG_FILE", "/home/cif/homelab/config/sync_music/delete_daemon.log")

# Tuning Parameters
TOLERANCE_SEC = get_int("TOLERANCE_SEC", 12)
SEARCH_TIMEOUT = get_int("SEARCH_TIMEOUT", 90)
QUEUE_TIMEOUT_HOURS = get_int("QUEUE_TIMEOUT_HOURS", 4)
MAX_RETRIES = get_int("MAX_RETRIES", 3)
LLM_SHORT_TITLE_WORDS = get_int("LLM_SHORT_TITLE_WORDS", 3)

SUBMIT_LISTENS_COUNT = get_int("SUBMIT_LISTENS_COUNT", 200)
WEEKLY_COUNT = get_int("WEEKLY_COUNT", 20)
MONTHLY_COUNT = get_int("MONTHLY_COUNT", 40)
WEEKLY_NAME = os.getenv("WEEKLY_NAME", "🔮 Discover Weekly")
MONTHLY_NAME = os.getenv("MONTHLY_NAME", "🌙 Discover Monthly")

DELETE_PLAYLIST = os.getenv("DELETE_PLAYLIST", "🗑 Delete Queue")
PROTECT_STARRED = get_bool("PROTECT_STARRED", True)
POLL_INTERVAL = get_int("POLL_INTERVAL", 300)
