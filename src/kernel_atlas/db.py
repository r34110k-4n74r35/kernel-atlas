"""SQLite schema and connection helpers for a built kernel index."""

from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = "2"


class SchemaError(sqlite3.DatabaseError):
    """An index is incomplete or uses an unsupported schema."""


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE dirs (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,   -- '' is the kernel root
    parent_id INTEGER,
    name      TEXT NOT NULL,
    depth     INTEGER NOT NULL,
    n_files   INTEGER NOT NULL DEFAULT 0,
    n_subdirs INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE files (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    dir_id    INTEGER NOT NULL,
    name      TEXT NOT NULL,
    ext       TEXT,
    size      INTEGER NOT NULL DEFAULT 0,
    lines     INTEGER NOT NULL DEFAULT 0,
    n_symbols INTEGER NOT NULL DEFAULT 0,
    is_symlink INTEGER NOT NULL DEFAULT 0,
    link_target TEXT,
    index_status TEXT NOT NULL DEFAULT 'pending',
    index_error TEXT
);

CREATE TABLE symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_line  INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    signature   TEXT,
    is_static   INTEGER NOT NULL DEFAULT 0,
    is_inline   INTEGER NOT NULL DEFAULT 0,
    is_exported INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE subsystems (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    status      TEXT,
    maintainers TEXT,
    reviewers   TEXT,
    lists       TEXT,
    trees       TEXT,
    web         TEXT,
    n_files     INTEGER NOT NULL DEFAULT 0
);

-- Which subsystems claim a given file/dir. rank 0 is the most precise match.
CREATE TABLE path_subsys (
    ref_kind     TEXT NOT NULL,      -- 'file' | 'dir'
    ref_id       INTEGER NOT NULL,
    subsystem_id INTEGER NOT NULL,
    score        INTEGER NOT NULL,
    rank         INTEGER NOT NULL
);

CREATE TABLE calls (
    caller_id INTEGER NOT NULL,
    callee    TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX idx_dirs_parent    ON dirs(parent_id);
CREATE INDEX idx_files_dir      ON files(dir_id);
CREATE INDEX idx_files_name     ON files(name);
CREATE INDEX idx_files_ext      ON files(ext);
CREATE INDEX idx_sym_name       ON symbols(name);
CREATE INDEX idx_sym_file       ON symbols(file_id);
CREATE INDEX idx_sym_kind       ON symbols(kind);
CREATE INDEX idx_ps_ref         ON path_subsys(ref_kind, ref_id, rank);
CREATE INDEX idx_ps_sub         ON path_subsys(subsystem_id);
CREATE INDEX idx_calls_caller   ON calls(caller_id);
CREATE INDEX idx_calls_callee   ON calls(callee);
"""


def connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def create(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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


def validate_schema(conn: sqlite3.Connection) -> dict[str, str]:
    """Validate a completed index and return its metadata.

    Keeping this explicit lets callers inspect or repair arbitrary SQLite files
    when needed, while normal CLI open paths can reject stale, future, corrupt,
    or interrupted indexes before printing partial results.
    """
    try:
        meta = get_meta(conn)
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"missing or unreadable metadata table: {exc}") from exc

    for key, value in meta.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SchemaError(
                f"index metadata {key!r} must contain text keys and values")

    actual = meta.get("schema_version")
    if not actual:
        raise SchemaError("index has no schema version (it may be incomplete)")
    if actual != SCHEMA_VERSION:
        raise SchemaError(
            f"unsupported index schema {actual!r}; expected {SCHEMA_VERSION!r}"
        )
    if not meta.get("kernel_version"):
        raise SchemaError("index has no kernel version (it may be incomplete)")
    try:
        config.validate_version(meta["kernel_version"])
    except ValueError as exc:
        raise SchemaError(f"index has an unsafe kernel version: {exc}") from exc
    required_meta = {
        "source", "tree_path", "built_at", "kinds", "has_calls",
        "n_dirs", "n_files", "n_symbols", "n_subsystems",
        "n_parse_skipped", "n_parse_failed", "n_oversize", "n_symlinks",
        "build_seconds",
    }
    missing_meta = sorted(required_meta - meta.keys())
    if missing_meta:
        raise SchemaError(
            "index is missing metadata field(s): " + ", ".join(missing_meta))
    for key in ("n_dirs", "n_files", "n_symbols", "n_subsystems",
                "n_parse_skipped", "n_parse_failed", "n_oversize",
                "n_symlinks"):
        value = meta.get(key)
        if value is not None and re.fullmatch(r"[0-9]+", value) is None:
            raise SchemaError(f"index metadata {key} is not a non-negative integer")
    if "has_calls" in meta and meta["has_calls"] not in {"0", "1"}:
        raise SchemaError("index metadata has_calls must be 0 or 1")
    if "build_seconds" in meta:
        try:
            seconds = float(meta["build_seconds"])
        except (TypeError, ValueError) as exc:
            raise SchemaError("index metadata build_seconds is not numeric") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise SchemaError(
                "index metadata build_seconds must be a finite non-negative number")

    required_columns = {
        "meta": {"key", "value"},
        "dirs": {"id", "path", "parent_id", "name", "depth", "n_files",
                 "n_subdirs"},
        "files": {"id", "path", "dir_id", "name", "ext", "size", "lines",
                  "n_symbols", "is_symlink", "link_target", "index_status",
                  "index_error"},
        "symbols": {"id", "file_id", "name", "kind", "start_line", "end_line",
                    "signature", "is_static", "is_inline", "is_exported"},
        "subsystems": {"id", "name", "status", "maintainers", "reviewers",
                       "lists", "trees", "web", "n_files"},
        "path_subsys": {"ref_kind", "ref_id", "subsystem_id", "score", "rank"},
        "calls": {"caller_id", "callee"},
    }
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"could not inspect index schema: {exc}") from exc
    missing = sorted(required_columns.keys() - present)
    if missing:
        raise SchemaError("index is missing table(s): " + ", ".join(missing))
    for table, expected_columns in required_columns.items():
        try:
            actual_columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"could not inspect {table} table: {exc}") from exc
        missing_columns = sorted(expected_columns - actual_columns)
        if missing_columns:
            raise SchemaError(
                f"index table {table} is missing column(s): "
                + ", ".join(missing_columns)
            )
    return meta
