# Use the official lightweight Python 3.11 image (based on Debian Bookworm)
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="Voicebox"
LABEL org.opencontainers.image.description="Turn EPUB books into EPUB3 Media Overlay audiobooks, manually or automatically from a watch folder."
LABEL org.opencontainers.image.source="https://github.com/adman234/voicebox"
LABEL org.opencontainers.image.licenses="MIT"

# Prevent Python from buffering stdout/stderr to ensure real-time logs in Docker
ENV PYTHONUNBUFFERED=1

# Container paths. Mount these from the host; the app creates them if missing.
# Logs, models and cache are resolved by the entrypoint: an explicit variable
# wins, then a mapped /logs, /models or /cache, else a folder under /config.
ENV VOICEBOX_INGEST_DIR=/ingest \
    VOICEBOX_OUTPUT_DIR=/output \
    VOICEBOX_CONFIG_DIR=/config

# Unraid conventions: 99 = nobody, 100 = users.
ENV PUID=99 \
    PGID=100 \
    UMASK=022

WORKDIR /app

# Install system dependencies
# - ffmpeg, for audio processing
# - tini, as init process (PID 1)
# - gosu, to drop from root to PUID/PGID at startup
# - espeak-ng, so Kokoro can pronounce words outside its dictionary instead of
#   skipping them
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tini gosu espeak-ng && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt first to leverage Docker layer caching
COPY requirements.txt ./

# Install Python dependencies
# Note: On amd64 architecture, this step pulls in NVIDIA CUDA libraries (~4.1GB in ./nvidia),
# which significantly increases the image size compared to arm64.
RUN pip install --no-cache-dir -r requirements.txt

# Kokoro's English G2P (misaki) calls spacy.cli.download() the first time it
# runs if this model is missing, which shells out to `pip install` and writes
# into site-packages. That fails once the container drops to a non-root user,
# so install the model at build time while we are still root.
RUN python3 -m spacy download en_core_web_sm

# Copy the entire project into the container's WORKDIR
COPY . .

# /logs, /models and /cache exist only as mount points: the entrypoint uses
# them when they are actually mapped, and otherwise keeps everything in /config.
RUN chmod +x /app/docker/entrypoint.sh && \
    mkdir -p /ingest/processed /ingest/failed /output /config /logs /models /cache

# Declare that the container will listen on port 7860 at runtime
EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=5)" || exit 1

# tini as PID 1 to reap zombies and forward signals, then the entrypoint script
# that sets ownership and drops privileges before running the command below.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

# Define the default command to run when the container starts
CMD [ "python3", "web_gui.py", "--host", "0.0.0.0", "--port", "7860" ]
