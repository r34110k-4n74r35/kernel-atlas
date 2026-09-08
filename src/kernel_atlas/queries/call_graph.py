"""Bounded source-level call exploration using concrete symbol identities.

Only resolved direct occurrences establish traversable edges. Macro, indirect,
and ambiguous occurrences remain boundary evidence, including mixed groups
which also contain a resolved direct occurrence.
"""

from __future__ import annotations

from collections import deque
import sqlite3


SOURCE_NOTE = (
    "These are source-level relationships, not runtime execution paths. "
    "Missing paths do not prove that functions are disconnected at runtime."
)


def _node(row, prefix="") -> dict:
    return {key: row[prefix + key] for key in ("id", "name", "kind", "path", "line")}


def symbol_node(conn: sqlite3.Connection, symbol_id: int) -> dict:
    row = conn.execute(
        "SELECT s.id,s.name,s.kind,f.path,s.start_line AS line FROM symbols s "
        "JOIN files f ON f.id=s.file_id WHERE s.id=?", (symbol_id,)
    ).fetchone()
    if row is None or row["kind"] not in {"function", "syscall"}:
        raise ValueError("call exploration requires a concrete function or syscall")
    return _node(row)


def _site_rows(conn, *, caller_id=None, callee_id=None, callee=None, limit=200):
    clauses, params = [], []
    if caller_id is not None:
        clauses.append("cs.caller_id=?")
        params.append(caller_id)
    if callee_id is not None:
        # An identically spelled indirect or macro occurrence does not inherit
        # the resolved identity of another occurrence in its aggregate group.
        clauses.extend(("c.callee_id=?", "cs.kind='direct'"))
        params.append(callee_id)
    if callee is not None:
        clauses.append("cs.callee=?")
        params.append(callee)
    return conn.execute(
        "SELECT cs.caller_id,cs.callee,cs.kind,cs.line,cs.byte_offset,"
        "s.name AS caller_name,f.path,c.callee_id,c.resolution "
        "FROM call_sites cs JOIN symbols s ON s.id=cs.caller_id "
        "JOIN files f ON f.id=s.file_id "
        "JOIN calls c ON c.caller_id=cs.caller_id AND c.callee=cs.callee "
        "WHERE " + " AND ".join(clauses) +
        " ORDER BY f.path,cs.line,cs.byte_offset,cs.callee,cs.kind LIMIT ?",
        (*params, limit),
    )


def _site(row) -> dict:
    direct = row["kind"] == "direct"
    return {
        "caller_id": row["caller_id"], "caller": row["caller_name"],
        "callee": row["callee"], "kind": row["kind"],
        "path": row["path"], "line": row["line"],
        "byte_offset": row["byte_offset"],
        "callee_id": row["callee_id"] if direct else None,
        "resolution": row["resolution"] if direct else row["kind"],
    }


def call_sites(conn: sqlite3.Connection, symbol_id: int, *, incoming=False,
               limit=200) -> dict:
    """Return bounded individual occurrences, retaining unresolved outgoing sites."""
    if not 1 <= limit <= 5000:
        raise ValueError("call-site limit must be between 1 and 5000")
    node = symbol_node(conn, symbol_id)
    kwargs = {"callee_id" if incoming else "caller_id": symbol_id}
    rows = list(_site_rows(conn, **kwargs, limit=limit + 1))
    return {
        "mode": "sites", "target": node,
        "direction": "callers" if incoming else "callees",
        "sites": [_site(row) for row in rows[:limit]],
        "truncated": len(rows) > limit,
        "truncation_reasons": ["site_limit"] if len(rows) > limit else [],
        "limit": limit, "note": SOURCE_NOTE,
    }


def _adjacent(conn, symbol_id, incoming):
    column = "callee_id" if incoming else "caller_id"
    return conn.execute(
        "SELECT c.*,s.id AS caller_identity,s.name AS caller_name,"
        "s.kind AS caller_kind,s.start_line AS caller_line,f.path AS caller_path,"
        "t.name AS callee_name,t.kind AS callee_kind,t.start_line AS callee_line,"
        "g.path AS callee_path "
        "FROM calls c JOIN symbols s ON s.id=c.caller_id "
        "JOIN files f ON f.id=s.file_id "
        "LEFT JOIN symbols t ON t.id=c.callee_id "
        "LEFT JOIN files g ON g.id=t.file_id "
        f"WHERE c.{column}=? "
        "ORDER BY c.callee,f.path,s.start_line,c.caller_id", (symbol_id,)
    )


