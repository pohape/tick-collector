"""Environment parsing and validation."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val or not val.strip():
        raise RuntimeError(f"missing required env var: {key}")
    return val.strip()


def _csv_list(key: str) -> list[str]:
    return [s.strip() for s in _require(key).split(",") if s.strip()]


DATA_DIR = Path(_require("DATA_DIR"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
BINANCE_SYMBOLS = _csv_list("BINANCE_SYMBOLS")
BYBIT_SYMBOLS = _csv_list("BYBIT_SYMBOLS")
WS_TEST_MODE = os.getenv("WS_TEST_MODE", "false").lower() in ("true", "1", "yes")

# Flush writer every N rows
FLUSH_EVERY = int(os.getenv("FLUSH_EVERY", "500"))

# Gap warning threshold in seconds
GAP_WARN_SECONDS = float(os.getenv("GAP_WARN_SECONDS", "10"))

# Maintenance
LOCAL_STORAGE_MB = int(_require("LOCAL_STORAGE_MB"))
WEBDAV_USER = _require("WEBDAV_USER")
WEBDAV_PASSWORD = _require("WEBDAV_PASSWORD")
WEBDAV_URL = _require("WEBDAV_URL")

# Optional second WebDAV for redundant backup
WEBDAV2_USER = os.getenv("WEBDAV2_USER", "").strip() or None
WEBDAV2_PASSWORD = os.getenv("WEBDAV2_PASSWORD", "").strip() or None
WEBDAV2_URL = os.getenv("WEBDAV2_URL", "").strip() or None
