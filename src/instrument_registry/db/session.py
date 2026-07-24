"""Connection helper for the local instrument/entity cache.

Mirrors currens's configure-then-connect pattern: a module-level default
path that callers can override per-call (`db_path=...`) without any
global mutable state beyond the default itself.

The default lives in the user's XDG data directory rather than inside the
package source tree, so that reinstalling, cleaning or re-cloning the
package can't destroy the cache. That matters mainly for
`instrument_aliases`: ATHEX/GLEIF rows are re-fetchable, locally-learned
aliases are not.
"""
import os
from pathlib import Path
import sqlite3

from instrument_registry.db.models import create_schema

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser() / "instrument_registry"
DEFAULT_DB_PATH = DATA_DIR / "instrument_registry.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve() if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    return connection
