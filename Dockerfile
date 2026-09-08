# Use the official lightweight Python 3.11 image (based on Debian Bookworm)
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="Voicebox"
LABEL org.opencontainers.image.description="Turn EPUB books into EPUB3 Media Overlay audiobooks, manually or automatically from a watch folder."
LABEL org.opencontainers.image.source="https://github.com/adman234/voicebox"
LABEL org.opencontainers.image.licenses="MIT"

# Prevent Python from buffering stdout/stderr to ensure real-time logs in Docker
ENV PYTHONUNBUFFERED=1

# Container paths. Mount these from the host; the app creates them if missing.
ENV VOICEBOX_INGEST_DIR=/ingest \
    VOICEBOX_OUTPUT_DIR=/output \
    VOICEBOX_CONFIG_DIR=/config \
    HF_HOME=/config/huggingface

# Unraid conventions: 99 = nobody, 100 = users.
ENV PUID=99 \
    PGID=100 \
    UMASK=022

WORKDIR /app

# Install system dependencies
# - ffmpeg, for audio processing
# - tini, as init process (PID 1)
# - gosu, to drop from root to PUID/PGID at startup
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tini gosu && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt first to leverage Docker layer caching
COPY requirements.txt ./

# Install Python dependencies
# Note: On amd64 architecture, this step pulls in NVIDIA CUDA libraries (~4.1GB in ./nvidia),
# which significantly increases the image size compared to arm64.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project into the container's WORKDIR
COPY . .

RUN chmod +x /app/docker/entrypoint.sh && \
    mkdir -p /ingest/processed /ingest/failed /output /config /app/logs

# Declare that the container will listen on port 7860 at runtime
EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=5)" || exit 1

# tini as PID 1 to reap zombies and forward signals, then the entrypoint script
# that sets ownership and drops privileges before running the command below.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

# Define the default command to run when the container starts
CMD [ "python3", "web_gui.py", "--host", "0.0.0.0", "--port", "7860" ]
