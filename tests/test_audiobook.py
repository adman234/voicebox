"""Audiobook export: chapter naming, tagging, m4b chapter markers, layout."""
import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from audible_epub3_maker import audiobook

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


def make_mp3(path: Path, seconds: int) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-b:a", "64k", str(path)], check=True)
    return path


def probe(path: Path, entries: str, extra=()):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries,
                          "-of", "json", *extra, str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


## --------------------------------------------------------------- pure logic

def test_chapter_title_prefers_the_chapters_own_heading():
    html = "<html><body><h1> The  First   Chapter </h1><p>Words.</p></body></html>"
    assert audiobook.chapter_title(html, 0) == "The First Chapter"


def test_chapter_title_falls_back_when_there_is_no_heading():
    assert audiobook.chapter_title("<html><body><p>Just prose.</p></body></html>", 4) == "Chapter 5"
    assert audiobook.chapter_title("", 0) == "Chapter 1"


def test_sanitize_strips_characters_that_break_paths():
    assert audiobook.sanitize('A/B\\C:D*E?F"G<H>I|J') == "ABCDEFGHIJ"
    assert audiobook.sanitize("   ") == "Untitled"
    assert audiobook.sanitize("", "Unknown") == "Unknown"
    assert len(audiobook.sanitize("x" * 400)) <= 120


def test_book_dir_uses_the_audiobookshelf_layout(tmp_path):
    assert audiobook.book_dir(tmp_path, "Jane Author", "A Title") == tmp_path / "Jane Author" / "A Title"
    # A book with no author still has to land somewhere sensible.
    assert audiobook.book_dir(tmp_path, "", "A Title") == tmp_path / "Unknown Author" / "A Title"


def test_chapter_metadata_timings_run_back_to_back():
    chapters = [("One", None), ("Two", None), ("Three", None)]
    text = audiobook.build_chapter_metadata(chapters, [1000, 2000, 500], "Book", "Author")
    starts = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("START=")]
    ends = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("END=")]
    assert starts == [0, 1000, 3000]
    assert ends == [1000, 3000, 3500]


## ----------------------------------------------------------------- exports

@needs_ffmpeg
def test_mp3_folder_is_numbered_tagged_and_lossless(tmp_path):
    chapters = [("The First Chapter", make_mp3(tmp_path / "a.mp3", 2)),
                ("The Second Chapter", make_mp3(tmp_path / "b.mp3", 3))]
    dest = tmp_path / "out"

    written = audiobook.export_mp3_folder(chapters, dest, "Test Book", "Jane Author",
                                          "Kokoro TTS - af_heart", b"\xff\xd8\xff cover")

    assert [p.name for p in written] == [
        "001 - The First Chapter.mp3", "002 - The Second Chapter.mp3"]
    assert (dest / "cover.jpg").read_bytes() == b"\xff\xd8\xff cover"

    tags = probe(written[0], "format_tags")["format"]["tags"]
    assert tags["title"] == "The First Chapter"
    assert tags["album"] == "Test Book"
    assert tags["artist"] == "Jane Author"
    assert tags["track"] == "1/2"
    # Stream-copied, so the audio is untouched.
    assert audiobook.duration_ms(written[0]) == pytest.approx(2000, abs=150)


@needs_ffmpeg
def test_m4b_carries_chapter_markers_and_runs_full_length(tmp_path):
    chapters = [("Chapter One", make_mp3(tmp_path / "a.mp3", 2)),
                ("Chapter Two", make_mp3(tmp_path / "b.mp3", 3))]
    target = tmp_path / "out" / "Book.m4b"

    audiobook.export_m4b(chapters, target, "Test Book", "Jane Author",
                         "Kokoro TTS - af_heart", None, workdir=tmp_path / "work")

    assert target.is_file()
    data = probe(target, "format=duration:format_tags", ["-show_chapters"])
    assert [c["tags"]["title"] for c in data["chapters"]] == ["Chapter One", "Chapter Two"]
    assert data["format"]["tags"]["title"] == "Test Book"
    assert data["format"]["tags"]["artist"] == "Jane Author"
    # Both chapters end to end, not just the first.
    assert float(data["format"]["duration"]) == pytest.approx(5.0, abs=0.3)


def test_metadata_json_is_what_audiobookshelf_expects(tmp_path):
    target = audiobook.write_metadata_json(tmp_path / "metadata.json", "Test Book",
                                           "Jane Author", "Kokoro TTS - af_heart",
                                           "en", "urn:isbn:9780817385750")
    data = json.loads(target.read_text())
    assert data["title"] == "Test Book"
    assert data["authors"] == ["Jane Author"]
    assert data["narrators"] == ["Kokoro TTS - af_heart"]
    assert data["isbn"] == "9780817385750"


