"""Persistent index schema and shared schema compatibility definitions.

DDL, indexes, and version identity live together so readers and builders use
one format contract without depending on connection or validation code.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "7"
READABLE_SCHEMA_VERSIONS = {"6", SCHEMA_VERSION}
# Direct typedef spellings can belong to any C tagged-type definition.  Enum
# aliases are retained even though type_members currently models only structs
# and unions, and structure queries deliberately remain struct/union-scoped.
TYPE_ALIAS_KINDS = ("struct", "union", "enum")


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
    n_subdirs INTEGER NOT NULL DEFAULT 0,
    n_files_recursive INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES dirs(id)
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
    index_error TEXT,
    call_domain TEXT NOT NULL DEFAULT 'kernel',
    FOREIGN KEY (dir_id) REFERENCES dirs(id)
);

CREATE TABLE symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_line  INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    signature   TEXT,
    summary     TEXT,
    description TEXT,
    is_static   INTEGER NOT NULL DEFAULT 0,
    is_inline   INTEGER NOT NULL DEFAULT 0,
    is_exported INTEGER NOT NULL DEFAULT 0,
    is_anonymous INTEGER NOT NULL DEFAULT 0,
    parse_complete INTEGER NOT NULL DEFAULT 1,
    parse_warnings TEXT NOT NULL DEFAULT '[]',
    unmatched_member_docs TEXT NOT NULL DEFAULT '{}',
    conditions TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (file_id) REFERENCES files(id)
);

CREATE TABLE type_aliases (
    symbol_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    UNIQUE (symbol_id, name),
    FOREIGN KEY (symbol_id) REFERENCES symbols(id)
);

-- Preorder member rows make nested anonymous aggregates representable while
-- retaining a stable declaration order.  Raw declarations remain the source
-- of truth; the parsed shape columns are study-oriented conveniences.
CREATE TABLE type_members (
    id                 INTEGER PRIMARY KEY,
    symbol_id          INTEGER NOT NULL,
    parent_id          INTEGER,
    ordinal            INTEGER NOT NULL,
    name               TEXT,
    kind               TEXT NOT NULL,
    type_text          TEXT,
    declaration        TEXT NOT NULL,
    start_line         INTEGER NOT NULL,
    end_line           INTEGER NOT NULL,
    bit_width          TEXT,
    array_dimensions   TEXT NOT NULL DEFAULT '[]',
    description        TEXT,
    description_source TEXT,
    conditions         TEXT NOT NULL DEFAULT '[]',
    visibility         TEXT NOT NULL DEFAULT 'unspecified',
    is_anonymous       INTEGER NOT NULL DEFAULT 0,
    generated_by       TEXT,
    UNIQUE (symbol_id, ordinal),
    FOREIGN KEY (symbol_id) REFERENCES symbols(id),
    FOREIGN KEY (parent_id) REFERENCES type_members(id)
);

CREATE TABLE subsystems (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    status      TEXT,
    maintainers TEXT,
    reviewers   TEXT,
    lists       TEXT,
    trees       TEXT,
    websites    TEXT,
    patchwork   TEXT,
    bugs        TEXT,
    chats       TEXT,
    profiles    TEXT,
    keywords    TEXT,
    n_files     INTEGER NOT NULL DEFAULT 0,
    n_primary_files INTEGER NOT NULL DEFAULT 0
);

-- Which subsystems claim a file.  Rank orders evidence; every section tied at
-- the top score is primary instead of manufacturing one alphabetical owner.
CREATE TABLE path_subsys (
    ref_kind     TEXT NOT NULL CHECK (ref_kind = 'file'),
    ref_id       INTEGER NOT NULL,
    subsystem_id INTEGER NOT NULL,
    score        INTEGER NOT NULL,
    rank         INTEGER NOT NULL,
    is_primary   INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    UNIQUE (ref_kind, ref_id, subsystem_id),
    FOREIGN KEY (ref_id) REFERENCES files(id),
    FOREIGN KEY (subsystem_id) REFERENCES subsystems(id)
);

-- Directory composition is derived from the ownership of descendant files.
-- MAINTAINERS F: patterns describe files, so applying them directly to a
-- directory name produces both false owners and false gaps.
CREATE TABLE dir_subsys (
    dir_id          INTEGER NOT NULL,
    subsystem_id    INTEGER NOT NULL,
    n_claimed       INTEGER NOT NULL,
    n_primary       INTEGER NOT NULL,
    coverage        REAL NOT NULL,
    rank            INTEGER NOT NULL,
    UNIQUE (dir_id, subsystem_id),
    FOREIGN KEY (dir_id) REFERENCES dirs(id),
    FOREIGN KEY (subsystem_id) REFERENCES subsystems(id)
);

-- Quoted ``#include "member.c"`` edges identify aggregate translation units.
-- They are not ordinary header dependencies: a static function in the member
-- is a same-unit target for calls written in the including source.
CREATE TABLE source_includes (
    includer_id INTEGER NOT NULL,
    included_id INTEGER NOT NULL,
    line        INTEGER NOT NULL,
    UNIQUE (includer_id, included_id),
    FOREIGN KEY (includer_id) REFERENCES files(id),
    FOREIGN KEY (included_id) REFERENCES files(id)
);

-- Sources with explicit Kbuild object evidence remain standalone translation
-- unit roots even when another source also includes them as a quoted C member.
CREATE TABLE translation_unit_roots (
    file_id INTEGER PRIMARY KEY,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

CREATE TABLE calls (
    caller_id      INTEGER NOT NULL,
    callee         TEXT NOT NULL,
    callee_id      INTEGER,
    resolution     TEXT NOT NULL DEFAULT 'unresolved'
      CHECK (resolution IN
             ('same_file', 'included_source', 'unique_global', 'ambiguous',
              'macro', 'indirect', 'unresolved')),
    direct_count   INTEGER NOT NULL DEFAULT 1 CHECK (direct_count >= 0),
    indirect_count INTEGER NOT NULL DEFAULT 0 CHECK (indirect_count >= 0),
    macro_count    INTEGER NOT NULL DEFAULT 0 CHECK (macro_count >= 0),
    CHECK (direct_count + indirect_count + macro_count > 0),
    CHECK ((resolution IN ('same_file', 'included_source', 'unique_global')) =
           (callee_id IS NOT NULL)),
    UNIQUE (caller_id, callee),
    FOREIGN KEY (caller_id) REFERENCES symbols(id),
    FOREIGN KEY (callee_id) REFERENCES symbols(id)
);

CREATE TABLE call_sites (
    caller_id INTEGER NOT NULL REFERENCES symbols(id),
    callee TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('direct','indirect','macro')),
    line INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL
);

CREATE TABLE document_text (
    file_id INTEGER PRIMARY KEY REFERENCES files(id),
    content TEXT NOT NULL
);

CREATE TABLE function_docs (
    symbol_id INTEGER PRIMARY KEY REFERENCES symbols(id),
    line INTEGER NOT NULL,
    summary TEXT NOT NULL,
    parameters TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT NOT NULL,
    returns TEXT NOT NULL
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
CREATE INDEX idx_alias_name     ON type_aliases(name);
CREATE INDEX idx_member_parent  ON type_members(parent_id);
CREATE INDEX idx_ps_ref         ON path_subsys(ref_kind, ref_id, rank);
CREATE INDEX idx_ps_sub         ON path_subsys(subsystem_id);
CREATE INDEX idx_ds_dir         ON dir_subsys(dir_id, rank);
CREATE INDEX idx_ds_sub         ON dir_subsys(subsystem_id);
CREATE INDEX idx_inc_includer   ON source_includes(includer_id);
CREATE INDEX idx_inc_included   ON source_includes(included_id);
CREATE INDEX idx_calls_caller   ON calls(caller_id);
CREATE INDEX idx_calls_callee   ON calls(callee);
CREATE INDEX idx_calls_target   ON calls(callee_id);
CREATE INDEX idx_sym_file_name  ON symbols(file_id, name, kind);
CREATE INDEX idx_call_sites_caller ON call_sites(caller_id, line, byte_offset);
CREATE INDEX idx_call_sites_callee ON call_sites(callee, caller_id);
"""
