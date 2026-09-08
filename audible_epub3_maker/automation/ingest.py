"""Watch folder and job queue.

A scanner thread polls the ingest folder for EPUB files, waits until each one
has stopped changing (so a book still being copied onto the share is not picked
up half-written), and appends it to a FIFO queue. A worker thread converts the
queued books one at a time, then moves each source file into `processed/` or
`failed/` so it is never picked up twice.

Polling is deliberate: inotify is unreliable on the network and FUSE mounts
that Unraid shares are usually made of.
"""
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from audible_epub3_maker.automation import runner as runner_mod
from audible_epub3_maker.automation.runner import runner
from audible_epub3_maker.automation.settings_store import settings_store
from audible_epub3_maker.utils.constants import (
    INGEST_DIR, INGEST_FAILED_DIR, INGEST_PROCESSED_DIR,
)

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# A file must also be this old before it is considered settled, which catches
# copies that pause for longer than one scan interval.
MIN_FILE_AGE_SECONDS = 2.0


@dataclass
class Job:
    id: int
    path: Path
    rel: str
    status: str = QUEUED
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    message: str = ""

    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "file": self.rel,
            "status": self.status,
            "queued_at": datetime.fromtimestamp(self.queued_at).strftime("%Y-%m-%d %H:%M:%S"),
            "duration": self.duration(),
            "message": self.message,
        }


