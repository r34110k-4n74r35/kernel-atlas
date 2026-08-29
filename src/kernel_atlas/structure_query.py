"""Resolve and describe indexed C structure and union definitions."""

from __future__ import annotations

import json
import re
import sqlite3

from .query_models import Resolution, Target
from .query_targeting import normalize_spec, rank_candidate, symbol_target


_STRUCT_SELECT = """
SELECT s.id, s.file_id, s.name, s.kind, s.start_line, s.end_line, s.signature,
       s.is_static, s.is_inline, s.is_exported, s.summary, s.description,
       s.is_anonymous, s.parse_complete, s.conditions, f.path, f.dir_id
FROM symbols s JOIN files f ON f.id = s.file_id
"""


def _structure_target(row: sqlite3.Row) -> Target:
    target = symbol_target(row)
    target.summary = row["summary"]
    target.description = row["description"]
    target.is_anonymous = bool(row["is_anonymous"])
    target.parse_complete = bool(row["parse_complete"])
    target.conditions = tuple(json.loads(row["conditions"]))
    return target


def _structure_name_sql() -> str:
    return (
        "(s.name=? OR EXISTS (SELECT 1 FROM type_aliases a "
        "WHERE a.symbol_id=s.id AND a.name=?))"
    )


def structure_selector(conn: sqlite3.Connection, target: Target) -> str | None:
    """Return a selector that resolves only *target*, when one is expressible."""
    if target.kind != "symbol" or target.symbol_kind not in {"struct", "union"}:
        raise ValueError("target is not a struct or union definition")
    name_sql = _structure_name_sql()
    name_params = (target.file_id, target.name, target.name)
    by_name = conn.execute(
        "SELECT COUNT(*) FROM symbols s WHERE s.file_id=?"
        " AND s.kind IN ('struct','union')" f" AND {name_sql}",
        name_params,
    ).fetchone()[0]
    if by_name == 1:
        return f"{target.path}:{target.name}"
    by_kind_and_name = conn.execute(
        "SELECT COUNT(*) FROM symbols s WHERE s.file_id=? AND s.kind=?"
        f" AND {name_sql}",
        (target.file_id, target.symbol_kind, target.name, target.name),
    ).fetchone()[0]
    if by_kind_and_name == 1:
        return f"{target.symbol_kind} {target.path}:{target.name}"
    if target.line is None:
        return None
    by_line = conn.execute(
        "SELECT COUNT(*) FROM symbols s WHERE s.file_id=?"
        " AND s.kind IN ('struct','union')"
        " AND s.start_line<=? AND s.end_line>=?",
        (target.file_id, target.line, target.line),
    ).fetchone()[0]
    if by_line == 1:
        return f"{target.path}:{target.line}"
    by_kind_and_line = conn.execute(
        "SELECT COUNT(*) FROM symbols s WHERE s.file_id=? AND s.kind=?"
        " AND s.start_line<=? AND s.end_line>=?",
        (target.file_id, target.symbol_kind, target.line, target.line),
    ).fetchone()[0]
    if by_kind_and_line == 1:
        return f"{target.symbol_kind} {target.path}:{target.line}"
    return None


def _aggregate_kind_filter(kind_hint: str | None) -> tuple[str, tuple[str, ...]]:
    if kind_hint is not None:
        return "s.kind=?", (kind_hint,)
    return "s.kind IN ('struct','union')", ()


def _aggregate_ambiguity_note(candidates: list[Target], subject: str) -> str:
    if len(candidates) < 2:
        return ""
    return (
        f"{len(candidates)} aggregate definitions match {subject}; "
        "inspect the candidates and use structure_selector() where an "
        "exact command selector is expressible"
    )


