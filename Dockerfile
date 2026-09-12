FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/music \
    HISTORY_FILE=/config/sync_history.json \
    SYNC_LOG_FILE=/config/sync_music.log \
    STATE_FILE=/config/discovery_state.json \
    SYNC_QUEUE_FILE=/config/download_queue.json \
    DISCOVERY_LOG_FILE=/config/discovery.log \
    DELETE_DAEMON_LOG_FILE=/config/delete_daemon.log \
    TRASH_DIR=/config/trash \
    MUSIC_ROOT_HOST=/music \
    MUSIC_ROOT_CONTAINER=/music

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt yt-dlp

COPY config.py sync_music.py listenbrainz_discovery.py delete_daemon.py ./
RUN mkdir -p /config /music && chown -R 1000:100 /config /music

USER 1000:100
ENTRYPOINT ["python", "/app/sync_music.py"]
