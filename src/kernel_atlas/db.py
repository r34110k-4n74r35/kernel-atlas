"""SQLite schema and connection helpers for a built kernel index."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import call_resolution, config

SCHEMA_VERSION = "5"
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
    caller_id  INTEGER NOT NULL,
    callee     TEXT NOT NULL,
    callee_id  INTEGER,
    resolution TEXT NOT NULL DEFAULT 'unresolved'
      CHECK (resolution IN
             ('same_file', 'included_source', 'unique_global', 'ambiguous',
              'macro', 'indirect', 'unresolved')),
    CHECK ((resolution IN ('same_file', 'included_source', 'unique_global')) =
           (callee_id IS NOT NULL)),
    UNIQUE (caller_id, callee),
    FOREIGN KEY (caller_id) REFERENCES symbols(id),
    FOREIGN KEY (callee_id) REFERENCES symbols(id)
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
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create(path: Path) -> sqlite3.Connection:
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


def _validate_row_domains(conn: sqlite3.Connection) -> None:
    """Reject SQLite values outside the logical domains of the schema.

    SQLite column affinity is intentionally permissive: a hand-built or
    damaged database can put a BLOB in a ``TEXT`` column, despite the declared
    type.  Query and rendering code is entitled to rely on these identities,
    counters, and flags after an explicit deep check, so validate the stored
    value classes as well as the table layout.
    """
    text_or_null = lambda name: (  # noqa: E731 - keeps predicates readable
        f"({name} IS NULL OR (typeof({name})='text'"
        f" AND instr({name},char(0))=0))")
    integer_id = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>0)")
    nonnegative_id = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>=0)")
    nonnegative = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>=0)")
    integer = lambda name: f"(typeof({name})='integer')"  # noqa: E731
    boolean = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name} IN (0,1))")
    clean_text = lambda name, nonempty=True: (  # noqa: E731
        f"(typeof({name})='text'"
        + (f" AND {name}!=''" if nonempty else "")
        + f" AND instr({name},char(0))=0)")

    status_values = (
        "'parsed','indexed','binary','symlink','skipped_binary',"
        "'skipped_oversize','read_error','parse_error'"
    )
    symbol_values = (
        "'function','syscall','struct','union','enum','typedef','macro',"
        "'variable','prototype'"
    )
    resolution_values = (
        "'same_file','included_source','unique_global','ambiguous','macro',"
        "'indirect','unresolved'"
    )
    member_kind_values = (
        "'field','function_pointer','struct','union','struct_group',"
        "'unnamed_bitfield','macro'"
    )
    subsystem_lists = (
        "maintainers", "reviewers", "lists", "trees", "websites",
        "patchwork", "bugs", "chats", "profiles", "keywords",
    )
    checks = {
        "dirs": " AND ".join((
            integer_id("id"), clean_text("path", nonempty=False),
            clean_text("name"),
            "(parent_id IS NULL OR " + integer_id("parent_id") + ")",
            nonnegative("depth"), nonnegative("n_files"),
            nonnegative("n_subdirs"), nonnegative("n_files_recursive"),
        )),
        "files": " AND ".join((
            integer_id("id"), clean_text("path"), integer_id("dir_id"),
            clean_text("name"), clean_text("ext", nonempty=False),
            nonnegative("size"),
            nonnegative("lines"), nonnegative("n_symbols"),
            boolean("is_symlink"), text_or_null("link_target"),
            f"(typeof(index_status)='text' AND index_status IN ({status_values}))",
            text_or_null("index_error"), clean_text("call_domain"),
        )),
        "symbols": " AND ".join((
            integer_id("id"), integer_id("file_id"), clean_text("name"),
            f"(typeof(kind)='text' AND kind IN ({symbol_values}))",
            "(typeof(start_line)='integer' AND start_line>=1)",
            "(typeof(end_line)='integer' AND end_line>=start_line)",
            text_or_null("signature"), text_or_null("summary"),
            text_or_null("description"), boolean("is_static"),
            boolean("is_inline"), boolean("is_exported"),
            boolean("is_anonymous"), boolean("parse_complete"),
            clean_text("parse_warnings", nonempty=False),
            clean_text("unmatched_member_docs", nonempty=False),
            clean_text("conditions", nonempty=False),
        )),
        "type_aliases": " AND ".join((
            integer_id("symbol_id"), clean_text("name"),
        )),
        "type_members": " AND ".join((
            integer_id("id"), integer_id("symbol_id"),
            "(parent_id IS NULL OR " + integer_id("parent_id") + ")",
            nonnegative("ordinal"),
            "(name IS NULL OR " + clean_text("name") + ")",
            f"(typeof(kind)='text' AND kind IN ({member_kind_values}))",
            text_or_null("type_text"), clean_text("declaration"),
            "(typeof(start_line)='integer' AND start_line>=1)",
            "(typeof(end_line)='integer' AND end_line>=start_line)",
            text_or_null("bit_width"), clean_text("array_dimensions", False),
            text_or_null("description"),
            "(description_source IS NULL OR (typeof(description_source)='text'"
            " AND description_source IN ('kernel-doc','inline-kernel-doc',"
            "'source-comment','macro-semantics'))) ",
            "((description IS NULL)=(description_source IS NULL))",
            clean_text("conditions", False),
            "(typeof(visibility)='text' AND visibility IN "
            "('unspecified','public','private'))",
            boolean("is_anonymous"), text_or_null("generated_by"),
        )),
        "subsystems": " AND ".join((
            nonnegative_id("id"), clean_text("name"), text_or_null("status"),
            *(text_or_null(name) for name in subsystem_lists),
            nonnegative("n_files"), nonnegative("n_primary_files"),
            "n_primary_files<=n_files",
        )),
        "path_subsys": " AND ".join((
            "(typeof(ref_kind)='text' AND ref_kind='file')",
            integer_id("ref_id"), nonnegative_id("subsystem_id"),
            integer("score"), nonnegative("rank"),
            boolean("is_primary"),
        )),
        "dir_subsys": " AND ".join((
            integer_id("dir_id"), nonnegative_id("subsystem_id"),
            nonnegative("n_claimed"), nonnegative("n_primary"),
            "n_primary<=n_claimed",
            "(typeof(coverage) IN ('integer','real')"
            " AND coverage>=0.0 AND coverage<=1.0)",
            nonnegative("rank"),
        )),
        "source_includes": " AND ".join((
            integer_id("includer_id"), integer_id("included_id"),
            "(typeof(line)='integer' AND line>=1)",
        )),
        "translation_unit_roots": integer_id("file_id"),
        "calls": " AND ".join((
            integer_id("caller_id"), clean_text("callee"),
            "(callee_id IS NULL OR " + integer_id("callee_id") + ")",
            f"(typeof(resolution)='text'"
            f" AND resolution IN ({resolution_values}))",
        )),
    }
    for table, predicate in checks.items():
        bad = conn.execute(
            f"SELECT rowid FROM {table} "
            f"WHERE NOT COALESCE(({predicate}),0) LIMIT 1"
        ).fetchone()
        if bad is not None:
            raise SchemaError(
                f"index table {table} contains an invalid value at row "
                f"{bad[0]}")

    for row in conn.execute(
            "SELECT id," + ",".join(subsystem_lists) + " FROM subsystems"):
        for field in subsystem_lists:
            if row[field] is None:
                continue
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SchemaError(
                    f"index subsystem {row['id']} has invalid {field} metadata"
                ) from exc
            if not isinstance(value, list) \
                    or any(not isinstance(item, str) for item in value):
                raise SchemaError(
                    f"index subsystem {row['id']} has invalid {field} metadata")

    for row in conn.execute(
            "SELECT id,parse_complete,parse_warnings,unmatched_member_docs,conditions"
            " FROM symbols WHERE parse_complete!=1 OR parse_warnings!='[]'"
            " OR unmatched_member_docs!='{}' OR conditions!='[]'"):
        try:
            warnings = json.loads(row["parse_warnings"])
            unmatched = json.loads(row["unmatched_member_docs"])
            conditions = json.loads(row["conditions"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaError(
                f"index symbol {row['id']} has invalid structure metadata") from exc
        if not isinstance(warnings, list) or any(
                not isinstance(value, str) for value in warnings):
            raise SchemaError(
                f"index symbol {row['id']} has invalid parse warnings")
        if bool(row["parse_complete"]) != (len(warnings) == 0):
            raise SchemaError(
                f"index symbol {row['id']} has inconsistent parse completeness")
        if not isinstance(unmatched, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in unmatched.items()):
            raise SchemaError(
                f"index symbol {row['id']} has invalid unmatched member docs")
        if not isinstance(conditions, list) or any(
                not isinstance(value, str) for value in conditions):
            raise SchemaError(
                f"index symbol {row['id']} has invalid conditions")

    for row in conn.execute(
            "SELECT id,array_dimensions,conditions FROM type_members"
            " WHERE array_dimensions!='[]' OR conditions!='[]'"):
        for field in ("array_dimensions", "conditions"):
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SchemaError(
                    f"index member {row['id']} has invalid {field}") from exc
            if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value):
                raise SchemaError(
                    f"index member {row['id']} has invalid {field}")


def _valid_index_path(path: str, *, root: bool = False) -> bool:
    """Whether a stored source identity is normalized relative POSIX text."""
    if root and path == "":
        return True
    if not path or path.startswith("/") or path.endswith("/") \
            or "\\" in path or "\0" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _validate_deep_structure(conn: sqlite3.Connection,
                             meta: dict[str, str]) -> None:
    """Audit row counts, path topology, parse state, and ownership rollups."""
    _validate_row_domains(conn)
    table_counts = {
        "n_dirs": "dirs", "n_files": "files", "n_symbols": "symbols",
        "n_type_aliases": "type_aliases",
        "n_type_members": "type_members",
        "n_subsystems": "subsystems", "n_calls": "calls",
    }
    for key, table in table_counts.items():
        actual = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if actual != int(meta[key]):
            raise SchemaError(
                f"index {table} row count disagrees with {key} metadata")

    foreign_error = next(iter(conn.execute("PRAGMA foreign_key_check")), None)
    if foreign_error is not None:
        raise SchemaError(
            f"index has a dangling reference in table {foreign_error[0]!r}")

    # Do not assume a third-party database retained the declared FK/UNIQUE
    # constraints merely because it advertises the current column layout.
    reference_checks = (
        ("directory parent", "SELECT 1 FROM dirs child LEFT JOIN dirs parent"
         " ON parent.id=child.parent_id WHERE child.parent_id IS NOT NULL"
         " AND parent.id IS NULL LIMIT 1"),
        ("file directory", "SELECT 1 FROM files f LEFT JOIN dirs d"
         " ON d.id=f.dir_id WHERE d.id IS NULL LIMIT 1"),
        ("symbol file", "SELECT 1 FROM symbols s LEFT JOIN files f"
         " ON f.id=s.file_id WHERE f.id IS NULL LIMIT 1"),
        ("type alias", "SELECT 1 FROM type_aliases a LEFT JOIN symbols s"
         " ON s.id=a.symbol_id WHERE s.id IS NULL LIMIT 1"),
        ("type member", "SELECT 1 FROM type_members m LEFT JOIN symbols s"
         " ON s.id=m.symbol_id LEFT JOIN type_members p ON p.id=m.parent_id"
         " WHERE s.id IS NULL OR (m.parent_id IS NOT NULL AND p.id IS NULL)"
         " LIMIT 1"),
        ("file ownership", "SELECT 1 FROM path_subsys p LEFT JOIN files f"
         " ON f.id=p.ref_id LEFT JOIN subsystems s ON s.id=p.subsystem_id"
         " WHERE f.id IS NULL OR s.id IS NULL LIMIT 1"),
        ("directory ownership", "SELECT 1 FROM dir_subsys p LEFT JOIN dirs d"
         " ON d.id=p.dir_id LEFT JOIN subsystems s ON s.id=p.subsystem_id"
         " WHERE d.id IS NULL OR s.id IS NULL LIMIT 1"),
        ("source inclusion", "SELECT 1 FROM source_includes edge"
         " LEFT JOIN files a ON a.id=edge.includer_id"
         " LEFT JOIN files b ON b.id=edge.included_id"
         " WHERE a.id IS NULL OR b.id IS NULL LIMIT 1"),
        ("translation-unit root", "SELECT 1 FROM translation_unit_roots root"
         " LEFT JOIN files f ON f.id=root.file_id"
         " WHERE f.id IS NULL LIMIT 1"),
        ("call", "SELECT 1 FROM calls c LEFT JOIN symbols caller"
         " ON caller.id=c.caller_id LEFT JOIN symbols target"
         " ON target.id=c.callee_id WHERE caller.id IS NULL"
         " OR (c.callee_id IS NOT NULL AND target.id IS NULL) LIMIT 1"),
    )
    for label, sql in reference_checks:
        if conn.execute(sql).fetchone() is not None:
            raise SchemaError(f"index has a dangling {label} reference")

    identity_checks = (
        ("directory", "dirs", "id"), ("directory path", "dirs", "path"),
        ("file", "files", "id"), ("file path", "files", "path"),
        ("symbol", "symbols", "id"),
        ("type alias", "type_aliases", "symbol_id,name"),
        ("type member", "type_members", "id"),
        ("type member ordinal", "type_members", "symbol_id,ordinal"),
        ("subsystem", "subsystems", "id"),
        ("subsystem name", "subsystems", "name"),
        ("file ownership", "path_subsys", "ref_kind,ref_id,subsystem_id"),
        ("directory ownership", "dir_subsys", "dir_id,subsystem_id"),
        ("source inclusion", "source_includes", "includer_id,included_id"),
        ("translation-unit root", "translation_unit_roots", "file_id"),
        ("call", "calls", "caller_id,callee"),
    )
    for label, table, columns in identity_checks:
        duplicate = conn.execute(
            f"SELECT 1 FROM {table} GROUP BY {columns}"
            " HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if duplicate is not None:
            raise SchemaError(f"index contains a duplicate {label} identity")

    dirs = conn.execute(
        "SELECT id,path,parent_id,name,depth,n_files,n_subdirs,"
        " n_files_recursive FROM dirs").fetchall()
    roots = [row for row in dirs if row["path"] == ""]
    if len(roots) != 1 or roots[0]["parent_id"] is not None \
            or roots[0]["depth"] != 0:
        raise SchemaError("index must contain exactly one valid kernel root")
    by_dir_id = {row["id"]: row for row in dirs}
    by_dir_path = {row["path"]: row for row in dirs}
    if len(by_dir_id) != len(dirs) or len(by_dir_path) != len(dirs):
        raise SchemaError("index contains duplicate directory identities")

    actual_files = {row["id"]: 0 for row in dirs}
    actual_subdirs = {row["id"]: 0 for row in dirs}
    actual_recursive = {row["id"]: 0 for row in dirs}
    for row in dirs:
        if not _valid_index_path(row["path"], root=True):
            raise SchemaError(
                f"index has an unsafe directory identity {row['path']!r}")
        if not row["path"]:
            continue
        expected_parent = row["path"].rpartition("/")[0]
        parent = by_dir_id.get(row["parent_id"])
        if (parent is None or parent["path"] != expected_parent
                or row["name"] != row["path"].rsplit("/", 1)[-1]
                or row["depth"] != row["path"].count("/") + 1):
            raise SchemaError(
                f"index has an inconsistent directory identity {row['path']!r}")
        actual_subdirs[parent["id"]] += 1

    allowed_status = {
        "parsed", "indexed", "binary", "symlink", "skipped_binary",
        "skipped_oversize", "read_error", "parse_error",
    }
    files = conn.execute(
        "SELECT id,path,dir_id,name,ext,lines,n_symbols,is_symlink,link_target,"
        " index_status,index_error FROM files"
    ).fetchall()
    failed = skipped = oversized = symlinks = 0
    for row in files:
        if not _valid_index_path(row["path"]):
            raise SchemaError(
                f"index has an unsafe file identity {row['path']!r}")
        parent = by_dir_id.get(row["dir_id"])
        expected_parent = row["path"].rpartition("/")[0]
        if (parent is None or parent["path"] != expected_parent
                or row["name"] != row["path"].rsplit("/", 1)[-1]):
            raise SchemaError(
                f"index has an inconsistent file identity {row['path']!r}")
        expected_ext = Path(row["name"]).suffix.lower()
        if row["ext"] != expected_ext:
            raise SchemaError(
                f"index file {row['path']!r} has inconsistent extension metadata")
        if row["index_status"] not in allowed_status:
            raise SchemaError(
                f"index file {row['path']!r} has invalid or unfinished status")
        if row["is_symlink"] not in (0, 1) \
                or bool(row["is_symlink"]) != (row["index_status"] == "symlink"):
            raise SchemaError(
                f"index file {row['path']!r} has inconsistent symlink state")
        if not row["is_symlink"] and row["link_target"] is not None:
            raise SchemaError(
                f"index file {row['path']!r} has an unexpected link target")
        parse_ext = row["ext"] in {".c", ".h"}
        valid_states = ({"parsed", "skipped_binary", "skipped_oversize",
                         "read_error", "parse_error"} if parse_ext else
                        {"indexed", "binary", "read_error"})
        if not row["is_symlink"] and row["index_status"] not in valid_states:
            raise SchemaError(
                f"index file {row['path']!r} has a status incompatible with"
                " its extension")
        if row["index_status"] in {"read_error", "parse_error"}:
            if not row["index_error"]:
                raise SchemaError(
                    f"index file {row['path']!r} is missing its parse error")
        elif not row["is_symlink"] and row["index_error"] is not None:
            raise SchemaError(
                f"index file {row['path']!r} has an unexpected parse error")
        actual_files[parent["id"]] += 1
        current = parent
        seen: set[int] = set()
        while current is not None:
            if current["id"] in seen:
                raise SchemaError("index directory parent graph contains a cycle")
            seen.add(current["id"])
            actual_recursive[current["id"]] += 1
            current = by_dir_id.get(current["parent_id"])
        failed += row["index_status"] in {"read_error", "parse_error"}
        oversized += row["index_status"] == "skipped_oversize"
        skipped += row["index_status"] in {"skipped_binary", "skipped_oversize"}
        if row["is_symlink"]:
            symlinks += 1
            if Path(row["name"]).suffix.lower() in {".c", ".h"}:
                skipped += 1

    for row in dirs:
        did = row["id"]
        if (row["n_files"] != actual_files[did]
                or row["n_subdirs"] != actual_subdirs[did]
                or row["n_files_recursive"] != actual_recursive[did]):
            raise SchemaError(
                f"index directory rollup is inconsistent for {row['path']!r}")

    actual_symbol_counts = {
        row["file_id"]: int(row["n"])
        for row in conn.execute(
            "SELECT file_id,COUNT(*) AS n FROM symbols GROUP BY file_id")
    }
    for row in files:
        if row["n_symbols"] != actual_symbol_counts.get(row["id"], 0):
            raise SchemaError(
                f"index symbol rollup is inconsistent for {row['path']!r}")
    bad_symbol_line = conn.execute(
        "SELECT f.path FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE s.end_line>f.lines OR f.ext NOT IN ('.c','.h')"
        " OR f.index_status!='parsed' LIMIT 1"
    ).fetchone()
    if bad_symbol_line is not None:
        raise SchemaError(
            f"index symbol identity is incompatible with file metadata for "
            f"{bad_symbol_line['path']!r}")

    alias_kind_params = ",".join("?" for _ in TYPE_ALIAS_KINDS)
    bad_alias = conn.execute(
        "SELECT a.name AS alias,s.kind,s.name,f.path"
        " FROM type_aliases a JOIN symbols s"
        " ON s.id=a.symbol_id"
        " JOIN files f ON f.id=s.file_id"
        f" WHERE s.kind NOT IN ({alias_kind_params}) LIMIT 1",
        TYPE_ALIAS_KINDS,
    ).fetchone()
    if bad_alias is not None:
        raise SchemaError(
            f"index has type alias {bad_alias['alias']!r} attached to "
            f"unsupported symbol kind {bad_alias['kind']!r} at "
            f"{bad_alias['path']}:{bad_alias['name']}")
    bad_member = conn.execute(
        "SELECT m.id FROM type_members m JOIN symbols s ON s.id=m.symbol_id"
        " LEFT JOIN type_members p ON p.id=m.parent_id"
        " WHERE s.kind NOT IN ('struct','union')"
        " OR m.start_line<s.start_line OR m.end_line>s.end_line"
        " OR (m.parent_id IS NOT NULL AND (p.symbol_id!=m.symbol_id"
        " OR p.ordinal>=m.ordinal OR p.kind NOT IN "
        " ('struct','union','struct_group','macro')"
        " OR m.start_line<p.start_line OR m.end_line>p.end_line)) LIMIT 1"
    ).fetchone()
    if bad_member is not None:
        raise SchemaError("index has an inconsistent aggregate-member identity")
    bad_member_order = conn.execute(
        "SELECT symbol_id FROM type_members GROUP BY symbol_id"
        " HAVING MIN(ordinal)!=0 OR MAX(ordinal)!=COUNT(*)-1"
        " OR COUNT(DISTINCT ordinal)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_member_order is not None:
        raise SchemaError("index aggregate-member ordinals are not contiguous")

    # Parent ids form a preorder forest.  Once traversal leaves a container's
    # subtree it may never re-enter it; otherwise query reconstruction moves a
    # later root underneath an earlier field despite contiguous ordinals.
    current_symbol_id: int | None = None
    parents: dict[int, int | None] = {}
    previous_chain: list[int] = []
    closed: set[int] = set()
    for row in conn.execute(
            "SELECT symbol_id,id,parent_id FROM type_members"
            " ORDER BY symbol_id,ordinal"):
        if row["symbol_id"] != current_symbol_id:
            current_symbol_id = row["symbol_id"]
            parents.clear()
            previous_chain.clear()
            closed.clear()
        chain: list[int] = []
        current = row["parent_id"]
        seen: set[int] = set()
        while current is not None:
            if current in seen or current not in parents:
                raise SchemaError(
                    "index has an inconsistent aggregate-member hierarchy")
            seen.add(current)
            chain.append(current)
            current = parents[current]
        chain.reverse()
        if any(ancestor in closed for ancestor in chain):
            raise SchemaError(
                "index aggregate-member preorder is not contiguous")
        common = 0
        while common < min(len(previous_chain), len(chain)) \
                and previous_chain[common] == chain[common]:
            common += 1
        closed.update(previous_chain[common:])
        parents[row["id"]] = row["parent_id"]
        previous_chain = [*chain, row["id"]]

    recorded_states = {
        "n_parse_skipped": skipped, "n_parse_failed": failed,
        "n_oversize": oversized, "n_symlinks": symlinks,
    }
    for key, actual in recorded_states.items():
        if actual != int(meta[key]):
            raise SchemaError(f"index file states disagree with {key} metadata")

    bad_primary = conn.execute(
        "SELECT ref_id FROM ("
        " SELECT p.*,MAX(score) OVER (PARTITION BY ref_id) AS max_score"
        " FROM path_subsys p)"
        " WHERE is_primary != (score=max_score) LIMIT 1"
    ).fetchone()
    if bad_primary is not None:
        raise SchemaError("index has inconsistent co-primary ownership evidence")
    bad_rank = conn.execute(
        "SELECT ref_id FROM path_subsys GROUP BY ref_id"
        " HAVING MIN(rank)!=0 OR MAX(rank)!=COUNT(*)-1"
        " OR COUNT(DISTINCT rank)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_rank is not None:
        raise SchemaError("index has inconsistent file-ownership ranks")
    bad_rank_order = conn.execute(
        "SELECT ref_id FROM ("
        " SELECT p.ref_id,p.rank,ROW_NUMBER() OVER ("
        " PARTITION BY p.ref_id ORDER BY p.score DESC,s.name,s.id)-1 expected"
        " FROM path_subsys p JOIN subsystems s ON s.id=p.subsystem_id)"
        " WHERE rank!=expected LIMIT 1"
    ).fetchone()
    if bad_rank_order is not None:
        raise SchemaError("index file-ownership ranks disagree with evidence")

    bad_subsystem = conn.execute(
        "SELECT s.name FROM subsystems s"
        " LEFT JOIN (SELECT subsystem_id,COUNT(*) AS claimed,"
        " SUM(is_primary) AS primary_n FROM path_subsys GROUP BY subsystem_id) p"
        " ON p.subsystem_id=s.id"
        " WHERE s.n_files!=COALESCE(p.claimed,0)"
        " OR s.n_primary_files!=COALESCE(p.primary_n,0) LIMIT 1"
    ).fetchone()
    if bad_subsystem is not None:
        raise SchemaError(
            f"index subsystem rollup is inconsistent for {bad_subsystem['name']!r}")

    bad_directory = conn.execute(
        "SELECT d.path FROM dir_subsys p JOIN dirs d ON d.id=p.dir_id"
        " WHERE p.n_claimed<0 OR p.n_primary<0 OR p.n_primary>p.n_claimed"
        " OR p.n_claimed>d.n_files_recursive"
        " OR ABS(p.coverage-CASE WHEN d.n_files_recursive=0 THEN 0.0"
        "   ELSE 1.0*p.n_primary/d.n_files_recursive END)>1e-12 LIMIT 1"
    ).fetchone()
    if bad_directory is not None:
        raise SchemaError(
            f"index directory ownership is inconsistent for "
            f"{bad_directory['path']!r}")
    bad_directory_rank = conn.execute(
        "SELECT dir_id FROM dir_subsys GROUP BY dir_id"
        " HAVING MIN(rank)!=0 OR MAX(rank)!=COUNT(*)-1"
        " OR COUNT(DISTINCT rank)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_directory_rank is not None:
        raise SchemaError("index has inconsistent directory-ownership ranks")
    bad_directory_order = conn.execute(
        "SELECT dir_id FROM ("
        " SELECT p.dir_id,p.rank,ROW_NUMBER() OVER ("
        " PARTITION BY p.dir_id ORDER BY (s.name='THE REST'),"
        " p.n_primary DESC,p.n_claimed DESC,s.name,s.id)-1 expected"
        " FROM dir_subsys p JOIN subsystems s ON s.id=p.subsystem_id)"
        " WHERE rank!=expected LIMIT 1"
    ).fetchone()
    if bad_directory_order is not None:
        raise SchemaError(
            "index directory-ownership ranks disagree with composition")

    # Recompute every directory/subsystem aggregate from file claims.  This is
    # the central evidence used by info, listings, and relationship queries.
    bad_rollup = conn.execute(
        "WITH RECURSIVE ancestry(file_id,dir_id) AS ("
        " SELECT id,dir_id FROM files UNION ALL"
        " SELECT a.file_id,d.parent_id FROM ancestry a"
        " JOIN dirs d ON d.id=a.dir_id WHERE d.parent_id IS NOT NULL),"
        " actual AS (SELECT a.dir_id,p.subsystem_id,COUNT(*) AS claimed,"
        " SUM(p.is_primary) AS primary_n FROM ancestry a"
        " JOIN path_subsys p ON p.ref_id=a.file_id"
        " GROUP BY a.dir_id,p.subsystem_id),"
        " mismatch AS ("
        " SELECT a.dir_id FROM actual a LEFT JOIN dir_subsys d"
        " ON d.dir_id=a.dir_id AND d.subsystem_id=a.subsystem_id"
        " WHERE d.dir_id IS NULL OR d.n_claimed!=a.claimed"
        " OR d.n_primary!=a.primary_n"
        " UNION ALL"
        " SELECT d.dir_id FROM dir_subsys d LEFT JOIN actual a"
        " ON a.dir_id=d.dir_id AND a.subsystem_id=d.subsystem_id"
        " WHERE a.dir_id IS NULL) SELECT dir_id FROM mismatch LIMIT 1"
    ).fetchone()
    if bad_rollup is not None:
        raise SchemaError("index directory ownership rollups disagree with files")

    bad_include = conn.execute(
        "SELECT edge.includer_id FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE edge.includer_id=edge.included_id OR edge.line<1"
        " OR edge.line>parent.lines"
        " OR parent.ext IS NOT '.c' OR member.ext IS NOT '.c'"
        " OR parent.index_status IS NOT 'parsed'"
        " OR member.index_status IS NOT 'parsed' LIMIT 1"
    ).fetchone()
    if bad_include is not None:
        raise SchemaError("index has an invalid C-source inclusion edge")
    bad_unit_root = conn.execute(
        "SELECT root.file_id FROM translation_unit_roots root"
        " JOIN files f ON f.id=root.file_id"
        " WHERE f.ext!='.c' OR f.index_status!='parsed' LIMIT 1"
    ).fetchone()
    if bad_unit_root is not None:
        raise SchemaError("index has an invalid translation-unit root")
    include_cycle = conn.execute(
        "WITH RECURSIVE reach(origin,current) AS ("
        " SELECT includer_id,included_id FROM source_includes UNION"
        " SELECT reach.origin,edge.included_id FROM reach"
        " JOIN source_includes edge ON edge.includer_id=reach.current)"
        " SELECT origin FROM reach WHERE origin=current LIMIT 1"
    ).fetchone()
    if include_cycle is not None:
        raise SchemaError("index C-source inclusion graph contains a cycle")


def validate_schema(conn: sqlite3.Connection, *, deep: bool = False,
                    reuse_call_evidence: bool = False) -> dict[str, str]:
    """Validate a completed index and return its metadata.

    Keeping this explicit lets callers inspect or repair arbitrary SQLite files
    when needed, while normal CLI open paths can reject stale, future, corrupt,
    or interrupted indexes before printing partial results.  ``deep=True``
    additionally scans every call edge; normal interactive opens deliberately
    avoid imposing that multi-million-row audit on each command.
    """
    try:
        raw_meta = conn.execute("SELECT key,value FROM meta").fetchall()
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"missing or unreadable metadata table: {exc}") from exc

    meta: dict[str, str] = {}
    for row in raw_meta:
        key, value = row[0], row[1]
        if not isinstance(key, str) or not key or "\0" in key \
                or not isinstance(value, str) or "\0" in value:
            raise SchemaError(
                f"index metadata {key!r} must contain text keys and values")
        if key in meta:
            raise SchemaError(f"index contains duplicate metadata key {key!r}")
        meta[key] = value

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
        "n_dirs", "n_files", "n_symbols", "n_type_aliases",
        "n_type_members", "n_subsystems", "n_calls",
        "n_calls_resolved", "n_calls_ambiguous", "n_calls_macro",
        "n_calls_indirect", "n_calls_unresolved",
        "n_parse_skipped", "n_parse_failed", "n_oversize", "n_symlinks",
        "build_seconds",
    }
    missing_meta = sorted(required_meta - meta.keys())
    if missing_meta:
        raise SchemaError(
            "index is missing metadata field(s): " + ", ".join(missing_meta))
    for key in ("source", "tree_path", "built_at", "kinds"):
        if not meta[key]:
            raise SchemaError(f"index metadata {key} must not be empty")
    try:
        datetime.fromisoformat(meta["built_at"])
    except ValueError as exc:
        raise SchemaError("index metadata built_at is not an ISO timestamp") from exc
    allowed_kinds = {
        "function", "syscall", "struct", "union", "enum", "typedef",
        "macro", "variable", "prototype",
    }
    kinds = meta["kinds"].split(",")
    if any(kind not in allowed_kinds for kind in kinds) \
            or len(kinds) != len(set(kinds)):
        raise SchemaError("index metadata kinds is invalid or contains duplicates")
    for key in ("n_dirs", "n_files", "n_symbols", "n_type_aliases",
                "n_type_members", "n_subsystems", "n_calls",
                "n_calls_resolved", "n_calls_ambiguous", "n_calls_macro",
                "n_calls_indirect", "n_calls_unresolved",
                "n_parse_skipped", "n_parse_failed", "n_oversize",
                "n_symlinks"):
        value = meta.get(key)
        if value is not None and re.fullmatch(r"[0-9]+", value) is None:
            raise SchemaError(f"index metadata {key} is not a non-negative integer")
        if value is not None and (len(value) > 19
                                  or int(value) > 2**63 - 1):
            raise SchemaError(f"index metadata {key} exceeds SQLite limits")
    if "has_calls" in meta and meta["has_calls"] not in {"0", "1"}:
        raise SchemaError("index metadata has_calls must be 0 or 1")
    if meta.get("has_calls") == "1" and (
            not ({"function", "syscall"} & set(kinds))
            or not {"macro", "variable"} <= set(kinds)):
        raise SchemaError(
            "call indexes require callable, macro, and variable kinds")
    if all(key in meta for key in (
            "n_calls", "n_calls_resolved", "n_calls_ambiguous",
            "n_calls_macro", "n_calls_indirect", "n_calls_unresolved")):
        total = int(meta["n_calls"])
        classified = sum(int(meta[key]) for key in (
            "n_calls_resolved", "n_calls_ambiguous", "n_calls_macro",
            "n_calls_indirect", "n_calls_unresolved"))
        if classified != total:
            raise SchemaError(
                "index call-resolution counts do not add up to n_calls")
        if meta.get("has_calls") == "0" and total:
            raise SchemaError("index without a call graph reports call edges")
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
                 "n_subdirs", "n_files_recursive"},
        "files": {"id", "path", "dir_id", "name", "ext", "size", "lines",
                  "n_symbols", "is_symlink", "link_target", "index_status",
                  "index_error", "call_domain"},
        "symbols": {"id", "file_id", "name", "kind", "start_line", "end_line",
                    "signature", "summary", "description", "is_static",
                    "is_inline", "is_exported", "is_anonymous",
                    "parse_complete", "parse_warnings",
                    "unmatched_member_docs", "conditions"},
        "type_aliases": {"symbol_id", "name"},
        "type_members": {"id", "symbol_id", "parent_id", "ordinal", "name",
                         "kind", "type_text", "declaration", "start_line",
                         "end_line", "bit_width", "array_dimensions",
                         "description", "description_source", "conditions",
                         "visibility", "is_anonymous", "generated_by"},
        "subsystems": {"id", "name", "status", "maintainers", "reviewers",
                       "lists", "trees", "websites", "patchwork", "bugs",
                       "chats", "profiles", "keywords", "n_files",
                       "n_primary_files"},
        "path_subsys": {"ref_kind", "ref_id", "subsystem_id", "score", "rank",
                         "is_primary"},
        "dir_subsys": {"dir_id", "subsystem_id", "n_claimed", "n_primary",
                       "coverage", "rank"},
        "source_includes": {"includer_id", "included_id", "line"},
        "translation_unit_roots": {"file_id"},
        "calls": {"caller_id", "callee", "callee_id", "resolution"},
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

    if not deep:
        return meta

    # Deep checks deliberately pay the full row-scan cost once: publication
    # and the explicit ``check`` command must validate the evidence queried by
    # every other command, not merely the surface table layout.
    try:
        _validate_deep_structure(conn, meta)
        actual_calls = {row["resolution"]: int(row["n"])
                        for row in conn.execute(
                            "SELECT resolution,COUNT(*) AS n FROM calls "
                            "GROUP BY resolution")}
        allowed_resolutions = {
            "same_file", "included_source", "unique_global", "ambiguous",
            "macro", "indirect", "unresolved",
        }
        if set(actual_calls) - allowed_resolutions:
            raise SchemaError("index has an unknown call-resolution state")
        expected_calls = {
            "same_file": None,
            "included_source": None,
            "unique_global": None,
            "ambiguous": int(meta["n_calls_ambiguous"]),
            "macro": int(meta["n_calls_macro"]),
            "indirect": int(meta["n_calls_indirect"]),
            "unresolved": int(meta["n_calls_unresolved"]),
        }
        resolved = int(meta["n_calls_resolved"])
        actual_resolved = (actual_calls.get("same_file", 0)
                           + actual_calls.get("included_source", 0)
                           + actual_calls.get("unique_global", 0))
        if actual_resolved != resolved:
            raise SchemaError(
                "index call rows disagree with n_calls_resolved metadata")
        for status, expected in expected_calls.items():
            if expected is not None and actual_calls.get(status, 0) != expected:
                raise SchemaError(
                    f"index {status} call rows disagree with metadata")
        if sum(actual_calls.values()) != int(meta["n_calls"]):
            raise SchemaError("index call row count disagrees with metadata")
        if int(meta["n_calls"]) == 0:
            if reuse_call_evidence:
                call_resolution.drop_evidence(conn)
            return meta

        if not reuse_call_evidence:
            call_resolution.prepare_evidence(conn, validating=True)
        try:
            bad_identity = conn.execute(
                "SELECT c.resolution,c.callee FROM calls c"
                " LEFT JOIN symbols caller ON caller.id=c.caller_id"
                " LEFT JOIN files caller_file ON caller_file.id=caller.file_id"
                " LEFT JOIN symbols target ON target.id=c.callee_id"
                " LEFT JOIN files target_file ON target_file.id=target.file_id"
                " WHERE c.resolution IS NULL OR c.resolution NOT IN ("
                "   'same_file','included_source','unique_global','ambiguous',"
                "   'macro','indirect','unresolved')"
                " OR c.callee IS NULL OR c.callee=''"
                " OR caller.id IS NULL OR caller_file.id IS NULL"
                " OR caller.kind IS NULL"
                " OR caller.kind NOT IN ('function','syscall')"
                " OR ((c.resolution IN ("
                "       'same_file','included_source','unique_global'))"
                "     != (c.callee_id IS NOT NULL))"
                " OR (c.callee_id IS NOT NULL AND target.id IS NULL)"
                " OR (c.resolution IN ("
                "       'same_file','included_source','unique_global') AND ("
                "      target.id IS NULL OR target_file.id IS NULL"
                "      OR target.name IS NULL OR target.name != c.callee"
                "      OR target.kind IS NULL"
                "      OR target.kind NOT IN ('function','syscall')"
                "      OR (c.resolution='same_file'"
                "          AND target.file_id != caller.file_id)"
                "      OR (c.resolution='included_source' AND ("
                "          target.file_id=caller.file_id OR NOT EXISTS ("
                "            SELECT 1 FROM translation_unit_members caller_unit"
                "            JOIN translation_unit_members target_unit"
                "              ON target_unit.unit_id=caller_unit.unit_id"
                "            WHERE caller_unit.member_file_id=caller.file_id"
                "              AND target_unit.member_file_id=target.file_id)))"
                "      OR (c.resolution='unique_global' AND ("
                "          target.file_id=caller.file_id"
                "          OR target.is_static IS NOT 0))"
                " )) LIMIT 1"
            ).fetchone()
            if bad_identity is not None:
                raise SchemaError(
                    "index has an inconsistent resolved call identity "
                    f"({bad_identity['resolution']} "
                    f"{bad_identity['callee']!r})")

            comparison = (
                " FROM calls c"
                " JOIN symbols caller ON caller.id=c.caller_id"
                " LEFT JOIN expected_call_outcomes expected"
                " ON expected.caller_file_id=caller.file_id"
                " AND expected.name=c.callee"
            )
            mismatch = (
                "expected.name IS NULL OR expected.resolution!=c.resolution"
                " OR expected.callee_id IS NOT c.callee_id"
            )
            bad_local = conn.execute(
                "SELECT c.resolution,c.callee,expected.resolution AS expected"
                + comparison
                + " WHERE c.resolution IN ('same_file','included_source') AND ("
                + mismatch + ") LIMIT 1"
            ).fetchone()
            if bad_local is not None:
                raise SchemaError(
                    "index has an impossible local call resolution "
                    f"({bad_local['resolution']} {bad_local['callee']!r})")

            bad_unique = conn.execute(
                "SELECT c.callee,expected.resolution AS expected"
                + comparison
                + " WHERE c.resolution='unique_global' AND ("
                + mismatch + ") LIMIT 1"
            ).fetchone()
            if bad_unique is not None:
                raise SchemaError(
                    "index has an impossible unique_global call resolution "
                    f"for {bad_unique['callee']!r}")

            bad_classification = conn.execute(
                "SELECT c.callee,c.resolution,expected.resolution AS expected"
                + comparison
                + " WHERE c.resolution!='indirect' AND ("
                + mismatch + ") LIMIT 1"
            ).fetchone()
            if bad_classification is not None:
                expected = bad_classification["expected"] or "no valid outcome"
                raise SchemaError(
                    "index call classification is inconsistent for "
                    f"{bad_classification['callee']!r}: recorded "
                    f"{bad_classification['resolution']}, expected {expected}")
        finally:
            call_resolution.drop_evidence(conn)
    except SchemaError:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
        raise SchemaError(f"could not validate index contents: {exc}") from exc
    return meta
