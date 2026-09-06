"""CLI handlers for source links, documentation, and cross-index lookup."""

from __future__ import annotations

import shlex
import sqlite3
import sys
from pathlib import Path

from .. import config, db, query, render
from ..queries import links
from ..presentation.terminal import Console


def cmd_web(args, support):
    """Print Elixir, kernel Git, GitHub, and kernel documentation URLs."""
    conn, meta = support.open_index(args)
    resolution = support.resolve_or_die(conn, args.target, meta)
    support._require_unique_symbol_identity(resolution, args.target, conn)
    target = resolution.target
    link_map = support._links_for(meta, target)
    version = support.index_version(meta)

    if not link_map:
        support._die(
            f"no upstream release-reference URLs for {target.display}; this "
            "index was built from local/custom source or a nonmatching "
            "archive (use 'path' or 'show' for the recorded source tree)")

    if args.url:
        url = link_map.get(args.url)
        if not url:
            why = ""
            if args.url == "docs":
                why = " (not a Documentation/ file)"
            elif args.url == "ident":
                why = " (not a symbol)"
            support._die(f"no {args.url} URL for {target.display}{why}")
        print(url)
        return

    if args.format == "json":
        sys.stdout.write(render.render_json({
            "target": target.display,
            "version": version,
            "links": link_map,
        }))
        return

    console = Console(args.color)
    location = target.path or "."
    if target.kind == "symbol" and target.line:
        location = f"{target.path}:{target.line}"
    label = (f"{location}  "
             f"{target.name if target.kind == 'symbol' else ''}").rstrip()
    console.heading(f"{label}   [Linux {version}]")
    console.blank()
    order = ("elixir", "ident", "git", "github", "docs")
    width = max(len(key) for key in order if key in link_map)
    for key in order:
        if key in link_map:
            # Leave each URL intact for copying, even in a narrow terminal.
            console.text(f"{key:<{width}}  {link_map[key]}", wrap=False)


def cmd_docs(args, support):
    conn, meta = support.open_index(args)
    resolution = support._resolve_area(conn, args.target, meta)
    target = resolution.target
    matches = query.documentation_matches(
        conn, target, limit=args.limit, under=args.under)
    entries = [match.entry for match in matches]
    if not entries:
        scope_note = f" under {args.under}" if args.under else ""
        support._die(
            f"no Documentation/ files related to {target.display}{scope_note}")
    subsystem = query.subsystem_for_target(conn, target)
    label = (subsystem["name"]
             if subsystem and subsystem["name"] not in query.CATCH_ALL
             else None)
    version = support.index_version(meta)
    if args.format == "json":
        payload = []
        for match in matches:
            entry = match.entry
            item = {
                "path": entry.path,
                "name": entry.name,
                "lines": entry.lines,
                "size": entry.size,
                "index": version,
            }
            item.update(links.links(
                version, entry.path, source=meta.get("source")))
            if args.explain:
                item["reasons"] = list(match.reasons)
            payload.append(item)
        sys.stdout.write(render.render_json(payload))
        return
    console = Console(args.color)
    heading = f"Documentation related to {target.display}"
    if label:
        heading += f"   [{label}]"
    heading += f"   [{support._linux(meta)}]"
    console.heading(heading)
    if resolution.note:
        console.note(resolution.note)
    console.blank()
    for match in matches:
        console.text(match.entry.path, tone="accent")
        if args.explain:
            for reason in match.reasons:
                console.text(f"- {reason}", tone="muted", indent=4)
    prefix = support._command_prefix(args, meta)
    first = shlex.quote(entries[0].path)
    console.blank()
    console.text(f"{len(entries)} file{'s' if len(entries) != 1 else ''}",
                 tone="muted")
    if links.links(version, entries[0].path, source=meta.get("source")):
        next_command = f"{prefix} web {first}"
    else:
        next_command = f"{prefix} show {first}"
    console.commands([next_command])


