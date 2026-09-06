"""CLI target resolution, ambiguity diagnostics, and follow-up commands."""

from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path

from ..queries import links
from ..queries import query
from .output import PROG, _die
from .selection import _same_path, index_version, selected_index
from .source import _normalize_target_spec, find_source_tree


_SOURCE_SUFFIXES = (".c", ".h", ".S", ".rs", ".dts", ".rst")


def _suggestions(conn, spec: str, limit: int = 5) -> list[str]:
    """Nearest matches for a mistyped target.

    Path-shaped input gets file suggestions, everything else symbol
    suggestions. A couple of shortened prefixes are tried so a typo in the last
    character or two still lands somewhere useful.
    """
    probe = spec.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    looks_like_path = "/" in spec or spec.endswith(_SOURCE_SUFFIXES)
    if looks_like_path:
        # Trim the extension first, otherwise shortening can never reach the
        # misspelled part of a name like 'inodee.c'.
        probe = probe.rsplit(".", 1)[0] if "." in probe else probe
    if len(probe) < 3:
        return []

    for attempt in range(4):
        trimmed = probe[:len(probe) - attempt]
        if len(trimmed) < 3:
            break
        if looks_like_path:
            like = query.like_escape(trimmed) + "%"
            rows = conn.execute(
                "SELECT path || '/' AS p FROM dirs WHERE name LIKE ? ESCAPE '\\'"
                " UNION ALL"
                " SELECT path AS p FROM files WHERE name LIKE ? ESCAPE '\\'"
                " LIMIT ?", (like, like, limit)).fetchall()
            if rows:
                return [r["p"] for r in rows]
        else:
            mode = "substring" if attempt == 0 else "prefix"
            near = query.search(conn, trimmed, mode=mode, limit=limit,
                                with_subsystem=False)
            if near:
                return [f"{e.name} ({e.path})" for e in near]
    return []


def resolve_or_die(conn, spec: str, meta: dict | None = None) -> query.Resolution:
    if meta is not None:
        spec = _normalize_target_spec(meta, spec)
    res = query.resolve(conn, spec)
    if res.target is None:
        near = _suggestions(conn, spec)
        hint = "\n  did you mean: " + ", ".join(near) if near else ""
        _die(res.note + hint)
    return res


def _resolve_area(conn, spec: str, meta: dict | None = None) -> query.Resolution:
    """Prefer a directory named `spec` over a symbol that happens to share it.

    `bpf` is a variable in security/bpf/hooks.c *and* the directory kernel/bpf/.
    For commands about an area (docs), the directory is the useful answer.
    `kernel/bpf` is preferred over deeper homonyms like security/bpf/.
    """
    if meta is not None:
        spec = _normalize_target_spec(meta, spec)
    raw = (spec or "").strip().strip("/")
    if raw and "/" not in raw and ":" not in raw and raw not in (".",):
        rows = conn.execute(
            "SELECT * FROM dirs WHERE name = ?", (raw,)).fetchall()
        if rows:
            def rank(r):
                p = r["path"]
                if p == raw:
                    return (0, 0, p)
                if p == f"kernel/{raw}":
                    return (1, 0, p)
                return (2, *query._path_rank(p))
            rows = sorted(rows, key=rank)
            picked = rows[0]
            t = query.Target(kind="dir", id=picked["id"], path=picked["path"],
                             name=picked["name"], dir_id=picked["id"])
            others = [query.Target(kind="dir", id=r["id"], path=r["path"],
                                   name=r["name"], dir_id=r["id"])
                      for r in rows[1:]]
            note = ""
            if others and picked["path"] != raw:
                note = (f"{len(rows)} directories named {raw!r}; "
                        f"using {picked['path']}/")
            return query.Resolution(t, others, note)
    return resolve_or_die(conn, spec)


def _target_spec(t: query.Target) -> str:
    """A spec `ka` will accept again (`.` for the kernel root)."""
    if t.kind == "symbol":
        return f"{t.path}:{t.line}" if t.line is not None else t.display
    return t.path or "."


def _command_prefix(args, meta: dict | None = None) -> str:
    """A shell-safe query prefix which preserves the selected index."""
    if getattr(args, "db", None):
        path = Path(args.db).expanduser().resolve()
        return f"{PROG} --db {shlex.quote(str(path))}"
    if getattr(args, "kernel", None):
        # Preserve the exact resolved filename alias.  Reusing an abbreviated
        # prefix can become ambiguous after another index is built.
        selected = ((meta.get("index_stem") or args.kernel)
                    if meta is not None else args.kernel)
        return f"{PROG} -K {shlex.quote(selected)}"
    return PROG