def test_metadata_json_omits_isbn_when_the_id_is_not_one(tmp_path):
    target = audiobook.write_metadata_json(tmp_path / "m.json", "T", "A", "N", "en",
                                           "urn:uuid:abc-123")
    assert "isbn" not in json.loads(target.read_text())


## ------------------------------------------------------- app integration

@needs_ffmpeg
def test_app_exports_only_the_requested_formats(tmp_path, monkeypatch):
    from audible_epub3_maker.app import App
    from audible_epub3_maker.config import settings

    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()
    make_mp3(tmp_dir / "aud0.mp3", 2)
    make_mp3(tmp_dir / "aud1.mp3", 2)

    book = types.SimpleNamespace(
        title="Test Book", author="Jane Author", language="en",
        identifier="urn:isbn:1234567890", get_cover_item=lambda: None,
    )
    monkeypatch.setattr(settings, "output_dir", tmp_path / "library")
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    monkeypatch.setattr(settings, "tts_voice", "af_heart")

    App().export_audiobook(book, ["m4b"], ["One", "Two"], [0, 1], tmp_dir)

    dest = tmp_path / "library" / "Jane Author" / "Test Book"
    assert (dest / "Test Book.m4b").is_file()
    # mp3 was not requested, so no chapter files should appear.
    assert not list(dest.glob("*.mp3"))


## --------------------------------------------- skipping needless alignment

class _StubTTS:
    """Returns audio but no word boundaries, like an engine that cannot align."""

    def __init__(self, seconds=1):
        self.seconds = seconds

    def html_to_speech(self, html_text, output_file, metadata=None):
        make_mp3(Path(output_file), self.seconds)
        return []


@needs_ffmpeg
def test_alignment_is_skipped_when_no_epub_is_wanted(tmp_path, monkeypatch):
    """Segmentation and alignment only feed the EPUB's SMIL. For audio-only
    output they are wasted work, and word boundaries are not even needed."""
    from audible_epub3_maker import worker
    from audible_epub3_maker.config import settings
    from audible_epub3_maker.utils.types import TaskPayload

    monkeypatch.setattr(worker, "create_tts_engine", lambda name: _StubTTS())
    monkeypatch.setattr(settings, "output_formats", ["m4b"])
    monkeypatch.setattr(settings, "tts_engine", "kokoro")

    payload = TaskPayload(idx=0, html_text="<html><body><h1>T</h1><p>Words.</p></body></html>",
                          audio_output_file=tmp_path / "aud0.mp3", audio_metadata={})
    result = worker.task_fn(payload)

    assert result.alignments == []
    assert result.tts_seconds > 0
    assert result.align_seconds == 0.0


@needs_ffmpeg
def test_missing_word_boundaries_still_fail_when_an_epub_is_wanted(tmp_path, monkeypatch):
    """The EPUB genuinely needs them, so this must not be silently skipped too."""
    from audible_epub3_maker import worker
    from audible_epub3_maker.config import settings
    from audible_epub3_maker.utils.types import NoWordBoundariesError, TaskPayload

    monkeypatch.setattr(worker, "create_tts_engine", lambda name: _StubTTS())
    monkeypatch.setattr(settings, "output_formats", ["epub"])

    payload = TaskPayload(idx=0, html_text="<html><body><p>Words.</p></body></html>",
                          audio_output_file=tmp_path / "aud0.mp3", audio_metadata={})
    with pytest.raises(NoWordBoundariesError):
        worker.task_fn(payload)


@needs_ffmpeg
def test_audiobook_uses_the_clean_title_not_the_suffixed_one(tmp_path, monkeypatch):
    """The suffix marks the generated EPUB; it has no place in a library."""
    from audible_epub3_maker.app import App
    from audible_epub3_maker.config import settings

    tmp_dir = tmp_path / "work"
    tmp_dir.mkdir()
    make_mp3(tmp_dir / "aud0.mp3", 1)

    book = types.SimpleNamespace(
        title="The Klan Unmasked _voicebox",   # already suffixed for the EPUB
        author="Stetson Kennedy", language="en", identifier="x",
        get_cover_item=lambda: None,
    )
    monkeypatch.setattr(settings, "output_dir", tmp_path / "library")
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    monkeypatch.setattr(settings, "tts_voice", "af_heart")
    monkeypatch.setattr(settings, "m4b_bitrate", "32k")

    App().export_audiobook(book, ["mp3"], ["One"], [0], tmp_dir,
                           title="The Klan Unmasked")

    dest = tmp_path / "library" / "Stetson Kennedy" / "The Klan Unmasked"
    assert dest.is_dir()
    assert not (tmp_path / "library" / "Stetson Kennedy" / "The Klan Unmasked _voicebox").exists()
    assert json.loads((dest / "metadata.json").read_text())["title"] == "The Klan Unmasked"