def cmd_locate(args, support):
    """Resolve a target in every built index to show version movement."""
    if getattr(args, "db", None):
        db_path = Path(args.db).expanduser()
        if not db_path.is_file():
            support._die(f"no index at {db_path}")
        available = [db_path]
        active = db_path
    else:
        available = config.list_indexes()
        if not available:
            support._die(
                f"no index built yet — run '{support.PROG} build lts' first")
        active = support.selected_index(args)

    rest = [path for path in available
            if not support._same_path(path, active)]
    rest.sort(key=support._index_version_key, reverse=True)
    ordered = (
        [active]
        if active is not None
        and any(support._same_path(path, active) for path in available)
        else []) + rest

    spec = args.target
    resolved_spec = spec
    rows = []
    for path in ordered:
        conn = None
        is_active = support._same_path(path, active)
        try:
            try:
                conn = db.connect(path, readonly=True)
                meta = db.validate_schema(conn)
                meta["index_stem"] = path.stem
                version = support.index_version(meta)
                if is_active:
                    resolved_spec = support._normalize_target_spec(meta, spec)
                resolution = query.resolve(conn, resolved_spec)
            except (sqlite3.Error, OSError) as exc:
                rows.append({
                    "version": path.stem,
                    "found": False,
                    "active": is_active,
                    "error": str(exc),
                })
                continue
            target = resolution.target
            line_qualified = query.line_selector_suffix(resolved_spec) is not None
            if line_qualified and target is not None and target.kind != "symbol":
                # Generic informational resolution falls back to a real file
                # when no symbol spans a requested line.  Cross-version lookup
                # must retain that as a miss, not silently change identities.
                target = None
            if target is None:
                rows.append({
                    "version": version,
                    "found": False,
                    "active": is_active,
                    "note": resolution.note,
                })
            else:
                subsystem = query.subsystem_for_target(conn, target)
                label = (
                    subsystem["name"]
                    if subsystem and subsystem["name"] not in query.CATCH_ALL
                    else None)
                if not label:
                    area = query.describe_area(target.path)
                    label = (area[0] if area
                             else (subsystem["name"] if subsystem else None))
                rows.append({
                    "version": version,
                    "found": True,
                    "active": is_active,
                    "kind": target.symbol_kind or target.kind,
                    "name": target.name,
                    "path": target.path or ".",
                    "line": target.line,
                    "end_line": target.end_line,
                    "subsystem": label,
                    "note": resolution.note or None,
                })
        finally:
            if conn is not None:
                conn.close()

    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return

    console = Console(args.color)
    active_name = next(
        (row["version"] for row in rows if row.get("active")), None)
    console.heading(
        f"{spec}  across {len(rows)} index"
        f"{'es' if len(rows) != 1 else ''}")
    if active_name:
        console.text(f"* = {support._linux({'index_stem': active_name})}",
                     tone="muted")
    console.blank()
    table_rows = []
    row_tones = []
    notes = []
    for row in rows:
        mark = "*" if row.get("active") else "-"
        if not row.get("found"):
            why = row.get("error") or row.get("note") or "not in this index"
            table_rows.append((mark, row["version"], "missing", why, "-"))
            row_tones.append("error" if row.get("error") else "muted")
            continue
        location = row["path"]
        if row.get("line"):
            location = f"{row['path']}:{row['line']}"
        subsystem = row.get("subsystem") or "-"
        table_rows.append((mark, row["version"], row["kind"], location, subsystem))
        row_tones.append(None)
        if row.get("note"):
            notes.append(f"{row['version']} note: {row['note']}")
    console.table(("ACTIVE", "VERSION", "KIND", "LOCATION / STATUS", "SUBSYSTEM"),
                  table_rows, tones={1: "success", 2: "muted", 4: "accent"},
                  row_tones=row_tones)
    for note in notes:
        console.note(note)
