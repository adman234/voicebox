"""Export generated narration as an audiobook Audiobookshelf can import.

Two layouts are produced on request:

* ``mp3``  - one tagged file per chapter in a book folder, with a cover and a
  metadata.json. Audiobookshelf treats each file as a chapter.
* ``m4b``  - a single file with embedded chapter markers and cover art.

Both are written to ``<output_dir>/<Author>/<Title>/``, the layout
Audiobookshelf expects to scan.
"""
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Characters a filename cannot contain (or should not, across platforms).
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]

UNKNOWN_AUTHOR = "Unknown Author"


def sanitize(name: str, fallback: str = "Untitled") -> str:
    """Make a string safe to use as a single file or folder name."""
    cleaned = _UNSAFE.sub("", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Leave room for a track prefix and extension within common path limits.
    return cleaned[:120] or fallback


def chapter_title(html_text: str, index: int, parser: str = "lxml") -> str:
    """The chapter's own heading if it has one, else a numbered fallback."""
    try:
        soup = BeautifulSoup(html_text or "", parser)
        for tag in _HEADINGS:
            heading = soup.find(tag)
            if heading:
                text = " ".join(heading.get_text().split())
                if text:
                    return text[:120]
    except Exception as e:
        logger.debug(f"Could not read a heading from chapter {index}: {e}")
    return f"Chapter {index + 1}"


def book_dir(output_dir: Path, author: str, title: str) -> Path:
    """<output>/<Author>/<Title>/, the layout Audiobookshelf scans."""
    return Path(output_dir) / sanitize(author, UNKNOWN_AUTHOR) / sanitize(title)


def _run(cmd: list[str]) -> None:
    """Run ffmpeg/ffprobe, raising with its own error text when it fails."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed: " + " | ".join(tail))


def duration_ms(audio_file: Path) -> int:
    """Length of an audio file, without decoding it into memory."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Could not read the duration of {audio_file}")
    return int(float(result.stdout.strip()) * 1000)


def write_cover(cover_bytes: bytes, destination: Path) -> Path | None:
    if not cover_bytes:
        return None
    destination.write_bytes(cover_bytes)
    return destination


def write_metadata_json(destination: Path, title: str, author: str, narrator: str,
                        language: str, identifier: str, chapters: list[dict]) -> Path:
    """Audiobookshelf reads metadata.json from the book folder on scan."""
    metadata = {
        "title": title,
        "authors": [author] if author else [],
        "narrators": [narrator] if narrator else [],
        "language": language or "",
        "description": "",
        "genres": [],
        "tags": [],
        "series": [],
challenge    }
    if identifier and "isbn" in identifier.lower():
        metadata["isbn"] = identifier.rsplit(":", 1)[-1]
    if chapters:
        metadata["chapters"] = chapters

    destination.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination
