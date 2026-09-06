"""Completed-index compatibility checks and call-evidence validation."""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime

from ..indexing import call_resolution
from . import config
from .integrity import validate_structure
from .schema import SCHEMA_VERSION, SchemaError


def validate_schema(conn: sqlite3.Connection, *, deep: bool = False,
                    reuse_call_evidence: bool = False) -> dict[str, str]:
    """Validate a completed index and return its metadata.

    Keeping this explicit lets callers inspect or repair arbitrary SQLite files
    when needed, while normal CLI open paths can reject stale, future, corrupt,
    or interrupted indexes before printing partial results.  ``deep=True``
    additionally scans every call record; normal interactive opens deliberately
    avoid imposing that multi-million-row audit on each command.
    """
    meta = _validate_metadata(conn)
    _validate_layout(conn)
    if not deep:
        return meta

    # Publication and explicit checks audit the evidence queried by every
    # command; interactive opens avoid these full row scans.
    try:
        validate_structure(conn, meta)
        _validate_call_evidence(conn, meta, reuse_call_evidence=reuse_call_evidence)
    except SchemaError:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
        raise SchemaError(f"could not validate index contents: {exc}") from exc
    return meta


def _validate_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    """Read completed-build metadata and enforce its value contracts."""
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
    managed_keys = {
        "managed_tree_id", "managed_tree_device", "managed_tree_inode",
        "managed_tree_digest",
    }
    present_managed = managed_keys & meta.keys()
    if present_managed and present_managed != managed_keys:
        raise SchemaError("index has incomplete managed source identity metadata")
    if present_managed:
        if (re.fullmatch(r"[0-9a-f]{64}", meta["managed_tree_id"]) is None
                or re.fullmatch(
                    r"[0-9a-f]{64}", meta["managed_tree_digest"]) is None):
            raise SchemaError("index has invalid managed source identity metadata")
        for key in ("managed_tree_device", "managed_tree_inode"):
            if (re.fullmatch(r"[0-9]+", meta[key]) is None
                    or len(meta[key]) > 20):
                raise SchemaError(
                    f"index metadata {key} is not a valid filesystem identity")
    required_meta = {
        "source", "tree_path", "built_at", "kinds", "has_calls",
        "n_dirs", "n_files", "n_symbols", "n_type_aliases",
        "n_type_members", "n_subsystems", "n_calls",
        "n_call_occurrences",
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
                "n_call_occurrences",
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
        occurrences = int(meta["n_call_occurrences"])
        if occurrences < total:
            raise SchemaError(
                "index reports fewer call occurrences than call records")
        classified = sum(int(meta[key]) for key in (
            "n_calls_resolved", "n_calls_ambiguous", "n_calls_macro",
            "n_calls_indirect", "n_calls_unresolved"))
        if classified != total:
            raise SchemaError(
                "index call-resolution counts do not add up to n_calls")
        if meta.get("has_calls") == "0" and (total or occurrences):
            raise SchemaError(
                "index without a call graph reports call evidence")
    if "build_seconds" in meta:
        try:
            seconds = float(meta["build_seconds"])
        except (TypeError, ValueError) as exc:
            raise SchemaError("index metadata build_seconds is not numeric") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise SchemaError(
                "index metadata build_seconds must be a finite non-negative number")
    return meta


def _validate_layout(conn: sqlite3.Connection) -> None:
    """Require every table and column used by the current reader."""
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
        "calls": {"caller_id", "callee", "callee_id", "resolution",
                  "direct_count", "indirect_count", "macro_count"},
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


def _validate_call_evidence(conn: sqlite3.Connection, meta: dict[str, str], *,
                            reuse_call_evidence: bool) -> None:
    """Check call counts and rederive each recorded resolution outcome."""
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
        return

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
            + " WHERE c.direct_count>0 AND c.resolution IN "
            "('same_file','included_source') AND ("
            + mismatch + ") LIMIT 1"
        ).fetchone()
        if bad_local is not None:
            raise SchemaError(
                "index has an impossible local call resolution "
                f"({bad_local['resolution']} {bad_local['callee']!r})")

        bad_unique = conn.execute(
            "SELECT c.callee,expected.resolution AS expected"
            + comparison
            + " WHERE c.direct_count>0"
            " AND c.resolution='unique_global' AND ("
            + mismatch + ") LIMIT 1"
        ).fetchone()
        if bad_unique is not None:
            raise SchemaError(
                "index has an impossible unique_global call resolution "
                f"for {bad_unique['callee']!r}")

        bad_classification = conn.execute(
            "SELECT c.callee,c.resolution,expected.resolution AS expected"
            + comparison
            + " WHERE c.direct_count>0 AND ("
            + mismatch + ") LIMIT 1"
        ).fetchone()
        if bad_classification is not None:
            expected = bad_classification["expected"] or "no valid outcome"
            raise SchemaError(
                "index call classification is inconsistent for "
                f"{bad_classification['callee']!r}: recorded "
                f"{bad_classification['resolution']}, expected {expected}")

        bad_parser_classification = conn.execute(
            "SELECT callee,resolution FROM calls WHERE direct_count=0 AND ("
            " (macro_count>0 AND resolution!='macro') OR"
            " (macro_count=0 AND resolution!='indirect')) LIMIT 1"
        ).fetchone()
        if bad_parser_classification is not None:
            raise SchemaError(
                "index parser-supplied call classification is inconsistent "
                f"for {bad_parser_classification['callee']!r}: recorded "
                f"{bad_parser_classification['resolution']}")
    finally:
        call_resolution.drop_evidence(conn)
