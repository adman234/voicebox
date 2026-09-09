"""A single point of control for the `main.py` subprocess.

Both the Run button and the ingest queue convert books by spawning `main.py`.
They share one runner so the two can never run at the same time: conversions
are CPU heavy and all of them write to the same log file that the GUI tails.
"""
import collections
import logging
import subprocess
import sys
import threading

from audible_epub3_maker.utils.constants import BASE_DIR

logger = logging.getLogger(__name__)

MANUAL = "manual"
AUTOMATION = "automation"

# How many lines of a conversion's output to keep. A traceback plus the lines
# leading up to it fit comfortably.
OUTPUT_TAIL_LINES = 200


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
        # main.py writes its errors to stderr, and the logging system can drop
        # the last records when the process exits straight after logging them.
        # Capturing the child's output directly is the only reliable way to see
        # why a conversion failed.
        self._output_lock = threading.Lock()
        self._output: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL_LINES)

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
            with self._output_lock:
                self._output.clear()
            proc = subprocess.Popen(
                argv,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            # The pipe must be drained continuously or the child blocks once it
            # fills, so the reader thread runs for the life of the process.
            threading.Thread(target=self._drain_output, args=(proc,),
                             name="voicebox-output", daemon=True).start()
            self._proc = proc
            self._source = source
            self._label = label
            return proc

    def _drain_output(self, proc: subprocess.Popen) -> None:
        """Keep the tail of the child's output, and pass it through to our own
        stderr so it still reaches `docker logs`."""
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                with self._output_lock:
                    self._output.append(line)
                print(line, file=sys.stderr, flush=True)
        except Exception as e:  # a closed pipe must not take the thread down
            logger.debug(f"Output reader stopped: {e}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def output_tail(self, lines: int = OUTPUT_TAIL_LINES) -> list[str]:
        """The last lines the most recent conversion printed."""
        with self._output_lock:
            return list(self._output)[-lines:]

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
