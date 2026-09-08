import json
import sys
import time

import pytest

from audible_epub3_maker.automation import ingest as ingest_mod
from audible_epub3_maker.automation.ingest import IngestQueue
from audible_epub3_maker.automation.settings_store import DEFAULTS, SettingsStore, coerce


## ----------------------------------------------------------------- settings

def test_coerce_clamps_out_of_range_values():
    out = coerce({"max_workers": "99", "align_threshold": 10, "tts_speed": 5.0, "scan_interval": 1})
    assert out["max_workers"] == 16
    assert out["align_threshold"] == 80.0
    assert out["tts_speed"] == 2.0
    assert out["scan_interval"] == 5


def test_coerce_drops_unknown_keys_and_keeps_defaults():
    out = coerce({"not_a_setting": "x"})
    assert "not_a_setting" not in out
    assert out == DEFAULTS


def test_coerce_falls_back_on_unusable_values():
    out = coerce({"tts_speed": "fast", "log_level": "chatty", "cleanup": "yes"})
    assert out["tts_speed"] == DEFAULTS["tts_speed"]
    assert out["log_level"] == "INFO"
    assert out["cleanup"] is True


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)

    saved = store.save({"tts_engine": "Azure", "tts_voice": "en-US-AvaMultilingualNeural",
                        "automation_enabled": False})
    assert saved["tts_engine"] == "azure"
    assert saved["automation_enabled"] is False

    assert json.loads(path.read_text())["tts_voice"] == "en-US-AvaMultilingualNeural"
    assert SettingsStore(path).all() == saved


def test_unreadable_settings_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json")
    assert SettingsStore(path).all() == DEFAULTS


## -------------------------------------------------------------------- queue

@pytest.fixture
def queue(tmp_path, monkeypatch):
    """An ingest queue whose 'conversion' is a subprocess that fails on 'bad' files."""
    def fake_build_command(input_file, **kwargs):
        code = "import sys; sys.exit(1 if 'bad' in sys.argv[1] else 0)"
        return [sys.executable, "-c", code, str(input_file)]

    monkeypatch.setattr(ingest_mod.runner_mod, "build_command", fake_build_command)
    monkeypatch.setattr(ingest_mod, "MIN_FILE_AGE_SECONDS", 0.0)

    ingest_dir = tmp_path / "ingest"
    ingest_dir.mkdir()
    store = SettingsStore(tmp_path / "settings.json")
    store.save({"scan_interval": 5, "stable_checks": 1, "output_dir": str(tmp_path / "out")})

    q = IngestQueue(ingest_dir, ingest_dir / "processed", ingest_dir / "failed", store)
    q.start()
    yield q
    q.stop()


def _wait_for_jobs(queue, count, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = queue.snapshot()
        if len(snapshot["history"]) >= count and not snapshot["pending"] and not snapshot["current"]:
            return snapshot
        time.sleep(0.2)
    pytest.fail(f"queue did not finish {count} job(s): {queue.snapshot()}")


def test_queue_converts_and_files_sources_by_result(queue):
    ingest_dir = queue.ingest_dir
    (ingest_dir / "good book.epub").write_bytes(b"x" * 64)
    (ingest_dir / "series").mkdir()
    (ingest_dir / "series" / "bad book.epub").write_bytes(b"x" * 64)
    (ingest_dir / "notes.txt").write_text("not an epub")

    snapshot = _wait_for_jobs(queue, 2)
    results = {job["file"]: job["status"] for job in snapshot["history"]}

    assert results["good book.epub"] == "done"
    assert results["series/bad book.epub"] == "failed"
    # Sub-folder structure is preserved when the source is filed away.
    assert (queue.processed_dir / "good book.epub").is_file()
    assert (queue.failed_dir / "series" / "bad book.epub").is_file()
    # Non-EPUB files are left alone.
    assert (ingest_dir / "notes.txt").is_file()
    assert not snapshot["blocked"]


def test_processed_files_are_not_converted_again(queue):
    (queue.ingest_dir / "book.epub").write_bytes(b"x" * 64)
    _wait_for_jobs(queue, 1)

    queue.request_scan()
    time.sleep(2)

    snapshot = queue.snapshot()
    assert len(snapshot["history"]) == 1
    assert not snapshot["pending"]


def test_enqueue_path_rejects_non_epub(queue, tmp_path):
    other = tmp_path / "cover.jpg"
    other.write_bytes(b"x")
    with pytest.raises(ValueError):
        queue.enqueue_path(other)
    with pytest.raises(FileNotFoundError):
        queue.enqueue_path(tmp_path / "missing.epub")


def test_clear_pending_releases_claims(tmp_path):
    """A queued file is claimed so it cannot be queued twice, until the queue is cleared."""
    ingest_dir = tmp_path / "ingest"
    ingest_dir.mkdir()
    # Deliberately not started: no worker thread, so the queue holds still.
    q = IngestQueue(ingest_dir, ingest_dir / "processed", ingest_dir / "failed",
                    SettingsStore(tmp_path / "settings.json"))

    book = ingest_dir / "book.epub"
    book.write_bytes(b"x" * 64)

    assert q.enqueue_path(book).rel == "book.epub"
    with pytest.raises(ValueError):
        q.enqueue_path(book)

    assert q.clear_pending() == 1
    assert q.snapshot()["pending"] == []
    # The claim is released, so the same file can be queued again.
    assert q.enqueue_path(book).rel == "book.epub"