def _call_graph_rebuild_hint(args, meta: dict) -> str | None:
    """An executable rebuild for this exact index, if its inputs exist.

    A missing downloaded tree can be fetched again from its recorded version.
    A missing custom ``--src`` tree cannot: silently substituting upstream
    source would publish a different snapshot under the same index identity.
    """
    version = index_version(meta)
    selected = meta.get("index_path")
    output = (Path(selected) if selected else selected_index(args)).resolve()
    command = f"{PROG} build {shlex.quote(version)}"
    tree = find_source_tree(meta)
    if tree is not None:
        command += f" --src {shlex.quote(str(tree))}"
    else:
        recorded = meta.get("tree_path")
        source = meta.get("source")
        if (isinstance(recorded, str) and recorded
                and isinstance(source, str) and source
                and _same_path(Path(source).expanduser(),
                               Path(recorded).expanduser())):
            return None
    return (f"{command} --output {shlex.quote(str(output))} "
            "--with-calls --force")


def _call_graph_rebuild_advice(args, meta: dict) -> str:
    hint = _call_graph_rebuild_hint(args, meta)
    if hint is not None:
        return f"rebuild with '{hint}'"
    recorded = meta.get("tree_path") or "the recorded custom source tree"
    return (f"restore the recorded custom source tree {recorded!r}, then rebuild "
            "this same index with --with-calls --force")


def _require_exact_line_qualifier(conn: sqlite3.Connection, spec: str) -> None:
    """Require a full file path when ``basename:line`` matches many files."""
    paths = query.ambiguous_line_paths(conn, spec)
    if len(paths) < 2:
        return
    tail = query.line_selector_suffix(spec)
    examples = ", ".join(f"{path}:{tail}" for path in paths[:3])
    basename = (spec or "").strip().rpartition(":")[0]
    _die(f"{len(paths)} files named {basename!r} make this line selector "
         "ambiguous; use one full indexed path:line"
         + (f" (for example: {examples})" if examples else ""))


def _require_unique_symbol_identity(
        res: query.Resolution, spec: str,
        conn: sqlite3.Connection | None = None) -> None:
    """Reject a guessed definition for commands whose output uses its line.

    ``info`` intentionally ranks and explains alternatives, but ``show``,
    ``path --line``, and ``web`` act on one concrete source identity.  A
    ``path:symbol`` qualifier is still ambiguous when conditional definitions
    repeat a name in the same file; ``path:line`` is the lossless spelling.
    """
    if conn is not None:
        _require_exact_line_qualifier(conn, spec)
    tail = query.line_selector_suffix(spec)
    line_qualified = tail is not None
    target = res.target
    if line_qualified and (target is None or target.kind != "symbol"):
        # ``resolve`` deliberately falls back to the containing file so that
        # informational commands can still describe a real path.  Commands
        # that act on a concrete source identity must not silently reinterpret
        # a failed ``path:line`` selector as the whole file.
        _die(res.note or f"no symbol spans line {tail}")
    if target is None:
        return
    if target.kind in {"file", "dir"}:
        alternatives = [candidate for candidate in res.candidates
                        if candidate.kind == target.kind]
        if not alternatives:
            return
        candidates = [target, *alternatives]
        noun = "files" if target.kind == "file" else "directories"
        examples = ", ".join(candidate.path or "." for candidate in candidates[:3])
        _die(f"{len(candidates)} {noun} match {spec!r}; use one full indexed path"
             + (f" (for example: {examples})" if examples else ""))
    if target.kind != "symbol":
        return
    if line_qualified:
        return
    callable_kinds = {"function", "syscall"}
    alternatives = [
        candidate for candidate in res.candidates
        if candidate.kind == "symbol"
        and (candidate.symbol_kind == target.symbol_kind
             or {candidate.symbol_kind, target.symbol_kind} <= callable_kinds)
    ]
    if not alternatives:
        return
    candidates = [target, *alternatives]
    same_file = len({candidate.path for candidate in candidates}) < len(candidates)
    qualifier = "path:line" if same_file else "path:symbol"
    examples = ", ".join(
        f"{candidate.path}:{candidate.line}" if same_file else candidate.display
        for candidate in candidates[:3])
    _die(f"{len(candidates)} definitions match {target.name!r}; qualify the "
         f"target as {qualifier} (for example: {examples})")


