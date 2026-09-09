import os
from pathlib import Path
## Internal configuration (not intended for user modification) ##

APP_NAME = "Voicebox"
APP_VERSION = "1.0.2"
APP_FULLNAME = APP_NAME + " v" + APP_VERSION
APP_IN_DEV = True

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _env_dir(env_var: str, default: Path) -> Path:
    """Resolve a directory from an environment variable, falling back to `default`.

    Lets the container point OUTPUT/INGEST/CONFIG at mounted volumes without
    editing the code.
    """
    value = os.environ.get(env_var, "").strip()
    return Path(value).expanduser() if value else default


OUTPUT_DIR = _env_dir("VOICEBOX_OUTPUT_DIR", BASE_DIR / "output")
INPUT_DIR = BASE_DIR / "input"  # for test
DEV_OUTPUT_DIR = BASE_DIR / "dev_output"  # for dev test

# Automation (watch folder) config
INGEST_DIR = _env_dir("VOICEBOX_INGEST_DIR", BASE_DIR / "ingest")
INGEST_PROCESSED_DIR = INGEST_DIR / "processed"
INGEST_FAILED_DIR = INGEST_DIR / "failed"
CONFIG_DIR = _env_dir("VOICEBOX_CONFIG_DIR", BASE_DIR / "config")
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# logging config
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
LOG_FORMAT = "%(asctime)s [%(levelname)5s] [p%(process)d,t%(thread)d] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
LOG_FORMAT_SIMPLE = "[%(asctime)s] [p%(process)d] [%(levelname)s] - %(message)s"

# HTML segmentation config
BEAUTIFULSOUP_PARSER = "lxml-xml"
SEG_TAG = "span"
SEG_ID_PREFIX = "ae"
SEG_MARK_ATTR = "data-ae-x"

AUDIO_MIMETYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
}