def resolve_structure(conn: sqlite3.Connection, spec: str) -> Resolution:
    """Resolve struct/union definitions and their direct typedef aliases.

    Unlike generic symbol resolution, this deliberately returns every
    competing identity to its caller.  A structure report must not guess among
    configuration variants, copied UAPI definitions, or same-named tags.
    """
    raw = (spec or "").strip()
    kind_hint = None
    for candidate_kind in ("struct", "union"):
        prefix = candidate_kind + " "
        if raw.startswith(prefix):
            kind_hint = candidate_kind
            raw = raw[len(prefix):].strip()
            break
    if not raw:
        return Resolution(None, note="an aggregate name is required")
    kind_sql, kind_params = _aggregate_kind_filter(kind_hint)

    if ":" in raw:
        head, _, tail = raw.rpartition(":")
        head_n = normalize_spec(head)
        line = None
        if re.fullmatch(r"[+-]?\d+", tail):
            try:
                line = int(tail)
            except ValueError:
                return Resolution(None, note=f"line number {tail!r} is too large")
            if line < 1:
                return Resolution(None, note="line number must be at least 1")
            if line > 2**63 - 1:
                return Resolution(None, note=f"line number {tail!r} is too large")

        exact = (
            conn.execute(
                "SELECT id,path,name FROM files WHERE path=?", (head_n,)
            ).fetchone()
            if head_n
            else None
        )
        if exact is not None:
            files = [exact]
        elif head_n and "/" not in head_n and "\\" not in head_n:
            files = conn.execute(
                "SELECT id,path,name FROM files WHERE name=?"
                " ORDER BY LENGTH(path),path,id",
                (head_n,),
            ).fetchall()
        else:
            files = []

        if files and line is not None:
            ids = [row["id"] for row in files]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                _STRUCT_SELECT
                + f" WHERE {kind_sql} AND s.file_id IN ({placeholders})"
                " AND s.start_line<=? AND s.end_line>=?",
                (*kind_params, *ids, line, line),
            ).fetchall()
            candidates = sorted(
                (_structure_target(row) for row in rows),
                key=lambda target: (
                    (target.end_line or 0) - (target.line or 0),
                    *rank_candidate(target),
                ),
            )
            if candidates:
                note = (
                    _aggregate_ambiguity_note(
                        candidates,
                        f"line {line} in {head_n or 'the selected file'}",
                    )
                    or f"line {line} falls inside this aggregate definition"
                )
                return Resolution(candidates[0], candidates[1:], note)
            if exact is not None:
                return Resolution(
                    None,
                    note=f"no aggregate definition spans line {line} in {head_n}",
                )
            return Resolution(
                None,
                note=f"no aggregate definition spans line {line} in any file "
                f"named {head_n!r}",
            )

        if files and line is None:
            ids = [row["id"] for row in files]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                _STRUCT_SELECT
                + f" WHERE {kind_sql} AND s.file_id IN ({placeholders})"
                f" AND {_structure_name_sql()}",
                (*kind_params, *ids, tail, tail),
            ).fetchall()
            candidates = sorted(
                (_structure_target(row) for row in rows),
                key=rank_candidate,
            )
            if candidates:
                note = _aggregate_ambiguity_note(candidates, repr(tail))
                return Resolution(candidates[0], candidates[1:], note)
            if exact is not None:
                return Resolution(
                    None,
                    note=f"{head_n} exists but defines no aggregate named {tail!r}",
                )
            return Resolution(
                None,
                note=f"no aggregate named {tail!r} exists in any file named "
                f"{head_n!r}",
            )

    rows = conn.execute(
        _STRUCT_SELECT + f" WHERE {kind_sql} AND {_structure_name_sql()}",
        (*kind_params, raw, raw),
    ).fetchall()
    candidates = sorted(
        (_structure_target(row) for row in rows),
        key=rank_candidate,
    )
    if not candidates:
        return Resolution(
            None,
            note=f"no struct/union tag or typedef alias is named {raw!r}",
        )
    note = _aggregate_ambiguity_note(candidates, repr(raw))
    return Resolution(candidates[0], candidates[1:], note)