def _links_for(meta: dict, t: query.Target) -> dict[str, str]:
    return links.links(
        index_version(meta), t.path, t.line,
        is_dir=(t.kind == "dir"),
        ident=(t.name if t.kind == "symbol" else None),
        source=meta.get("source"))


def _subsystem_payload(row) -> dict:
    payload = dict(
        name=row["name"], status=row["status"],
        n_files=row["n_files"], claimed_files=row["n_files"],
        primary_files=row["n_primary_files"],
        **query.subsystem_json_fields(row),
    )
    if "n_claimed" in row.keys():
        payload["directory_claimed_files"] = row["n_claimed"]
        payload["directory_primary_files"] = row["n_primary"]
        payload["directory_coverage"] = row["coverage"]
    if "is_primary" in row.keys():
        payload["match_score"] = row["score"]
        payload["match_rank"] = row["rank"]
        payload["is_primary"] = bool(row["is_primary"])
    return payload


def _relationship_subsystem(conn, meta: dict, spec: str):
    exact = conn.execute(
        "SELECT * FROM subsystems WHERE name = ?", (spec,)).fetchall()
    if exact:
        return exact[0], None
    folded = conn.execute(
        "SELECT * FROM subsystems WHERE name = ? COLLATE NOCASE ORDER BY name",
        (spec,)).fetchall()
    if len(folded) == 1:
        return folded[0], None
    if len(folded) > 1:
        names = ", ".join(row["name"] for row in folded[:8])
        _die(f"{spec!r} is ambiguous under case-insensitive matching: {names}")

    normalized = _normalize_target_spec(meta, spec)
    resolved = query.resolve(conn, normalized)
    if resolved.target is not None:
        if resolved.candidates:
            candidates = [resolved.target, *resolved.candidates]
            owners = [query.subsystem_for_target(conn, candidate)
                      for candidate in candidates]
            owner_ids = {owner["id"] for owner in owners if owner is not None}
            if len(owner_ids) == 1 and all(
                    owner is not None and owner["name"] not in query.CATCH_ALL
                    for owner in owners):
                subsystem = next(owner for owner in owners
                                 if owner["id"] in owner_ids)
                note = (f"all {len(candidates)} matches for {spec!r} belong to "
                        f"{subsystem['name']}")
                return subsystem, note
            same_file = len({candidate.path for candidate in candidates}) \
                < len(candidates)
            qualifier = "path:line" if same_file else "path:symbol"
            examples = ", ".join(
                f"{candidate.path}:{candidate.line}" if same_file
                else candidate.display
                for candidate in candidates[:4])
            _die(f"target {spec!r} is ambiguous; qualify it as {qualifier}"
                 + (f" (for example: {examples})" if examples else ""))
        subsystem = query.subsystem_for_target(conn, resolved.target)
        if subsystem is None and resolved.target.kind == "dir":
            owners = query.directory_primary_subsystems(
                conn, resolved.target.id)
            specific = [row for row in owners
                        if row["name"] not in query.CATCH_ALL]
            if len(owners) > 1:
                examples = ", ".join(
                    f"{row['name']} ({row['coverage']:.0%})"
                    for row in specific[:5])
                _die(f"{resolved.target.display} has mixed ownership across "
                     f"{len(owners)} primary owners; name a subsystem explicitly"
                     + (f" ({examples})" if examples else ""))
        if subsystem is None and resolved.target.kind != "dir":
            file_id = resolved.target.file_id or resolved.target.id
            owners = query.file_primary_subsystems(conn, file_id)
            specific = [row for row in owners
                        if row["name"] not in query.CATCH_ALL]
            if len(specific) > 1:
                examples = ", ".join(row["name"] for row in specific[:5])
                _die(f"{resolved.target.display} has {len(specific)} "
                     "co-primary subsystem owners; name a subsystem explicitly"
                     + (f" ({examples})" if examples else ""))
        if subsystem is None or subsystem["name"] in query.CATCH_ALL:
            _die(f"{resolved.target.display} has no specific subsystem owner")
        note = f"resolved {spec!r} to {subsystem['name']}"
        return subsystem, note

    matches = query.subsystem_by_name(conn, spec)
    if not matches:
        _die(f"no target or subsystem matching {spec!r}")
    if len(matches) > 1:
        names = ", ".join(row["name"] for row in matches[:8])
        _die(f"{spec!r} matches {len(matches)} subsystems; use a more specific "
             f"name ({names})")
    return matches[0], None
