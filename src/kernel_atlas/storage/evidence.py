"""Validate optional stored call-site and documentation evidence."""

from __future__ import annotations

import json
import sqlite3

from .schema import SchemaError

CAPABILITY_TABLES = {
    "has_call_sites": (
        "call_sites",
        {"caller_id", "callee", "kind", "line", "byte_offset"},
    ),
    "has_document_text": ("document_text", {"file_id", "content"}),
    "has_function_docs": (
        "function_docs",
        {
            "symbol_id",
            "line",
            "summary",
            "parameters",
            "description",
            "context",
            "returns",
        },
    ),
}


def validate_evidence(conn, meta, *, deep=False):
    """Audit declared extensions while leaving schema-6 indexes readable."""
    for flag, (table, expected) in CAPABILITY_TABLES.items():
        if flag not in meta:
            continue
        if meta[flag] not in {"0", "1"}:
            raise SchemaError(f"invalid index capability {flag}")
        if meta[flag] != "1":
            continue
        actual = {row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")}
        if not expected <= actual:
            raise SchemaError(
                f"index capability {flag} requires {table}; rebuild the index"
            )
    if not deep:
        return
    if meta.get("has_call_sites") == "1":
        bad = conn.execute(
            "SELECT 1 FROM call_sites c LEFT JOIN symbols s ON s.id=c.caller_id "
            "LEFT JOIN calls edge ON edge.caller_id=c.caller_id AND edge.callee=c.callee "
            "WHERE s.id IS NULL OR edge.caller_id IS NULL "
            "OR s.kind NOT IN ('function','syscall') "
            "OR c.kind NOT IN ('direct','indirect','macro') "
            "OR typeof(c.line)!='integer' OR c.line<s.start_line OR c.line>s.end_line "
            "OR typeof(c.byte_offset)!='integer' OR c.byte_offset<0 LIMIT 1"
        ).fetchone()
        if bad:
            raise SchemaError("invalid call-site evidence")
        bad = conn.execute(
            "SELECT 1 FROM call_sites c JOIN symbols s ON s.id=c.caller_id "
            "JOIN files f ON f.id=s.file_id WHERE c.byte_offset>=f.size LIMIT 1"
        ).fetchone()
        if bad:
            raise SchemaError("call-site evidence is outside its source file")
        bad = conn.execute(
            "SELECT 1 FROM call_sites c JOIN calls edge "
            "ON edge.caller_id=c.caller_id AND edge.callee=c.callee "
            "GROUP BY c.caller_id,c.callee "
            "HAVING SUM(c.kind='direct')!=edge.direct_count "
            "OR SUM(c.kind='indirect')!=edge.indirect_count "
            "OR SUM(c.kind='macro')!=edge.macro_count LIMIT 1"
        ).fetchone()
        if bad:
            raise SchemaError("call-site evidence does not match occurrence counts")
    try:
        if meta.get("has_function_docs") == "1":
            for row in conn.execute(
                "SELECT d.*,s.kind,s.start_line FROM function_docs d "
                "LEFT JOIN symbols s ON s.id=d.symbol_id"
            ):
                if (
                    row["kind"] not in {"function", "syscall", "prototype"}
                    or type(row["line"]) is not int
                    or not 1 <= row["line"] <= row["start_line"]
                ):
                    raise SchemaError("invalid documented function identity")
                parameters = json.loads(row["parameters"])
                if not isinstance(parameters, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in parameters.items()
                ):
                    raise SchemaError("invalid function parameter documentation")
    except (TypeError, ValueError, sqlite3.DatabaseError) as error:
        raise SchemaError(f"invalid stored evidence: {error}") from error
