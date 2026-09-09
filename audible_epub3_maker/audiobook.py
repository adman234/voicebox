"""Export generated narration as an audiobook that Audiobookshelf can import.

Two layouts, either or both:

* ``mp3`` - one tagged file per chapter, which Audiobookshelf reads as chapters.
  The per-chapter files already exist by this point, so they are stream-copied
  rather than re-encoded: lossless and quick.
* ``m4b`` - a single file carrying chapter markers and cover art. This one is
  re-encoded to AAC, so it costs a pass over the audio.

Both are written to ``<output_dir>/<Author>/<Title>/``, the layout
Audiobookshelf expects to scan.
"""
import json
import logging
import re
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

EPUB = "epub"
MP3 = "mp3"
M4B = "m4b"
FORMATS = [EPUB, MP3, M4B]

UNKNOWN_AUTHOR = "Unknown Author"
DEFAULT_BITRATE = "64k"

# Characters that are unsafe in a file or folder name.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def sanitize(name: str, fallback: str = "Untitled") -> str:
    """Make a string safe to use as one file or folder name."""
    cleaned = _UNSAFE.sub("", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Leave room for a track prefix and extension within common path limits.
    return cleaned[:120] or fallback


def chapter_title(html_source: str, index: int) -> str:
    """A chapter's own heading, or a numbered fallback when it has none."""
    try:
        soup = BeautifulSoup(html_source or "", "lxml")
        for tag in _HEADINGS:
            heading = soup.find(tag)
            if heading:
                text = " ".join(heading.get_text().split())
                if text:
                    return text[:120]
    except Exception as e:
        logger.debug(f"No heading found for chapter {index}: {e}")
    return f"Chapter {index + 1}"


def book_dir(output_dir, author: str, title: str) -> Path:
    """<output>/<Author>/<Title>/, the layout Audiobookshelf scans."""
    return Path(output_dir) / sanitize(author, UNKNOWN_AUTHOR) / sanitize(title)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = " | ".join((result.stderr or "").strip().splitlines()[-4:])
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {tail}")
    return result


def duration_ms(audio_file) -> int:
    """Length of an audio file, read from its header rather than decoded."""
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_file)]).stdout.strip()
    if not out:
        raise RuntimeError(f"Could not read the duration of {audio_file}")
    return int(float(out) * 1000)


def write_metadata_json(destination: Path, title: str, author: str, narrator: str,
                        language: str, identifier: str) -> Path:
    """Audiobookshelf reads metadata.json from the book folder when it scans."""
    data = {
        "title": title,
        "authors": [author] if author else [],
        "narrators": [narrator] if narrator else [],
        "language": language or "",
        "description": "",
        "genres": [],
        "tags": [],
        "series": [],
    }
    if identifier and "isbn" in identifier.lower():
        data["isbn"] = identifier.rsplit(":", 1)[-1]

    destination.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination


def export_mp3_folder(chapters, destination: Path, title: str, author: str,
                      narrator: str, cover: bytes | None) -> list[Path]:
    """Write one tagged mp3 per chapter, numbered so they sort correctly."""
    destination.mkdir(parents=True, exist_ok=True)
    written = []

    for number, (name, source) in enumerate(chapters, start=1):
        target = destination / f"{number:03d} - {sanitize(name, f'Chapter {number}')}.mp3"
        # Copy the audio stream and rewrite only the tags: no quality loss.
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
              "-c", "copy", "-id3v2_version", "3",
              "-metadata", f"title={name}",
              "-metadata", f"album={title}",
              "-metadata", f"artist={author or UNKNOWN_AUTHOR}",
              "-metadata", f"album_artist={author or UNKNOWN_AUTHOR}",
              "-metadata", f"composer={narrator}",
              "-metadata", f"track={number}/{len(chapters)}",
              str(target)])
        written.append(target)

    if cover:
        (destination / "cover.jpg").write_bytes(cover)
    return written


def build_chapter_metadata(chapters, durations, title: str, author: str) -> str:
    """FFMETADATA naming the book and where each chapter starts and ends."""
    lines = [";FFMETADATA1", f"title={title}", f"artist={author or UNKNOWN_AUTHOR}",
             f"album={title}"]
    start = 0
    for (name, _), length in zip(chapters, durations):
        end = start + length
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={name}"]
        start = end
    return "\n".join(lines) + "\n"


def export_m4b(chapters, destination: Path, title: str, author: str, narrator: str,
               cover: bytes | None, workdir: Path, bitrate: str = DEFAULT_BITRATE) -> Path:
    """Concatenate the chapters into one m4b with markers and cover art."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    listing = workdir / "concat.txt"
    listing.write_text("".join(
        "file '%s'\n" % str(path).replace("'", r"'\''") for _, path in chapters
    ), encoding="utf-8")

    metadata = workdir / "chapters.txt"
    metadata.write_text(
        build_chapter_metadata(chapters, [duration_ms(p) for _, p in chapters], title, author),
        encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-i", str(metadata)]

    cover_file = None
    if cover:
        cover_file = workdir / "cover.jpg"
        cover_file.write_bytes(cover)
        cmd += ["-i", str(cover_file)]

    cmd += ["-map", "0:a", "-map_metadata", "1"]
    if cover_file:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += ["-c:a", "aac", "-b:a", bitrate,
            "-metadata", f"title={title}",
            "-metadata", f"artist={author or UNKNOWN_AUTHOR}",
            "-metadata", f"album={title}",
            "-metadata", f"composer={narrator}",
            "-movflags", "+faststart", str(destination)]
    _run(cmd)
    return destination
