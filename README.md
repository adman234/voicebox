# 🎧 Voicebox

Generate audiobooks from plain EPUB files in **EPUB3 Media Overlays** format using high-quality TTS (Text-to-Speech) engines like **Azure** and **Kokoro**, with a **Web GUI** and an **automatic watch folder**.

> Voicebox is a fork of [funway/audible-epub3-maker](https://github.com/funway/audible-epub3-maker) that adds hands-off batch conversion and a Docker image built for Unraid.

You can read or listen to the generated EPUB using any ebook reader that supports EPUB 3 Media Overlays, such as Thorium Reader. The generated MP3 files can also be played with any standard audio player.

---

## ✨ Features

- Convert plain EPUB books into audiobooks compliant with **[EPUB 3 Media Overlays](https://www.w3.org/TR/epub/#sec-media-overlays)** specification.
- Supports TTS engines:
  - [Azure TTS](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-text-to-speech) (high-quality cloud service)
  - [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (offline open-source model, currently supports English text alignment only)
- Automatic sentence segmentation and force alignment
- Parallel multi-process generation
- Gradio-based Web GUI for easy interaction without command line
- **Automation queue**: drop EPUB files into an ingest folder and they are converted on their own, one at a time
- **Settings tab**: pick the defaults every automatic conversion uses, saved across restarts
- Docker-ready architecture for easy deployment, with an Unraid template and PUID/PGID support

---

## 🛠 Installation

### ⚙️ From Source
#### 1. git clone & pip install
```bash
git clone https://github.com/adman234/voicebox.git
cd voicebox
pip install -r requirements.txt
```

#### 2. TTS Engine Configuration

Depending on the engine you plan to use, follow the steps below:

- **Azure**:
  - You must configure the following two environment variables:
    ```bash
    AZURE_TTS_KEY=your_azure_speech_key
    AZURE_TTS_REGION=your_speech_region
    ```
  - You can define them:
    - In a `.env` file in the project root (recommended)
    - Or `export` them manually in your shell or `.bashrc` / `.zshrc` file:
      ```bash
      export AZURE_TTS_KEY=your_azure_speech_key
      export AZURE_TTS_REGION=your_speech_region
      ```
  - [How to get Microsoft Azure Text-to-Speech API key](https://docs.merkulov.design/how-to-get-microsoft-azure-tts-api-key/)

- **Kokoro**:
  - No environment configuration is required.
  - The model file will automatically download on first use.


### 🐳 From Docker

Images are published to the GitHub Container Registry on every push to `main`:

- `ghcr.io/adman234/voicebox:latest`
- `ghcr.io/adman234/voicebox:v1.2.3` for tagged releases

The image includes all dependencies and runs the Web GUI by default (linux/amd64 only). It is roughly
3.5 GB compressed — most of that is the NVIDIA CUDA libraries that `torch` pulls in for Kokoro.

#### Volumes

| Container path | What it holds |
|----------------|---------------|
| `/ingest`      | Watch folder. Drop `.epub` files here for automatic conversion. Voicebox creates `processed/` and `failed/` inside it. |
| `/output`      | Generated audiobooks. |
| `/config`      | `settings.json` (the automation defaults) and the downloaded Kokoro model. Keep this on persistent storage. |

#### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUID` / `PGID` | `99` / `100` | Ownership of generated files. The defaults are Unraid's `nobody:users`. |
| `UMASK` | `022` | File mode mask for generated files. |
| `AZURE_TTS_KEY` / `AZURE_TTS_REGION` | empty | Only needed for the Azure engine. Kokoro runs offline. |

#### Using docker-compose

A sample configuration file `docker-compose.example.yml` is included in the repository:

```yaml
services:
  voicebox:
    image: ghcr.io/adman234/voicebox:latest
    container_name: voicebox
    ports:
      - "7860:7860"
    volumes:
      - ./ingest:/ingest
      - ./output:/output
      - ./config:/config
    environment:
      - PUID=99
      - PGID=100
      - UMASK=022
      - AZURE_TTS_KEY=your_azure_speech_key
      - AZURE_TTS_REGION=your_speech_region
    restart: unless-stopped
```

#### Using docker CLI

```bash
docker pull ghcr.io/adman234/voicebox:latest

docker run -d \
    --name voicebox \
    -p 7860:7860 \
    -v ./ingest:/ingest \
    -v ./output:/output \
    -v ./config:/config \
    -e PUID=99 -e PGID=100 \
    ghcr.io/adman234/voicebox:latest
```

### 🧡 On Unraid

An Unraid template is included at [`unraid/voicebox.xml`](unraid/voicebox.xml).

1. Copy it to `/boot/config/plugins/dockerMan/templates-user/my-voicebox.xml` on your server, then
   **Docker → Add Container → Template: my-voicebox**. Or use **Add Container** and fill in the
   repository `ghcr.io/adman234/voicebox:latest` with the three volumes above.
2. Point **Ingest** at a share you can drop books into (e.g. `/mnt/user/books/ingest`), **Output** at
   where the audiobooks should land, and **Config** at `/mnt/user/appdata/voicebox`.
3. Start the container and open the WebUI on port 7860.

The published package is **public**, so Unraid pulls it without any registry credentials. If you ever
switch it to private (package page → Package settings → Change visibility), you will need to run
`docker login ghcr.io` on the server before the pull will work.

### 💡 Notes

- **Using Azure TTS?** Make sure you set the `AZURE_TTS_KEY` and `AZURE_TTS_REGION` environment variables before starting the container.

- **Kokoro model files** are baked in or cached under `/config`: the spaCy English model ships inside the
  image, and the Kokoro weights download to `/config/huggingface` on first use. Keep the `/config` volume
  so neither is fetched again. The container runs as `PUID`/`PGID` and cannot write to site-packages, so
  nothing is installed at runtime.

- **Using Kokoro TTS?** Keep an eye on your system's memory usage — the model runs locally and can consume several GB of RAM. On low-memory systems, this may cause OOM (out-of-memory) errors. The model downloads on first use into `/config`, so keep that volume to avoid re-downloading it.

---

## 🚀 Usage

### 🖥️ CLI

```bash
python main.py <input_file.epub> [options]
```

#### Required:
- `input_file`: The path to the source EPUB file.

#### Optional arguments:

| Option                | Description                                      | Default                     |
|-----------------------|--------------------------------------------------|-----------------------------|
| `-d`, `--output_dir`  | Output directory                                 | `<input_file_stem>_audible` |
| `-o`, `--output_filename` | Generated EPUB filename                      | original input filename     |
| `--title_suffix`      | Suffix appended to the OPF `dc:title`            | empty                       |
| `--log_level`         | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) | INFO                     |
| `--tts_engine`        | TTS engine (`azure` or `kokoro`)                 | azure                       |
| `--tts_lang`          | Language code                                    | azure → en-US; <br/>kokoro → first supported |
| `--tts_voice`         | Voice name                                       | azure → en-US-AvaMultilingualNeural; <br/>kokoro → first voice for language |
| `--tts_speed`         | Playback speed (e.g., 1.0 = normal)              | 1.0                         |
| `--tts_chunk_len`     | Max chars per TTS chunk                          | auto                        |
| `--newline_mode`      | How to detect paragraph breaks from newlines (`none`, `single`, `multi`) | multi |
| `-m`, `--max_workers` | Number of worker processes                       | 3                           |
| `--align_threshold`   | Force alignment fuzzy match threshold (0–100)    | 95.0                        |
| `-f`, `--force`       | Force all prompts (non-interactive mode)         | false                       |
| `--cleanup`           | Remove temp files (.mp3) after generation        | false                       |

#### Example

```bash
python main.py mybook.epub \
    --tts_engine azure \
    --tts_lang zh-CN \
    --tts_voice zh-CN-XiaoxiaoNeural \
    -d ./output_dir \
    -m 4 \
    --log_level DEBUG
```

### 🌐 Web GUI

```bash
python web_gui.py
```
#### Optional arguments:

| Argument   | Description                         | Default       |
| ---------- | ----------------------------------- | ------------- |
| `--host`   | Host to bind the Gradio web server  | `127.0.0.1`   |
| `--port`   | Port to bind the Gradio web server  | `7860`        |

Then open your browser and interact with the friendly interface!

The interface has three tabs:

- **🎬 Convert** — pick one EPUB, adjust the options, press Run and watch the log. Its fields are
  pre-filled from the saved defaults each time the page loads.
- **🤖 Automation** — live view of the ingest queue: what is converting now, what is waiting, and how
  recent jobs turned out. Buttons to scan immediately, cancel the running job, or clear the queue/history.
- **⚙️ Settings** — the defaults used for automatic conversions.

![Web GUI Screenshot](screenshot.png)

---

## 🤖 Automation (watch folder)

Copy `.epub` files into the ingest folder (`/ingest` in Docker) and Voicebox converts them without
being asked:

1. The folder is scanned every 15 seconds by default. Sub-folders are scanned too, and their structure
   is mirrored into the output directory.
2. A file is only queued once its size and timestamp have stopped changing, so a book still being
   copied onto the share is never picked up half-written.
3. Jobs run **one at a time**, using the options from the **Settings** tab. A conversion started by hand
   from the Convert tab takes priority; the queue waits for it to finish.
4. On success the source `.epub` is moved to `ingest/processed/`; on failure it moves to `ingest/failed/`
   so it is easy to see what needs attention and nothing is ever converted twice.

Settings are stored in `settings.json` inside the config folder, so they survive container updates.
Automation can be paused at any time with the checkbox in the Settings tab.

---

## 💾 Output

- `*.mp3`: Generated audio for each chapter
- `*.epub`: A new EPUB file with embedded mp3 audio and synchronized smil overlays

---

## 📄 License

This project is licensed under the MIT License.

---

## 🚗 TODO
- Add CPU/GPU selection for offline TTS models (note: pip install on amd64 arch defaults to pull a 4GB NVIDIA library (\"▔□▔) )
- Support more TTS models
- Implement voice preview and cost estimation for commercial models
- Integrate WhisperX for audio-text alignment in TTS models without native word boundary output
  
---
