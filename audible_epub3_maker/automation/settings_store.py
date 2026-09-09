"""Persisted defaults used when Voicebox converts books on its own.

The Settings tab of the web GUI writes this file; the ingest queue reads it
before every job, so changing a default takes effect on the next book without
restarting the container.
"""
import json
import logging
import threading
from pathlib import Path
from typing import Any

from audible_epub3_maker.audiobook import FORMATS as OUTPUT_FORMATS
from audible_epub3_maker.utils.constants import OUTPUT_DIR, SETTINGS_FILE

logger = logging.getLogger(__name__)

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
TTS_ENGINES = ["azure", "kokoro"]
NEWLINE_MODES = ["none", "single", "multi"]
M4B_BITRATES = ["32k", "48k", "64k", "96k", "128k"]

DEFAULTS: dict[str, Any] = {
    # Automation behaviour
    "automation_enabled": True,
    "scan_interval": 15,     # seconds between ingest folder scans
    "stable_checks": 2,      # consecutive unchanged scans before a file is queued

    # Defaults mirroring the Convert tab
    "output_dir": str(OUTPUT_DIR),
    "output_formats": ["m4b"],
    "m4b_bitrate": "64k",
    "output_filename": "",
    "title_suffix": "_voicebox",
    "log_level": "INFO",
    "cleanup": False,
    "tts_engine": "kokoro",
    "tts_lang": "a",
    "tts_voice": "af_heart",
    "tts_speed": 1.0,
    "tts_chunk_len": 0,
    "newline_mode": "multi",
    "align_threshold": 95.0,
    "max_workers": 3,
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, low: int, high: int) -> int:
    number = int(float(value))
    return max(low, min(high, number))


def _as_float(value: Any, low: float, high: float) -> float:
    number = float(value)
    return max(low, min(high, number))


def _as_formats(value: Any) -> list[str]:
    """Keep only recognised output formats, never an empty selection."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    chosen = [str(item).strip().lower() for item in items]
    chosen = [name for name in chosen if name in OUTPUT_FORMATS]
    # Producing nothing is never what was meant.
    return chosen or ["epub"]


def _as_choice(value: Any, choices: list[str], fallback: str) -> str:
    text = str(value or "").strip()
    for choice in choices:
        if text.lower() == choice.lower():
            return choice
    return fallback


_CASTS = {
    "automation_enabled": _as_bool,
    "scan_interval": lambda v: _as_int(v, 5, 3600),
    "stable_checks": lambda v: _as_int(v, 1, 20),
    "output_dir": lambda v: str(v or "").strip() or DEFAULTS["output_dir"],
    "output_formats": _as_formats,
    "m4b_bitrate": lambda v: _as_choice(v, M4B_BITRATES, "64k"),
    "output_filename": lambda v: str(v or "").strip(),
    "title_suffix": lambda v: str(v or "").strip(),
    "log_level": lambda v: _as_choice(v, LOG_LEVELS, "INFO"),
    "cleanup": _as_bool,
    "tts_engine": lambda v: _as_choice(v, TTS_ENGINES, "kokoro"),
    "tts_lang": lambda v: str(v or "").strip(),
    "tts_voice": lambda v: str(v or "").strip(),
    "tts_speed": lambda v: _as_float(v, 0.5, 2.0),
    "tts_chunk_len": lambda v: _as_int(v, 0, 100_000),
    "newline_mode": lambda v: _as_choice(v, NEWLINE_MODES, "multi"),
    "align_threshold": lambda v: _as_float(v, 80.0, 100.0),
    "max_workers": lambda v: _as_int(v, 1, 16),
}


def coerce(values: dict) -> dict:
    """Return `values` merged onto the defaults, with every entry type-checked.

    Unknown keys are dropped and unusable ones fall back to their default, so a
    hand-edited settings.json can never stop the app from starting.
    """
    result = dict(DEFAULTS)
    for key, raw in (values or {}).items():
        if key not in _CASTS:
            continue
        try:
            result[key] = _CASTS[key](raw)
        except (TypeError, ValueError):
            logger.warning(f"Ignoring invalid value for setting '{key}': {raw!r}")
    return result


class SettingsStore:
    """Thread-safe reader/writer for the automation defaults JSON file."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._values = dict(DEFAULTS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._values = coerce(raw)
                logger.info(f"Loaded automation settings from {self._path}")
            except FileNotFoundError:
                self._values = dict(DEFAULTS)
                logger.info(f"No settings file at {self._path}, using built-in defaults")
            except Exception as e:
                self._values = dict(DEFAULTS)
                logger.warning(f"Could not read {self._path} ({e}), using built-in defaults")
            return dict(self._values)

    def save(self, values: dict) -> dict:
        """Validate and persist `values`, returning what was actually stored."""
        with self._lock:
            merged = coerce({**self._values, **(values or {})})
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a sibling temp file then rename, so a crash mid-write
            # cannot leave a truncated settings.json behind.
            tmp_path = self._path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(self._path)
            self._values = merged
            logger.info(f"Saved automation settings to {self._path}")
            return dict(merged)

    def all(self) -> dict:
        with self._lock:
            return dict(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, DEFAULTS.get(key, default))


settings_store = SettingsStore()
