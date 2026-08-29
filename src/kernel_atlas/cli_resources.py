"""CLI handlers for source links, documentation, and cross-index lookup."""

from __future__ import annotations

import shlex
import sqlite3
import sys
from pathlib import Path

from . import config, db, links, query, render


def cmd_web(args, support):
    """Print Elixir, kernel Git, GitHub, and kernel documentation URLs."""
    conn, meta = support.open_index(args)
    resolution = support.resolve_or_die(conn, args.target, meta)
    support._require_unique_symbol_identity(resolution, args.target)
    target = resolution.target
    link_map = support._links_for(meta, target)
    version = support.index_version(meta)

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

    color = render.use_color(args.color)
    location = target.path or "."
    if target.kind == "symbol" and target.line:
        location = f"{target.path}:{target.line}"
    label = (f"{location}  "
             f"{target.name if target.kind == 'symbol' else ''}").rstrip()
    print(render.paint(label, "1;36", color)
          + render.paint(f"   [Linux {version}]", "90", color))
    order = ("elixir", "ident", "git", "github", "docs")
    width = max(len(key) for key in order if key in link_map)
    for key in order:
        if key in link_map:
            print(f"  {key:<{width}}  {link_map[key]}")


def cmd_docs(args, support):
    conn, meta = support.open_index(args)
    resolution = support._resolve_area(conn, args.target, meta)
    target = resolution.target
    entries = query.documentation_for(conn, target, limit=args.limit)
    if not entries:
        support._die(
            f"no Documentation/ files related to {target.display}")
    subsystem = query.subsystem_for_target(conn, target)
    label = (subsystem["name"]
             if subsystem and subsystem["name"] not in query.CATCH_ALL
             else None)
    version = support.index_version(meta)
    if args.format == "json":
        payload = []
        for entry in entries:
            item = {
                "path": entry.path,
                "name": entry.name,
                "lines": entry.lines,
                "size": entry.size,
                "index": version,
            }
            item.update(links.links(version, entry.path))
            payload.append(item)
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    heading = f"Documentation related to {target.display}"
    if label:
        heading += f"   [{label}]"
    heading += f"   [{support._linux(meta)}]"
    print(render.paint(heading, "1", color))
    if resolution.note:
        print(render.paint(f"  ({resolution.note})", "33", color))
    for entry in entries:
        print(f"  {entry.path}")
    prefix = support._command_prefix(args, meta)
    first = shlex.quote(entries[0].path)
    print(render.paint(
        f"\n{len(entries)} file{'s' if len(entries) != 1 else ''}"
        f"   Next: {prefix} web {first}", "90", color))


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

    color = render.use_color(args.color)
    active_name = next(
        (row["version"] for row in rows if row.get("active")), None)
    note = (f"  * = {support._linux({'index_stem': active_name})}"
            if active_name else "")
    print(render.paint(
        f"{spec}  across {len(rows)} index"
        f"{'es' if len(rows) != 1 else ''}{note}\n", "1", color))
    version_width = max((len(row["version"]) for row in rows), default=8)
    for row in rows:
        mark = "*" if row.get("active") else " "
        version = row["version"].ljust(version_width)
        prefix = f"  {mark} {version}"
        if not row.get("found"):
            why = row.get("error") or "not in this index"
            print(f"{prefix}  {render.paint(why, '90', color)}")
            continue
        location = row["path"]
        if row.get("line"):
            location = f"{row['path']}:{row['line']}"
        subsystem = row.get("subsystem") or "-"
        print(f"{prefix}  {row['kind']:<10} {location:<42} "
              f"{render.paint(subsystem, '35', color)}")