def structure_detail(conn: sqlite3.Connection, target: Target) -> dict:
    """Return the stable nested payload for one resolved aggregate."""
    if target.kind != "symbol" or target.symbol_kind not in {"struct", "union"}:
        raise ValueError("target is not a struct or union definition")
    symbol = conn.execute(
        "SELECT signature,summary,description,is_anonymous,parse_complete,"
        "parse_warnings, unmatched_member_docs,conditions FROM symbols WHERE id=?",
        (target.id,),
    ).fetchone()
    if symbol is None:
        raise ValueError("aggregate definition is missing from the index")

    aliases = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM type_aliases WHERE symbol_id=? ORDER BY name",
            (target.id,),
        )
    ]
    if bool(symbol["is_anonymous"]) and target.name not in aliases:
        aliases.insert(0, target.name)
    rows = conn.execute(
        "SELECT id,parent_id,ordinal,name,kind,type_text,declaration,start_line,"
        " end_line,bit_width,array_dimensions,description,description_source,"
        " conditions,visibility,is_anonymous,generated_by"
        " FROM type_members WHERE symbol_id=? ORDER BY ordinal",
        (target.id,),
    ).fetchall()

    by_id: dict[int, dict] = {}
    roots: list[dict] = []
    for row in rows:
        type_text = row["type_text"]
        referenced_kind = referenced_name = None
        reference = re.search(
            r"\b(struct|union|enum)\s+([A-Za-z_]\w*)", type_text or ""
        )
        if reference is not None:
            referenced_kind, referenced_name = reference.groups()
        dimensions = json.loads(row["array_dimensions"])
        member = {
            "ordinal": row["ordinal"],
            "name": row["name"],
            "kind": row["kind"],
            "type": type_text,
            "declaration": row["declaration"],
            "line": row["start_line"],
            "end_line": row["end_line"],
            "bit_width": row["bit_width"],
            "array_dimensions": dimensions,
            "is_flexible_array": bool(dimensions and dimensions[-1] == ""),
            "description": row["description"],
            "description_source": row["description_source"],
            "conditions": json.loads(row["conditions"]),
            "visibility": row["visibility"],
            "is_anonymous": bool(row["is_anonymous"]),
            "generated_by": row["generated_by"],
            "referenced_kind": referenced_kind,
            "referenced_name": referenced_name,
            "children": [],
        }
        by_id[row["id"]] = member
        if row["parent_id"] is None:
            roots.append(member)
        else:
            by_id[row["parent_id"]]["children"].append(member)

    direct = len(roots)
    documentable = sum(row["name"] is not None for row in rows)
    described = sum(
        row["name"] is not None and row["description"] is not None for row in rows
    )
    source_documentation = {
        "kernel-doc",
        "inline-kernel-doc",
        "source-comment",
    }
    documented = sum(
        row["name"] is not None
        and row["description_source"] in source_documentation
        for row in rows
    )
    semantic_descriptions = sum(
        row["description_source"] == "macro-semantics" for row in rows
    )
    unmatched = json.loads(symbol["unmatched_member_docs"])
    warnings = json.loads(symbol["parse_warnings"])
    selector = structure_selector(conn, target)
    anonymous = bool(symbol["is_anonymous"])
    tag = None if anonymous else target.name
    return {
        "kind": target.symbol_kind,
        "name": target.name,
        "tag": tag,
        "c_name": f"{target.symbol_kind} {tag}" if tag else None,
        "selector": selector,
        "path": target.path,
        "line": target.line,
        "end_line": target.end_line,
        "signature": symbol["signature"],
        "summary": symbol["summary"],
        "description": symbol["description"],
        "is_anonymous": anonymous,
        "aliases": aliases,
        "direct_member_count": direct,
        "total_member_count": len(rows),
        "documentable_member_count": documentable,
        "described_member_count": described,
        "documented_member_count": documented,
        "semantic_description_count": semantic_descriptions,
        "documentation_coverage": documented / documentable if documentable else 0.0,
        "parse_complete": bool(symbol["parse_complete"]),
        "warnings": warnings,
        "conditions": json.loads(symbol["conditions"]),
        "unmatched_member_docs": unmatched,
        "members": roots,
    }
