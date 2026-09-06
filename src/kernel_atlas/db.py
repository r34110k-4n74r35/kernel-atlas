"""Public SQLite connection and completed-index validation interface.

Schema definitions and integrity audits live in dedicated modules; callers
continue to create, open, finalize, and validate indexes through this module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .storage.schema import (
    INDEXES as INDEXES,
    SCHEMA as SCHEMA,
    SCHEMA_VERSION as SCHEMA_VERSION,
    TYPE_ALIAS_KINDS as TYPE_ALIAS_KINDS,
    SchemaError as SchemaError,
)
from .storage.validation import validate_schema as validate_schema


def _require_safe_sidecars(path: Path) -> None:
    """Keep SQLite's adjacent files within the project too.

    Read-only SQLite connections can still touch WAL shared-memory files.
    Hard links cannot be traced back to all their aliases, so refuse them for
    files SQLite may update instead of risking an alias outside the project.
    """
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = config.require_project_path(Path(f"{path}{suffix}"))
        if sidecar.exists() and sidecar.stat().st_nlink > 1:
            raise ValueError(f"SQLite sidecar has multiple hard links: {sidecar}")


def connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    # Resolve a permitted leaf alias before checking sidecars: SQLite uses the
    # real database location for its journal and WAL files.
    path = config.require_project_path(path).resolve()
    if path.exists() and path.stat().st_nlink > 1:
        raise ValueError(f"index database has multiple hard links: {path}")
    _require_safe_sidecars(path)
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create(path: Path) -> sqlite3.Connection:
    # Creating an index replaces the directory entry.  An existing leaf alias
    # may be removed safely without opening or changing its target.
    path = config.require_project_path(path, follow_leaf=False)
    _require_safe_sidecars(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    conn.executescript(SCHEMA)
    return conn


def finalize(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEXES)
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    # Numeric access works both for connections returned by ``connect`` and
    # ordinary sqlite3 connections whose row_factory was not changed.
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
