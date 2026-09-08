"""Explicit function contracts stored from source kernel-doc comments."""

from __future__ import annotations

import json
import sqlite3

NOTE = (
    "These are documented requirements, not verified behavioral guarantees. "
    "Only explicitly documented parameters are shown; missing fields remain unknown."
)


def contract(conn: sqlite3.Connection, symbol_id: int) -> dict:
    row = conn.execute(
        "SELECT d.* FROM function_docs d WHERE d.symbol_id=?", (symbol_id,)
    ).fetchone()
    if row is None:
        return {"documented": False, "line": None, "summary": None,
                "parameters": {}, "description": None, "context": None,
                "returns": None, "note": NOTE}
    return {
        "documented": True, "line": row["line"],
        **{field: row[field] or None
           for field in ("summary", "description", "context", "returns")},
        "parameters": json.loads(row["parameters"]), "note": NOTE,
    }
