"""Evidence-backed relationships between MAINTAINERS subsystems.

Two independent signals are exposed:

* ownership overlap: sections which claim the same files;
* direct C invocation flow: resolved caller/callee identities whose files have
  disjoint primary-owner sets.

The second signal is intentionally conservative.  Name-only, macro-only and
ambiguous calls remain coverage statistics and are never promoted to an edge.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class OwnershipOverlap:
    subsystem: str
    shared_files: int
    selected_files: int
    other_files: int
    selected_coverage: float
    other_coverage: float
    jaccard: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CallFlow:
    direction: str
    subsystem: str | None
    edges: int
    callers: int
    callees: int
    source_files: int
    target_files: int
    internal: bool = False
    unclassified: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def ownership_overlaps(conn: sqlite3.Connection, subsystem_id: int, *,
                       min_files: int = 1, limit: int = 0
                       ) -> list[OwnershipOverlap]:
    """Sections sharing files with ``subsystem_id``, strongest first."""
    selected = conn.execute(
        "SELECT n_files FROM subsystems WHERE id = ?", (subsystem_id,)
    ).fetchone()
    if selected is None:
        return []
    selected_files = int(selected["n_files"])
    sql = """
        SELECT other.id, other.name, other.n_files,
               COUNT(*) AS shared_files
        FROM path_subsys chosen
        JOIN path_subsys shared
          ON shared.ref_kind = 'file'
         AND shared.ref_id = chosen.ref_id
         AND shared.subsystem_id != chosen.subsystem_id
        JOIN subsystems other ON other.id = shared.subsystem_id
        WHERE chosen.ref_kind = 'file'
          AND chosen.subsystem_id = ?
          AND other.name != 'THE REST'
        GROUP BY other.id
        HAVING COUNT(*) >= ?
        ORDER BY shared_files DESC, other.name
    """
    params: list[int] = [subsystem_id, min_files]
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    out: list[OwnershipOverlap] = []
    for row in conn.execute(sql, params):
        shared = int(row["shared_files"])
        other_files = int(row["n_files"])
        union = selected_files + other_files - shared
        out.append(OwnershipOverlap(
            subsystem=row["name"],
            shared_files=shared,
            selected_files=selected_files,
            other_files=other_files,
            selected_coverage=(shared / selected_files if selected_files else 0.0),
            other_coverage=(shared / other_files if other_files else 0.0),
            jaccard=(shared / union if union else 0.0),
        ))
    return out


def _call_flow_sql(direction: str, include_internal: bool) -> str:
    if direction == "outgoing":
        scoped_file, other_file = "caller.file_id", "callee.file_id"
    elif direction == "incoming":
        scoped_file, other_file = "callee.file_id", "caller.file_id"
    else:
        raise ValueError(f"unknown call-flow direction: {direction}")
    internal = "" if include_internal else (
        "AND (other_owner.subsystem_id IS NULL "
        "OR other_owner.subsystem_id != scoped_owner.subsystem_id)")
    return f"""
        SELECT CASE WHEN other.name IS NULL OR other.name='THE REST'
                    THEN NULL ELSE other.name END AS subsystem,
               other.name IS NULL OR other.name='THE REST' AS unclassified,
               COALESCE(other_owner.subsystem_id =
                        scoped_owner.subsystem_id, 0) AS internal,
               COUNT(*) AS edges,
               COUNT(DISTINCT c.caller_id) AS callers,
               COUNT(DISTINCT c.callee_id) AS callees,
               COUNT(DISTINCT caller.file_id) AS source_files,
               COUNT(DISTINCT callee.file_id) AS target_files
        FROM calls c
        JOIN symbols caller ON caller.id = c.caller_id
        JOIN symbols callee ON callee.id = c.callee_id
        JOIN path_subsys scoped_owner
          ON scoped_owner.ref_kind = 'file'
         AND scoped_owner.ref_id = {scoped_file}
         AND scoped_owner.is_primary = 1
        LEFT JOIN path_subsys other_owner
          ON other_owner.ref_kind = 'file'
         AND other_owner.ref_id = {other_file}
         AND other_owner.is_primary = 1
        LEFT JOIN subsystems other ON other.id = other_owner.subsystem_id
        WHERE scoped_owner.subsystem_id = ?
          AND c.callee_id IS NOT NULL
          AND c.resolution IN ('same_file', 'included_source', 'unique_global')
          AND (
            other_owner.subsystem_id = scoped_owner.subsystem_id
            OR NOT EXISTS (
              SELECT 1
              FROM path_subsys source_shared
              JOIN path_subsys target_shared
                ON target_shared.ref_kind = 'file'
               AND target_shared.ref_id = callee.file_id
               AND target_shared.is_primary = 1
               AND target_shared.subsystem_id = source_shared.subsystem_id
              WHERE source_shared.ref_kind = 'file'
                AND source_shared.ref_id = caller.file_id
                AND source_shared.is_primary = 1
            )
          )
          {internal}
        GROUP BY CASE WHEN other.name IS NULL OR other.name='THE REST'
                      THEN NULL ELSE other_owner.subsystem_id END,
                 COALESCE(other_owner.subsystem_id =
                          scoped_owner.subsystem_id, 0)
        HAVING COUNT(*) >= ?
        ORDER BY edges DESC, subsystem
    """


def call_flows(conn: sqlite3.Connection, subsystem_id: int, *,
               direction: str = "both", include_internal: bool = False,
               min_edges: int = 1, limit: int = 0) -> list[CallFlow]:
    """Aggregate resolved calls crossing disjoint primary-owner sets.

    A file may have several equally strong MAINTAINERS owners.  If the source
    and target share any of them, the call is internal to that shared boundary;
    it must not manufacture pairwise flows between their other co-owners.
    """
    directions = ("outgoing", "incoming") if direction == "both" else (direction,)
    out: list[CallFlow] = []
    for current in directions:
        params = [subsystem_id]
        params.append(min_edges)
        rows = conn.execute(
            _call_flow_sql(current, include_internal), params).fetchall()
        if limit > 0:
            rows = rows[:limit]
        out.extend(CallFlow(
            direction=current,
            subsystem=row["subsystem"],
            edges=int(row["edges"]),
            callers=int(row["callers"]),
            callees=int(row["callees"]),
            source_files=int(row["source_files"]),
            target_files=int(row["target_files"]),
            internal=bool(row["internal"]),
            unclassified=bool(row["unclassified"]),
        ) for row in rows)
    return out


def call_resolution_coverage(conn: sqlite3.Connection,
                             subsystem_id: int) -> dict[str, int]:
    """Resolution outcomes for calls originating in primary-owned files."""
    counts = {key: 0 for key in (
        "same_file", "included_source", "unique_global", "ambiguous",
        "macro", "indirect", "unresolved")}
    for row in conn.execute(
        "SELECT c.resolution, COUNT(*) AS n FROM calls c"
        " JOIN symbols caller ON caller.id = c.caller_id"
        " JOIN path_subsys owner ON owner.ref_kind='file'"
        "  AND owner.ref_id=caller.file_id AND owner.is_primary=1"
        " WHERE owner.subsystem_id=? GROUP BY c.resolution",
        (subsystem_id,),
    ):
        counts[row["resolution"]] = int(row["n"])
    counts["resolved"] = (counts["same_file"] + counts["included_source"]
                          + counts["unique_global"])
    counts["total"] = sum(counts[key] for key in (
        "same_file", "included_source", "unique_global", "ambiguous",
        "macro", "indirect", "unresolved"))
    return counts