class IngestQueue:
    """Scans the ingest folder and converts what it finds, one book at a time."""

    def __init__(self, ingest_dir: Path = INGEST_DIR,
                 processed_dir: Path = INGEST_PROCESSED_DIR,
                 failed_dir: Path = INGEST_FAILED_DIR,
                 store=settings_store):
        self.ingest_dir = Path(ingest_dir)
        self.processed_dir = Path(processed_dir)
        self.failed_dir = Path(failed_dir)
        self.store = store

        self._lock = threading.RLock()
        self._pending: list[Job] = []
        self._history: list[Job] = []
        self._current: Job | None = None
        self._next_id = 1

        # Paths already queued, running, or that we could not move out of the
        # way - all of them must be ignored by the scanner.
        self._claimed: set[str] = set()
        self._blocked: dict[str, str] = {}
        # path -> (size, mtime, consecutive unchanged scans)
        self._settling: dict[str, tuple[int, float, int]] = {}

        self._stop = threading.Event()
        self._scan_now = threading.Event()
        self._work_ready = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_scan: float | None = None
        self._last_error: str = ""

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._threads:
            return
        self._ensure_dirs()
        self._stop.clear()
        for name, target in (("voicebox-scanner", self._scan_loop),
                             ("voicebox-worker", self._work_loop)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info(f"Ingest queue started, watching {self.ingest_dir}")

    def stop(self) -> None:
        self._stop.set()
        self._scan_now.set()
        self._work_ready.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    def _ensure_dirs(self) -> None:
        for directory in (self.ingest_dir, self.processed_dir, self.failed_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self._last_error = f"Cannot create {directory}: {e}"
                logger.error(self._last_error)

    # ----------------------------------------------------------------- scanning

    def request_scan(self) -> None:
        """Ask the scanner to run immediately instead of waiting out its interval."""
        self._scan_now.set()

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            interval = float(self.store.get("scan_interval", 15))
            try:
                if self.store.get("automation_enabled", True):
                    self._scan_once()
            except Exception as e:
                self._last_error = f"Scan failed: {e}"
                logger.exception(self._last_error)
            self._scan_now.wait(interval)
            self._scan_now.clear()

    def _iter_candidates(self):
        """Yield EPUB files in the ingest tree, skipping processed/ and failed/."""
        skip_roots = {self.processed_dir.resolve(), self.failed_dir.resolve()}
        for path in sorted(self.ingest_dir.rglob("*.epub")):
            try:
                resolved = path.resolve()
                if not path.is_file() or path.name.startswith("."):
                    continue
                if any(root in resolved.parents for root in skip_roots):
                    continue
                yield path, resolved
            except OSError:
                continue

    def _scan_once(self) -> None:
        self._ensure_dirs()
        seen: set[str] = set()

        for path, resolved in self._iter_candidates():
            key = str(resolved)
            seen.add(key)

            with self._lock:
                if key in self._claimed or key in self._blocked:
                    continue

            try:
                stat = path.stat()
            except OSError:
                continue

            if stat.st_size == 0 or (time.time() - stat.st_mtime) < MIN_FILE_AGE_SECONDS:
                self._settling[key] = (stat.st_size, stat.st_mtime, 0)
                continue

            previous = self._settling.get(key)
            if previous and previous[0] == stat.st_size and previous[1] == stat.st_mtime:
                stable_count = previous[2] + 1
            else:
                stable_count = 1
            self._settling[key] = (stat.st_size, stat.st_mtime, stable_count)

            if stable_count >= int(self.store.get("stable_checks", 2)):
                self._settling.pop(key, None)
                self._enqueue(path, resolved)

        # Forget files that have gone away, so a re-added file settles afresh.
        for key in list(self._settling):
            if key not in seen:
                self._settling.pop(key, None)
        with self._lock:
            for key in list(self._blocked):
                if key not in seen:
                    self._blocked.pop(key, None)
            self._last_scan = time.time()

    def _enqueue(self, path: Path, resolved: Path) -> Job:
        with self._lock:
            try:
                rel = str(path.relative_to(self.ingest_dir))
            except ValueError:
                rel = path.name
            job = Job(id=self._next_id, path=path, rel=rel)
            self._next_id += 1
            self._pending.append(job)
            self._claimed.add(str(resolved))
        logger.info(f"Queued [job {job.id}] {rel}")
        self._work_ready.set()
        return job

    def enqueue_path(self, path) -> Job:
        """Queue a specific file directly (used by the 'queue this file' action)."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        if path.suffix.lower() != ".epub":
            raise ValueError(f"Not an EPUB file: {path.name}")
        resolved = path.resolve()
        with self._lock:
            if str(resolved) in self._claimed:
                raise ValueError(f"{path.name} is already queued.")
        return self._enqueue(path, resolved)

    # ------------------------------------------------------------------ working

    def _take_next(self) -> Job | None:
        with self._lock:
            if self._current is not None or not self._pending:
                return None
            job = self._pending.pop(0)
            job.status = RUNNING
            job.started_at = time.time()
            self._current = job
            return job

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            job = self._take_next()
            if job is None:
                self._work_ready.wait(2.0)
                self._work_ready.clear()
                continue
            try:
                self._run_job(job)
            except Exception as e:
                logger.exception(f"[job {job.id}] crashed: {e}")
                self._finish(job, FAILED, f"Internal error: {e}")

    def _run_job(self, job: Job) -> None:
        # A conversion started from the Run button owns the machine until it is
        # done; wait it out rather than competing with it.
        while runner.is_busy() and not self._stop.is_set():
            time.sleep(1.0)
        if self._stop.is_set():
            self._finish(job, CANCELLED, "Shutting down.", move=False)
            return

        if not job.path.is_file():
            self._finish(job, FAILED, "File disappeared before conversion started.", move=False)
            return

        config = self.store.all()
        output_dir = Path(config["output_dir"])
        # Mirror any sub-folder structure from the ingest tree into the output.
        rel_parent = Path(job.rel).parent
        if str(rel_parent) not in (".", ""):
            output_dir = output_dir / rel_parent

        argv = runner_mod.build_command(
            input_file=job.path,
            output_dir=output_dir,
            output_filename=config["output_filename"],
            title_suffix=config["title_suffix"],
            log_level=config["log_level"],
            cleanup=config["cleanup"],
            tts_engine=config["tts_engine"],
            tts_lang=config["tts_lang"],
            tts_voice=config["tts_voice"],
            tts_speed=config["tts_speed"],
            tts_chunk_len=config["tts_chunk_len"],
            newline_mode=config["newline_mode"],
            align_threshold=config["align_threshold"],
            max_workers=config["max_workers"],
        )

        try:
            proc = runner.start(argv, runner_mod.AUTOMATION, f"job {job.id}: {job.rel}")
        except RuntimeError as e:
            # Something else grabbed the runner in between; retry this job later.
            logger.warning(f"[job {job.id}] deferred: {e}")
            with self._lock:
                job.status = QUEUED
                job.started_at = None
                self._pending.insert(0, job)
                self._current = None
            time.sleep(2.0)
            return

        returncode = proc.wait()

        if returncode == 0:
            self._finish(job, DONE, f"Saved to {output_dir}.")
        elif returncode < 0:
            self._finish(job, CANCELLED, f"Cancelled (signal {-returncode}).")
        else:
            self._finish(job, FAILED, f"main.py exited with code {returncode}. See the log for details.")

    def _finish(self, job: Job, status: str, message: str, move: bool = True) -> None:
        if move:
            destination_root = self.processed_dir if status == DONE else self.failed_dir
            try:
                destination = self._move_aside(job.path, destination_root)
                message = f"{message} Source moved to {destination}."
            except Exception as e:
                # Leaving the file in place would make the scanner queue it
                # again forever, so block it until it is moved or removed.
                with self._lock:
                    self._blocked[str(job.path.resolve())] = f"Could not move source file: {e}"
                message = f"{message} WARNING: could not move source file ({e}); it will be ignored until removed."
                logger.error(f"[job {job.id}] {message}")

        job.status = status
        job.message = message
        job.finished_at = time.time()

        with self._lock:
            self._claimed.discard(str(job.path.resolve()))
            self._history.insert(0, job)
            del self._history[50:]
            if self._current is job:
                self._current = None

        log = logger.info if status == DONE else logger.warning
        log(f"[job {job.id}] {status}: {job.rel} - {message}")
        self._work_ready.set()

    def _move_aside(self, source: Path, destination_root: Path) -> Path:
        try:
            rel = source.relative_to(self.ingest_dir)
        except ValueError:
            rel = Path(source.name)

        destination = destination_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = destination.with_name(f"{destination.stem}.{stamp}{destination.suffix}")
        shutil.move(str(source), str(destination))
        return destination

    # ------------------------------------------------------------------- status

    def cancel_current(self) -> str:
        with self._lock:
            current = self._current
        if current is None:
            return "No automation job is running."
        return runner.cancel()

    def clear_pending(self) -> int:
        with self._lock:
            count = len(self._pending)
            for job in self._pending:
                self._claimed.discard(str(job.path.resolve()))
            self._pending.clear()
        logger.info(f"Cleared {count} queued job(s)")
        return count

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def snapshot(self) -> dict:
        busy, source, label = runner.status()
        with self._lock:
            return {
                "enabled": bool(self.store.get("automation_enabled", True)),
                "ingest_dir": str(self.ingest_dir),
                "current": self._current.as_dict() if self._current else None,
                "pending": [job.as_dict() for job in self._pending],
                "history": [job.as_dict() for job in self._history],
                "blocked": dict(self._blocked),
                "last_scan": self._last_scan,
                "last_error": self._last_error,
                "runner_busy": busy,
                "runner_source": source,
                "runner_label": label,
            }


ingest_queue = IngestQueue()
