# Voicebox

Voicebox turns EPUB books into audiobooks without you having to watch it. Drop an
`.epub` into a watch folder and it comes out as an M4B, a folder of MP3s, or a
read-along EPUB. It runs as a Docker container and has an Unraid template.

It is a fork of [audible-epub3-maker](https://github.com/funway/audible-epub3-maker)
by [funway](https://github.com/funway). The EPUB parsing, sentence alignment and
TTS engine support are funway's work. This fork adds the automation and the
container around it. It is not intended to be merged back upstream.

## What is different from upstream

- **Watch folder.** Books dropped into `/ingest` are queued and converted one at a
  time. Finished sources move to `processed/`, failures to `failed/`.
- **Settings and Automation tabs** in the web GUI. Settings holds the defaults used
  for automatic conversions and is saved in `/config/settings.json`. Automation shows
  the queue and recent jobs.
- **Audiobook output.** Besides upstream's EPUB 3 Media Overlays read-along output,
  it can write an M4B with chapters and cover art (the default) or a tagged MP3
  folder laid out for Audiobookshelf. `scripts/epub_to_audiobook.py` pulls the
  audio back out of an EPUB that was already converted.
- **Partial runs resume.** If some chapters fail, the run exits with code 3 and keeps
  the hidden `.<book>_tmp` folder. Running the same book again with the same voice
  settings only generates the missing chapters.
- **Docker image on GHCR**, with PUID/PGID/UMASK, everything persisted under
  `/config`, the spaCy model baked in, and an Unraid template.
- **Kokoro performance fixes.** The pipeline is built once per worker instead of
  once per chapter, and the parent process no longer holds its own copy of the model.
- A `--title_suffix` option, progress shown as a percentage, and conversion errors
  written to the log instead of being lost.

## Running it

Images are published to `ghcr.io/adman234/voicebox` on every push to `main`
(`:latest`) and on version tags. The image is linux/amd64 only and roughly 3.5 GB,
mostly the CUDA libraries that torch pulls in for Kokoro.

```bash
docker run -d \
    --name voicebox \
    -p 7860:7860 \
    -v ./ingest:/ingest \
    -v ./output:/output \
    -v ./config:/config \
    -e PUID=99 -e PGID=100 \
    ghcr.io/adman234/voicebox:latest
```

Then open the web GUI on port 7860. A compose file is in
[`docker-compose.example.yml`](docker-compose.example.yml).

### Volumes

| Container path | What it holds |
|----------------|---------------|
| `/ingest` | Watch folder. `processed/` and `failed/` are created inside it. |
| `/output` | Generated audiobooks. |
| `/config` | `settings.json`, plus `logs/`, `huggingface/` (the Kokoro model) and `cache/`. Keep it on persistent storage. |
| `/logs`, `/models`, `/cache` | Optional. Map any of these to move that folder out of `/config`. |

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUID` / `PGID` | `99` / `100` | Owner of generated files. The defaults are Unraid's `nobody:users`. |
| `UMASK` | `022` | File mode mask for generated files. |
| `AZURE_TTS_KEY` / `AZURE_TTS_REGION` | empty | Only needed for the Azure engine. Kokoro runs offline. |
| `VOICEBOX_LOG_DIR` | `/config/logs` | Where logs are written. |

### Unraid

1. Copy [`unraid/voicebox.xml`](unraid/voicebox.xml) to
   `/boot/config/plugins/dockerMan/templates-user/my-voicebox.xml`, then use
   Docker > Add Container > Template: my-voicebox.
2. Point Ingest at a share you can drop books into, Output at your audiobook
   library, and Config at `/mnt/user/appdata/voicebox`.
3. Leave Extra Parameters empty unless you are using an NVIDIA GPU (below).

### NVIDIA GPU

Kokoro uses CUDA on its own when torch can see a GPU. On Unraid:

1. Install the Nvidia Driver plugin and reboot.
2. Set Extra Parameters to `--runtime=nvidia`, `NVIDIA_VISIBLE_DEVICES` to the GPU
   UUID from `nvidia-smi -L`, and `NVIDIA_DRIVER_CAPABILITIES` to `compute,utility`.
3. Lower Max Workers to 1 or 2. Each worker loads its own copy of the model into
   VRAM and they share one GPU, so more workers cost memory without going faster.

Each worker logs `device=cuda` or `device=cpu` when its model loads. Only the TTS
step is accelerated; phonemisation, the M4B encode and EPUB assembly stay on the CPU.

Kokoro on CPU can use several GB of RAM, so watch memory on small servers.

## Output formats

Choose any combination in the Convert tab, or in Settings for automatic runs.

| Format | What you get |
|--------|--------------|
| M4B audiobook (default) | One `.m4b` with chapter markers and cover art. |
| MP3 folder | `<output>/<Author>/<Title>/` with one tagged MP3 per chapter, `cover.jpg` and `metadata.json`. |
| EPUB 3 read-along | The original upstream output: an EPUB with embedded audio and SMIL overlays, for readers such as Thorium. |

## Command line

The upstream CLI still works:

```bash
pip install -r requirements.txt
python main.py book.epub --tts_engine kokoro -d ./out
python web_gui.py --host 0.0.0.0 --port 7860
```

Run `python main.py --help` for all options. For Azure, set `AZURE_TTS_KEY` and
`AZURE_TTS_REGION` in the environment or a `.env` file.

## License

MIT, as upstream.