def _edge(row) -> dict:
    caller = {"id": row["caller_identity"], **{
        key: row["caller_" + key] for key in ("name", "kind", "path", "line")}}
    callee = None if row["callee_id"] is None else {
        "id": row["callee_id"], **{
            key: row["callee_" + key] for key in ("name", "kind", "path", "line")}}
    return {
        "caller": caller, "callee": callee, "name": row["callee"],
        "resolution": row["resolution"], "direct_count": row["direct_count"],
        "indirect_count": row["indirect_count"], "macro_count": row["macro_count"],
    }


def _has_cycle(edges) -> bool:
    """Detect a directed cycle without confusing converging paths with cycles."""
    adjacency: dict[int, list[int]] = {}
    for edge in edges:
        adjacency.setdefault(edge["caller"]["id"], []).append(edge["callee"]["id"])
    colors: dict[int, int] = {}
    for start in adjacency:
        if start in colors:
            continue
        colors[start] = 1
        stack = [(start, iter(adjacency.get(start, ())))]
        while stack:
            node, neighbors = stack[-1]
            other = next(neighbors, None)
            if other is None:
                colors[node] = 2
                stack.pop()
            elif colors.get(other) == 1:
                return True
            elif other not in colors:
                colors[other] = 1
                stack.append((other, iter(adjacency.get(other, ()))))
    return False


def explore(conn: sqlite3.Connection, symbol_id: int, *, depth=3,
            max_nodes=200, incoming=False, to_id=None, sites=False) -> dict:
    """Breadth-first exploration with independent node, edge, and site budgets.

    Path searches return one shortest directed chain. Limits and unresolved
    boundaries make incompleteness explicit; they never justify runtime claims.
    Incoming traversal follows reversed edges but retains their source direction.
    """
    if not 1 <= depth <= 64:
        raise ValueError("call exploration depth must be between 1 and 64")
    if not 1 <= max_nodes <= 5000:
        raise ValueError("max_nodes must be between 1 and 5000")
    if incoming and to_id is not None:
        raise ValueError("path search cannot be combined with incoming traversal")
    start = symbol_node(conn, symbol_id)
    destination = symbol_node(conn, to_id) if to_id is not None else None
    nodes = {symbol_id: {**start, "depth": 0}}
    parents: dict[int, int] = {}
    queue = deque([(symbol_id, 0)])
    edges, boundary = [], []
    reasons: set[str] = set()
    max_edges = max_nodes * 10
    site_budget = max_edges
    inspected = 0
    found = to_id == symbol_id
    stopped = False
    while queue and not found and not stopped:
        current, level = queue.popleft()
        for row in _adjacent(conn, current, incoming):
            if inspected >= max_edges:
                reasons.add("edge_limit")
                stopped = True
                break
            inspected += 1
            edge = _edge(row)
            if sites:
                occurrences = list(_site_rows(
                    conn, caller_id=row["caller_id"], callee=row["callee"],
                    limit=site_budget + 1))
                edge["sites"] = [_site(item) for item in occurrences[:site_budget]]
                if len(occurrences) > site_budget:
                    reasons.add("site_limit")
                site_budget -= min(site_budget, len(occurrences))
            if edge["callee"] is None:
                boundary.append({**edge, "reason": edge["resolution"],
                                 "depth": level + 1})
                continue
            # Mixed call groups can resolve only their direct occurrences.
            if not incoming and (edge["indirect_count"] or edge["macro_count"]):
                boundary.append({**edge, "callee": None,
                                 "reason": "non_direct_occurrences",
                                 "depth": level + 1})
            neighbor = edge["caller"] if incoming else edge["callee"]
            other = neighbor["id"]
            if other not in nodes:
                if level >= depth:
                    reasons.add("depth_limit")
                    boundary.append({**edge, "reason": "depth_limit",
                                     "depth": level + 1})
                    continue
                if len(nodes) >= max_nodes:
                    reasons.add("node_limit")
                    boundary.append({**edge, "reason": "node_limit",
                                     "depth": level + 1})
                    # No target can be admitted after the budget is exhausted.
                    stopped = True
                    break
                nodes[other] = {**neighbor, "depth": level + 1}
                parents[other] = current
                queue.append((other, level + 1))
            edges.append(edge)
            if other == to_id:
                found = True
                break
    path = []
    if found:
        current = to_id
        while current != symbol_id:
            path.append(nodes[current])
            current = parents[current]
        path.append(nodes[symbol_id])
        path.reverse()
    return {
        "mode": "path" if destination else "graph", "target": start,
        "destination": destination, "direction": "callers" if incoming else "callees",
        "nodes": list(nodes.values()), "edges": edges, "boundary": boundary,
        "path": path, "found": found if destination else None,
        "cycles_detected": _has_cycle(edges), "truncated": bool(reasons),
        "truncation_reasons": sorted(reasons),
        "limits": {"depth": depth, "max_nodes": max_nodes, "max_edges": max_edges,
                   "max_sites": max_edges if sites else None},
        "note": SOURCE_NOTE,
    }
