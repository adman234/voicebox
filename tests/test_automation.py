import json
import sys
import time

import pytest

from audible_epub3_maker.automation import ingest as ingest_mod
from audible_epub3_maker.automation.ingest import IngestQueue
from audible_epub3_maker.automation.runner import ConversionRunner
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


def test_failure_output_is_captured_for_the_ui(tmp_path, monkeypatch):
    """A crashing conversion's traceback must reach the job, not vanish into stderr."""
    def exploding_command(input_file, **kwargs):
        code = ("import sys; print('progress line', flush=True); "
                "raise RuntimeError('disk quota exceeded')")
        return [sys.executable, "-c", code]

    monkeypatch.setattr(ingest_mod.runner_mod, "build_command", exploding_command)
    monkeypatch.setattr(ingest_mod, "MIN_FILE_AGE_SECONDS", 0.0)

    ingest_dir = tmp_path / "ingest"
    ingest_dir.mkdir()
    store = SettingsStore(tmp_path / "settings.json")
    store.save({"scan_interval": 5, "stable_checks": 1, "output_dir": str(tmp_path / "out")})

    q = IngestQueue(ingest_dir, ingest_dir / "processed", ingest_dir / "failed", store)
    q.start()
    try:
        (ingest_dir / "book.epub").write_bytes(b"x" * 64)
        snapshot = _wait_for_jobs(q, 1)
    finally:
        q.stop()

    job = snapshot["history"][0]
    assert job["status"] == "failed"
    # The exception reaches the one-line summary shown in the jobs table...
    assert "disk quota exceeded" in job["message"]
    # ...and the full output, including the traceback, is available to the UI.
    full = "\n".join(snapshot["last_output"])
    assert "progress line" in full
    assert "Traceback" in full
    assert "RuntimeError: disk quota exceeded" in full


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


## ----------------------------------------------------------------- progress

def _feed(runner, lines):
    """Push output lines through the runner's parser as the reader thread would."""
    with runner._output_lock:
        for line in lines:
            runner._track_progress(line)


def test_progress_tracks_chapters_from_real_log_lines():
    runner = ConversionRunner()

    # Before the task count is known, only the stage is reported.
    assert runner.progress()["total"] is None
    assert runner.progress()["percent"] is None

    _feed(runner, ["2026-09-09 07:50:44 [ INFO] - 🚀 Start processing [book.epub] ... (Total tasks: 8)"])
    progress = runner.progress()
    assert progress["total"] == 8
    assert progress["percent"] == 0.0
    assert progress["stage"] == "converting chapters"

    _feed(runner, [
        "✅ [Task 0] complete. TaskResult(...)",
        "✅ [Task 3] complete. TaskResult(...)",
        "❌ [Task 1] failed. TaskErrorResult(...)",
    ])
    progress = runner.progress()
    assert progress["finished"] == 3
    assert progress["failed"] == 1
    assert progress["percent"] == 37.5

    # A worker-level failure line must not be double counted with the app's.
    _feed(runner, ["⚠️ [Task 1] failed during execution"])
    assert runner.progress()["finished"] == 3

    # Repeated lines cannot inflate the count either.
    _feed(runner, ["✅ [Task 0] complete. TaskResult(...)"])
    assert runner.progress()["finished"] == 3

    _feed(runner, ["🎉 Processing complete. 7 success, 1 failed. (finished in 40m 12s)"])
    assert runner.progress()["stage"] == "assembling EPUB"
    _feed(runner, ["💾 EPUB saved to /output/book.epub"])
    assert runner.progress()["stage"] == "saved"


def test_progress_resets_between_conversions():
    runner = ConversionRunner()
    _feed(runner, ["🚀 Start processing [a.epub] ... (Total tasks: 4)", "✅ [Task 0] complete."])
    assert runner.progress()["finished"] == 1

    proc = runner.start([sys.executable, "-c", "pass"], "manual", "b.epub")
    proc.wait()
    progress = runner.progress()
    assert progress["total"] is None and progress["finished"] == 0


## ------------------------------------------------------- command building

@pytest.mark.parametrize("suffix", ["-voicebox", "_voicebox", "by-voicebox", "--odd", ""])
def test_values_starting_with_a_hyphen_survive_argparse(suffix):
    """A bare "--opt value" makes argparse read a leading "-" as another option,
    so a suffix like "-voicebox" failed with "expected one argument"."""
    argv = ingest_mod.runner_mod.build_command(
        input_file="/in/book.epub", output_dir="/out", output_filename="",
        title_suffix=suffix, log_level="INFO", cleanup=False,
        tts_engine="kokoro", tts_lang="a", tts_voice="af_heart", tts_speed=1.0,
        tts_chunk_len=0, newline_mode="multi", align_threshold=95.0, max_workers=1,
        output_formats=["m4b"],
    )
    assert f"--title_suffix={suffix}" in argv
    # Nothing may be passed as a separate bare value that could be misread.
    assert not any(a == "--title_suffix" for a in argv)


def test_build_command_carries_the_chosen_formats():
    argv = ingest_mod.runner_mod.build_command(
        input_file="/in/book.epub", output_dir="/out", output_filename="",
        title_suffix="", log_level="INFO", cleanup=False,
        tts_engine="kokoro", tts_lang="a", tts_voice="af_heart", tts_speed=1.0,
        tts_chunk_len=0, newline_mode="multi", align_threshold=95.0, max_workers=1,
        output_formats=["mp3", "m4b"],
    )
    assert "--output_formats=mp3,m4b" in argv


def test_defaults_are_m4b_and_the_voicebox_suffix():
    assert DEFAULTS["output_formats"] == ["m4b"]
    assert DEFAULTS["title_suffix"] == "_voicebox"


def test_progress_is_weighted_by_chapter_size():
    """Front matter is tiny and real chapters are long, so counting chapters
    reports wildly optimistic progress. Weighting by characters fixes it."""
    runner = ConversionRunner()
    _feed(runner, [
        "📏 Chapter characters: 0=100,1=100,2=100,3=30000,4=30000,5=30000",
        "🚀 Start processing [book.epub] ... (Total tasks: 6)",
    ])

    # The three trivial front-matter chapters are done: half the chapters,
    # but only a third of one percent of the actual work.
    _feed(runner, [f"✅ [Task {i}] complete." for i in range(3)])
    progress = runner.progress()
    assert progress["finished"] == 3
    assert progress["percent"] < 1.0        # not 50%

    _feed(runner, ["✅ [Task 3] complete."])
    assert runner.progress()["percent"] == pytest.approx(33.4, abs=0.5)


def test_progress_falls_back_to_counting_when_sizes_are_absent():
    runner = ConversionRunner()
    _feed(runner, ["🚀 Start processing [book.epub] ... (Total tasks: 4)",
                   "✅ [Task 0] complete."])
    assert runner.progress()["percent"] == 25.0
