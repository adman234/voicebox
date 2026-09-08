"""A single point of control for the `main.py` subprocess.

Both the Run button and the ingest queue convert books by spawning `main.py`.
They share one runner so the two can never run at the same time: conversions
are CPU heavy and all of them write to the same log file that the GUI tails.
"""
import logging
import subprocess
import sys
import threading

from audible_epub3_maker.utils.constants import BASE_DIR

logger = logging.getLogger(__name__)

MANUAL = "manual"
AUTOMATION = "automation"


def build_command(input_file, output_dir, output_filename, title_suffix, log_level, cleanup,
                  tts_engine, tts_lang, tts_voice, tts_speed,
                  tts_chunk_len, newline_mode, align_threshold, max_workers) -> list[str]:
    """Build the `main.py` argv for one conversion."""
    args = [
        sys.executable, str(BASE_DIR / "main.py"),
        str(input_file),
        "-d", str(output_dir) if output_dir else "",
        "-o", str(output_filename or ""),
        "--title_suffix", str(title_suffix or ""),
        "--log_level", str(log_level),
        "--tts_engine", str(tts_engine).lower(),
        "--tts_lang", str(tts_lang or ""),
        "--tts_voice", str(tts_voice or ""),
        "--tts_speed", str(tts_speed),
        "--tts_chunk_len", str(int(tts_chunk_len)),
        "--newline_mode", str(newline_mode),
        "--align_threshold", str(align_threshold),
        "--max_workers", str(int(max_workers)),
        "--force",
    ]
    if cleanup:
        args.append("--cleanup")
    return args


class ConversionRunner:
    """Owns whichever conversion subprocess is currently running, if any."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._source: str = ""
        self._label: str = ""

    def _reap(self) -> None:
        """Forget a finished process. Caller must hold the lock."""
        if self._proc is not None and self._proc.poll() is not None:
            logger.info(f"Conversion process [p{self._proc.pid}] exited with code {self._proc.returncode}")
            self._proc = None
            self._source = ""
            self._label = ""

    def status(self) -> tuple[bool, str, str]:
        """Return (busy, source, label) for the conversion in flight."""
        with self._lock:
            self._reap()
            return (self._proc is not None, self._source, self._label)

    def is_busy(self) -> bool:
        return self.status()[0]

    def start(self, argv: list[str], source: str, label: str) -> subprocess.Popen:
        """Spawn a conversion, or raise RuntimeError if one is already running."""
        with self._lock:
            self._reap()
            if self._proc is not None:
                raise RuntimeError(
                    f"A conversion is already running ({self._source}: {self._label}). "
                    f"Wait for it to finish or cancel it first."
                )
            logger.info(f"Starting {source} conversion: {label}")
            logger.debug(f"Command: {argv}")
            proc = subprocess.Popen(argv, cwd=str(BASE_DIR))
            self._proc = proc
            self._source = source
            self._label = label
            return proc

    def cancel(self) -> str:
        """Terminate the running conversion. Returns a message describing what happened."""
        with self._lock:
            self._reap()
            if self._proc is None:
                return "No conversion is running."

            proc, label = self._proc, self._label
            try:
                proc.terminate()
                proc.wait(5)
                message = f"Conversion cancelled ({label})."
            except subprocess.TimeoutExpired:
                proc.kill()
                message = f"Force killed conversion [p{proc.pid}] ({label})."
            except Exception as e:
                return f"Error terminating conversion [p{proc.pid}]: {e}"

            self._proc = None
            self._source = ""
            self._label = ""
            logger.warning(message)
            return message


runner = ConversionRunner()
