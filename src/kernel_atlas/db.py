"""SQLite schema and connection helpers for a built kernel index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

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
    n_symbols INTEGER NOT NULL DEFAULT 0
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
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.DatabaseError:
        return {}
