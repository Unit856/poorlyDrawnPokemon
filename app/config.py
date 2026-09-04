"""Process configuration.

Everything the app needs to find on disk hangs off DATA_DIR, which is the single
Docker volume described in scope 13. Images and the database live inside it
together because, per that section, they are the entire product state.
"""

from __future__ import annotations

import os
from pathlib import Path

from app import __version__


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).resolve()


DATA_DIR = _env_path("WTP_DATA_DIR", "/data")
IMAGES_DIR = DATA_DIR / "images"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "wtp.sqlite3"
DATABASE_URL = os.environ.get("WTP_DATABASE_URL", "") or f"sqlite:///{DB_PATH}"

# PokeAPI rejects requests with no User-Agent (HTTP 403), so this is required,
# not politeness.
POKEAPI_BASE = os.environ.get("WTP_POKEAPI_BASE", "https://pokeapi.co/api/v2")
USER_AGENT = os.environ.get(
    "WTP_USER_AGENT", f"whos-that-pokemon/{__version__} (self-hosted friend-group tool)"
)
SEED_CONCURRENCY = int(os.environ.get("WTP_SEED_CONCURRENCY", "8"))
SEED_TIMEOUT_SECONDS = float(os.environ.get("WTP_SEED_TIMEOUT", "30"))

CANVAS_SIZE = 800  # scope 7.2: export at 800x800
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # a line-art 800x800 PNG is ~100-400 KB (scope 13)
FILL_TOLERANCE = 32  # scope 7.2: per-channel, out of 255
UNDO_DEPTH = 30  # scope 7.2: stack of at least 30 actions
#: Opaque fallback if transparency ever composites badly in-game (scope 7.2).
CANVAS_FALLBACK_BACKGROUND = "#F7F7F7"

SUBMIT_COOLDOWN_SECONDS = 10  # scope 12
PACK_ROW_WARN_THRESHOLD = 2800  # scope 10.4
PACK_CHUNK_SIZE = 2500  # scope 10.4, reserved split rule


def ensure_dirs() -> None:
    for path in (DATA_DIR, IMAGES_DIR, CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
