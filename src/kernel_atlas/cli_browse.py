"""CLI handlers for browsing indexed paths, symbols, and subsystems."""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import replace

from . import query, render
from .query import Entry


def cmd_info(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    t = res.target
    color = render.use_color(args.color)

    composition = query.all_subsystems(
        conn, "dir" if t.kind == "dir" else "file",
        t.id if t.kind == "dir" else (t.file_id or t.id))
    unclassified = next((s for s in composition
                         if s["name"] in query.CATCH_ALL
                         and ((t.kind == "dir" and s["n_primary"] > 0)
                              or (t.kind != "dir" and s["is_primary"]))), None)
    subs = [s for s in composition if s["name"] not in query.CATCH_ALL]
    area = query.describe_area(t.path)
    lnks = support._links_for(meta, t)

    path_row = None
    subtree_files = None
    symbols_by_kind: dict[str, int] = {}
    if t.kind in {"dir", "file"}:
        table = "dirs" if t.kind == "dir" else "files"
        path_row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (t.id,)).fetchone()
        if t.kind == "dir" and path_row is not None:
            subtree_files = path_row["n_files_recursive"]
        elif t.kind == "file" and path_row is not None:
            symbols_by_kind = {
                r["kind"]: r["n"] for r in conn.execute(
                    "SELECT kind, COUNT(*) n FROM symbols WHERE file_id = ?"
                    " GROUP BY kind ORDER BY n DESC", (t.id,))
            }

    if t.kind == "dir":
        unmatched_files = query.directory_unclaimed_files(conn, t.path)
    else:
        unmatched_files = int(not any(
            bool(row["is_primary"]) for row in composition))

    tree = support.find_source_tree(meta)
    source_entry = support.source_member(tree, t.path) if tree is not None else None
    on_disk = str(source_entry) if source_entry is not None else None
    source_exists = ((source_entry.exists() or source_entry.is_symlink())
                     if source_entry is not None else None)
    linkage = None
    if t.kind == "symbol" and t.symbol_kind in {
            "function", "syscall", "variable", "prototype"}:
        if t.is_exported:
            linkage = "exported to modules"
        elif t.is_static:
            linkage = "static (file-local)"
        elif t.symbol_kind == "prototype":
            linkage = "declaration"
        else:
            linkage = "global"

    if args.format == "json":
        unclassified_payload = None
        if unclassified is not None or unmatched_files:
            if t.kind == "dir":
                catch_all_primary = (int(unclassified["n_primary"])
                                     if unclassified is not None else 0)
                total = int(subtree_files or 0)
                unclassified_payload = {
                    "primary_files": catch_all_primary,
                    "claimed_files": (int(unclassified["n_claimed"])
                                      if unclassified is not None else 0),
                    "unmatched_files": unmatched_files,
                    "coverage": ((catch_all_primary + unmatched_files) / total
                                 if total else 0.0),
                    "maintainers_section": (unclassified["name"]
                                            if unclassified is not None
                                            else None),
                }
            else:
                unclassified_payload = {
                    "is_primary": (bool(unclassified["is_primary"])
                                   if unclassified is not None else False),
                    "unmatched": bool(unmatched_files),
                    "match_score": (unclassified["score"]
                                    if unclassified is not None else None),
                    "match_rank": (unclassified["rank"]
                                   if unclassified is not None else None),
                    "maintainers_section": (unclassified["name"]
                                            if unclassified is not None
                                            else None),
                }
        target = {
            "kind": t.kind, "symbol_kind": t.symbol_kind, "name": t.name,
            "path": t.path, "line": t.line, "end_line": t.end_line,
            "signature": t.signature,
        }
        if t.symbol_kind in {"function", "syscall"}:
            target.update(is_static=t.is_static, is_inline=t.is_inline,
                          is_exported=t.is_exported, linkage=linkage)
        elif t.symbol_kind == "variable":
            target.update(is_static=t.is_static, is_exported=t.is_exported,
                          linkage=linkage)
        elif t.symbol_kind == "prototype":
            target.update(is_static=t.is_static, is_inline=t.is_inline,
                          linkage=linkage)
        elif t.kind == "dir" and path_row is not None:
            target.update(
                n_subdirs=path_row["n_subdirs"],
                n_files=path_row["n_files"],
                n_files_subtree=subtree_files,
            )
        elif t.kind == "file" and path_row is not None:
            target.update(
                extension=path_row["ext"], size=path_row["size"],
                lines=path_row["lines"], n_symbols=path_row["n_symbols"],
                symbols_by_kind=symbols_by_kind,
                is_symlink=bool(path_row["is_symlink"]),
                link_target=path_row["link_target"],
                index_status=path_row["index_status"],
                index_error=path_row["index_error"],
            )
        payload = {
            "target": target,
            "area": {"name": area[0], "description": area[1]} if area else None,
            "subsystems": [
                support._subsystem_payload(s)
                for s in subs[:args.max_subsystems]],
            "n_subsystems": len(subs),
            "unclassified_ownership": unclassified_payload,
            "ancestry": [{"path": p, "subsystem": s}
                         for p, s in query.ancestry(conn, t.path)],
            "links": lnks,
            "source_path": on_disk,
            "source_exists": source_exists,
            "index": support.index_version(meta),
            "note": res.note,
            "other_candidates": [c.display
                                 for c in res.candidates[:args.max_candidates]],
            "n_other_candidates": len(res.candidates),
        }
        sys.stdout.write(render.render_json(payload))
        return

    print(render.paint(t.display, "1;36", color))
    if res.note:
        print(render.paint(f"  ({res.note})", "33", color))
    print()

    def field(k, v):
        if v is not None and v != "":
            print(f"  {k:<12} {v}")

    if t.kind == "symbol":
        field("kind", t.symbol_kind)
        location_label = "declared in" if t.symbol_kind == "prototype" \
            else "defined in"
        field(location_label, f"{t.path}:{t.line}"
              + (f"-{t.end_line} ({t.end_line - t.line + 1} lines)"
                 if t.end_line and t.line else ""))
        field("signature", t.signature)
        field("linkage", linkage)
    else:
        field("kind", "directory" if t.kind == "dir" else "file")
        field("path", t.path or "<kernel root>")
        if t.kind == "dir" and path_row is not None:
            field("contains", f"{path_row['n_subdirs']} subdirectories, "
                              f"{path_row['n_files']} files")
            if subtree_files != path_row["n_files"]:
                field("subtree", f"{subtree_files:,} files in total")
        elif path_row is not None:
            field("size", f"{path_row['size']:,} bytes, "
                          f"{path_row['lines']:,} lines")
            field("index status", path_row["index_status"])
            if path_row["is_symlink"]:
                field("symlink to", path_row["link_target"] or "unknown")
            field("index error", path_row["index_error"])
            if symbols_by_kind:
                field("defines", ", ".join(
                    f"{count} {kind}" for kind, count in symbols_by_kind.items()))

    if source_exists:
        field("on disk", on_disk)
    elif on_disk is not None:
        field("source path", f"{on_disk} (missing)")
    field("index", support._linux(meta))
    field("elixir", lnks.get("elixir"))
    if lnks.get("docs"):
        field("docs", lnks["docs"])
    if lnks.get("ident"):
        field("ident", lnks["ident"])

    if area:
        print()
        print(render.paint(f"  Area: {area[0]}", "1;32", color))
        print(f"    {area[1]}")

    if subs or unclassified is not None or unmatched_files:
        print()
        heading = ("  Subsystem composition (from descendant files)"
                   if t.kind == "dir" else "  Subsystem (from MAINTAINERS)")
        print(render.paint(heading, "1;35", color))
        for i, s in enumerate(subs[:args.max_subsystems]):
            marker = "*" if (i == 0 if t.kind == "dir"
                               else bool(s["is_primary"])) else " "
            if t.kind == "dir":
                detail = (f"{s['n_primary']:,} primary / {s['n_claimed']:,} "
                          f"claimed descendant files ({s['coverage']:.0%})")
            else:
                detail = f"{s['n_files']:,} claimed files"
            print(f"   {marker} {render.paint(s['name'], '1', color)}"
                  f"   [{s['status'] or 'unknown'}]  {detail}")
            f = query.subsystem_json_fields(s)
            for who in f["maintainers"][:3]:
                print(f"       maintainer  {who}")
            for lst in f["lists"][:2]:
                print(f"       list        {lst}")
        if unclassified is not None:
            if t.kind == "dir":
                detail = (f"{unclassified['n_primary']:,} primary descendant "
                          f"files ({unclassified['coverage']:.0%})")
            else:
                detail = "the only primary ownership match for this file"
            print("     " + render.paint("Unclassified", "1", color)
                  + f"   {detail}; represented only by the "
                    f"{unclassified['name']} catch-all")
        if unmatched_files:
            if t.kind == "dir":
                detail = (f"{unmatched_files:,} descendant file"
                          f"{'s have' if unmatched_files != 1 else ' has'}")
            else:
                detail = "the containing file has"
            print("     " + render.paint("Unclassified", "1", color)
                  + f"   {detail} no primary MAINTAINERS match")
        if len(subs) > args.max_subsystems:
            print(f"     ... and {len(subs) - args.max_subsystems} more "
                  f"(--max-subsystems to show)")
    elif not area:
        print("\n  No MAINTAINERS section claims this path.")

    anc = query.ancestry(conn, t.path)
    if anc:
        print()
        print(render.paint("  Path breakdown", "1", color))
        for p, s in anc:
            print(f"    {p + '/':<38} {s or '-'}")

    if res.candidates:
        print()
        print(render.paint(f"  {len(res.candidates)} other candidate(s) "
                           f"for this name", "33", color))
        for c in res.candidates[:args.max_candidates]:
            print(f"    {c.display}  ({c.symbol_kind or c.kind})")
        if len(res.candidates) > args.max_candidates:
            print(f"    ... and {len(res.candidates) - args.max_candidates} more")

    prefix = support._command_prefix(args, meta)
    target_arg = shlex.quote(support._target_spec(t))
    next_lines = [f"\n  Next:  {prefix} siblings {target_arg}"]
    if t.kind == "symbol" and t.symbol_kind in {"struct", "union"}:
        next_lines.append(f"         {prefix} struct {target_arg}")
    next_lines.append(f"         {prefix} web {target_arg}")
    print("\n".join(next_lines))


def cmd_siblings(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    t = res.target
    scope = query.build_scope(conn, t, args.level)
    if scope.dir_sql is None and scope.file_sql is None and scope.sym_where is None:
        support._die(f"cannot build a '{args.level}' scope for {t.display} ({scope.label})")

    kinds = support.symbol_filter_kinds(args, support.kinds_from_args(args, t))
    support._reject_symbol_size_sort(args, kinds)
    if (args.level == "tree"
            and any(k in query.SYMBOL_KINDS for k in kinds)
            and not args.limit):
        support._die("listing symbols across the whole tree needs -n N "
             "(there are millions; try -n 50, or 'find' for a name search)")
    sub = query.subsystem_for_target(conn, t)
    if (args.level == "subsystem" and sub is not None
            and sub["name"] in query.CATCH_ALL and not args.limit):
        support._die("the target is claimed only by the catch-all THE REST subsystem; "
             "this scope is almost the whole tree and needs -n N")
    # Fetch one extra row so that dropping the target itself does not eat one
    # of the requested rows; subsystems are looked up only for what survives.
    grep = support._checked_grep(args.grep)
    entries = query.collect(
        conn, scope, kinds, limit=args.limit + 1 if args.limit else 0,
        grep=grep,
        exported_only=args.exported, static=support._static_mode(args),
        with_subsystem=False, sort=args.sort)

    target_entry = next((e for e in entries if support._entry_is_target(e, t)), None)
    others = [e for e in entries if not support._entry_is_target(e, t)]
    if args.limit:
        others = others[:args.limit]
    entries = others
    if args.include_self:
        target_entry = target_entry or query.entry_for_target(conn, t)
        if target_entry is not None:
            # --include-self means exactly that: filters and --kinds govern the
            # N *other* rows, while the explicitly requested target is always
            # present in addition to them.
            target_entry.is_target = True
            entries.append(target_entry)
            query.sort_entries(entries, args.sort)
    want_subsystem = (args.with_subsystem
                      or "subsystem" in support._split_list(args.columns))
    if want_subsystem:
        query.annotate_subsystems(conn, entries)

    label = sub["name"] if sub else None
    if label in query.CATCH_ALL:
        area = query.describe_area(t.path)
        label = area[0] if area else None
    header = (f"Siblings of {t.display}  [{support._linux(meta)}]\n"
              f"  level: {scope.label}"
              + (f"   subsystem: {label}" if label else "")
              + f"   showing: {', '.join(kinds)}\n")
    support.emit(entries, args, set(kinds), want_subsystem, header,
         index=support.index_version(meta))


def cmd_ls(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target or "", meta)
    t = res.target
    if t.kind == "symbol":
        prefix = support._command_prefix(args, meta)
        target = shlex.quote(support._target_spec(t))
        support._die(f"{t.display} is a symbol; try '{prefix} siblings {target}'")

    if t.kind == "dir":
        scope = query.Scope(
            f"contents of {t.path or 'the kernel root'}/",
            "SELECT * FROM dirs WHERE parent_id = ?", (t.id,),
            "SELECT * FROM files WHERE dir_id = ?", (t.id,),
            "s.file_id IN (SELECT id FROM files WHERE dir_id = ?)", (t.id,))
        default = ("dir", "file")
    else:
        scope = query.Scope(f"file {t.path}", None, (),
                            "SELECT * FROM files WHERE id = ?", (t.id,),
                            "s.file_id = ?", (t.id,))
        default = query.SYMBOL_KINDS

    kinds = support.kinds_from_args(args, t) if support._split_list(args.kinds) else default
    kinds = support.symbol_filter_kinds(args, kinds)
    support._reject_symbol_size_sort(args, kinds)
    want_subsystem = (args.with_subsystem
                      or "subsystem" in support._split_list(args.columns))
    entries = query.collect(conn, scope, kinds, limit=args.limit,
                            grep=support._checked_grep(args.grep),
                            exported_only=args.exported, static=support._static_mode(args),
                            with_subsystem=want_subsystem, sort=args.sort)
    support.emit(entries, args, set(kinds), want_subsystem,
         f"{scope.label}  [{support._linux(meta)}]\n", index=support.index_version(meta))


def cmd_find(args, support):
    conn, meta = support.open_index(args)
    mode = "exact" if args.exact else ("glob" if args.glob else
                                       ("prefix" if args.prefix else "substring"))
    if support._split_list(args.kinds):
        kinds = [k for k in support.kinds_from_args(args, None) if k in query.SYMBOL_KINDS]
        if not kinds:
            support._die("find only searches symbols; try --kinds function,struct,...")
    else:
        kinds = []
    support._reject_symbol_size_sort(args, kinds or query.SYMBOL_KINDS)
    grep = support._checked_grep(args.grep)
    explicit_columns = support._split_list(args.columns)
    want_subsystem = (args.format != "names"
                      and (args.with_subsystem or not explicit_columns
                           or "subsystem" in explicit_columns))
    entries = query.search(conn, args.pattern, kinds=kinds, mode=mode,
                           limit=args.limit,
                           exported_only=args.exported,
                           with_subsystem=want_subsystem, grep=grep,
                           static=support._static_mode(args), sort=args.sort)
    support.emit(entries, args, {"function"}, want_subsystem,
         f"Symbols matching {args.pattern!r} ({mode})  [{support._linux(meta)}]\n",
         index=support.index_version(meta),
         default_columns=("kind", "name", "path", "line", "subsystem"))


def cmd_subsystems(args, support):
    conn, meta = support.open_index(args)
    order = {
        "size": "n_files DESC, name",
        "claimed": "n_files DESC, name",
        "primary": "n_primary_files DESC, name",
        "name": "name",
    }[args.sort]
    rows = conn.execute(
        f"SELECT * FROM subsystems ORDER BY {order}").fetchall()
    pattern = support._checked_grep(args.grep)
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        rows = [r for r in rows if rx.search(r["name"] or "")]
    if args.limit:
        rows = rows[:args.limit]
    if args.format == "json":
        payload = []
        for row in rows:
            item = support._subsystem_payload(row)
            item["index"] = support.index_version(meta)
            payload.append(item)
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    print(render.paint(f"{len(rows)} subsystems  [{support._linux(meta)}]", "1", color))
    print(f"  {'CLAIMED':>7} {'PRIMARY':>7}  {'STATUS':<16} NAME")
    for r in rows:
        print(f"  {r['n_files']:>7,} {r['n_primary_files']:>7,}  "
              f"{r['status'] or '?':<16} {r['name']}")


def cmd_subsystem(args, support):
    conn, meta = support.open_index(args)
    rows = query.subsystem_by_name(conn, args.name)
    if not rows:
        prefix = support._command_prefix(args, meta)
        support._die(f"no subsystem matching {args.name!r} "
             f"(try '{prefix} subsystems --grep {shlex.quote(args.name)}')")
    if len(rows) > 1:
        if args.format == "json":
            sys.stdout.write(render.render_json({
                "query": args.name,
                "ambiguous": True,
                "matches": [dict(name=r["name"], status=r["status"],
                                 n_files=r["n_files"],
                                 primary_files=r["n_primary_files"])
                            for r in rows],
                "index": support.index_version(meta),
            }))
            return
        color = render.use_color(args.color)
        print(render.paint(f"{len(rows)} subsystems match {args.name!r}:", "1", color))
        for r in rows:
            print(f"  {r['n_files']:>6,} claimed  "
                  f"{r['n_primary_files']:>6,} primary  {r['name']}")
        return
    s = rows[0]
    f = query.subsystem_json_fields(s)
    directory_limit = args.limit if args.limit else 10**9
    directory_rows = conn.execute(
        "SELECT d.path, p.n_claimed, p.n_primary, p.coverage FROM dirs d"
        " JOIN dir_subsys p ON p.dir_id=d.id WHERE p.subsystem_id=?"
        " AND d.path != ''"
        " ORDER BY p.n_primary DESC, p.coverage DESC, d.depth DESC, d.path"
        " LIMIT ?", (s["id"], directory_limit)
    ).fetchall()
    if args.format == "json":
        payload = support._subsystem_payload(s)
        payload["index"] = support.index_version(meta)
        payload["directories"] = [
            {
                "path": row["path"],
                "primary_files": row["n_primary"],
                "claimed_files": row["n_claimed"],
                "coverage": row["coverage"],
            }
            for row in directory_rows
        ]
        if args.files:
            payload["files"] = [r["path"] for r in conn.execute(
                "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
                " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path",
                (s["id"],))]
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    print(render.paint(s["name"], "1;35", color))
    print(f"  index        {support._linux(meta)}")
    print(f"  status       {s['status'] or 'unknown'}")
    for who in f["maintainers"]:
        print(f"  maintainer   {who}")
    for who in f["reviewers"][:5]:
        print(f"  reviewer     {who}")
    for lst in f["lists"]:
        print(f"  list         {lst}")
    for tree in f["trees"][:3]:
        print(f"  git          {tree}")
    for website in f["websites"]:
        print(f"  web          {website}")
    for url in f["patchwork"]:
        print(f"  patchwork    {url}")
    for url in f["bugs"]:
        print(f"  bugs         {url}")
    for chat in f["chats"]:
        print(f"  chat         {chat}")
    for profile in f["profiles"]:
        print(f"  profile      {profile}")
    if f["keywords"]:
        print(f"  keywords     {', '.join(f['keywords'])}")
    print(f"  files        {s['n_files']:,} claimed, "
          f"{s['n_primary_files']:,} primary")

    print(render.paint("\n  Directory composition", "1", color))
    for r in directory_rows:
        print(f"    {r['path'] + '/':<48} {r['n_primary']:>5} primary  "
              f"{r['n_claimed']:>5} claimed  {r['coverage']:>6.1%}")

    if args.files:
        print(render.paint("\n  Files", "1", color))
        for r in conn.execute(
            "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
            " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path", (s["id"],)
        ):
            print(f"    {r['path']}")


def cmd_tree(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target or "", meta)
    t = res.target
    base = t.path if t.kind == "dir" else query.parent_path(t.path)
    color = render.use_color(args.color)
    max_depth = args.depth
    base_depth = base.count("/") + 1 if base else 0

    rows = conn.execute(
        "SELECT path, name, depth, n_files, n_subdirs FROM dirs"
        " WHERE (path = ? OR path LIKE ? ESCAPE '\\') AND depth <= ? ORDER BY path",
        (base, query.like_under(base), base_depth + max_depth)).fetchall()
    entries = [Entry(kind="dir", name=r["name"], path=r["path"],
                     n_files=r["n_files"], n_subdirs=r["n_subdirs"])
               for r in rows if r["path"]]
    if args.files:
        # Visual depth: files `max_depth` components below `base`, not one
        # extra level deeper than the directories (the old Python filter).
        slash_max = (base.count("/") + max_depth) if base else (max_depth - 1)
        like = query.like_under(base)
        if slash_max >= 0:
            frows = conn.execute(
                "SELECT path, name, size, lines, n_symbols FROM files"
                f" WHERE path LIKE ? ESCAPE '\\' AND {support._SLASH_COUNT} <= ? ORDER BY path",
                (like, slash_max)).fetchall()
            entries += [Entry(kind="file", name=r["name"], path=r["path"],
                              size=r["size"], lines=r["lines"],
                              n_symbols=r["n_symbols"])
                        for r in frows]
    entries = [e for e in entries if e.path != base]
    # Directory and file queries are separate; merge them by path before the
    # tree renderer records sibling insertion order.
    entries.sort(key=lambda e: (e.path, e.kind))
    if args.format == "json":
        payload = [render.entry_dict(e) for e in entries]
        ver = support.index_version(meta)
        for row in payload:
            row["index"] = ver
        sys.stdout.write(render.render_json(payload))
        return

    # render_tree nests on path components, so strip the base to avoid redrawing
    # the ancestors of the directory the user asked about.
    prefix = f"{base}/" if base else ""
    relative = [replace(e, path=e.path[len(prefix):]) for e in entries]
    print(render.paint(f"{base or 'kernel root'}/", "1;34", color))
    sys.stdout.write(render.render_tree(relative, color))
    print(render.paint(f"\n{len(entries)} entries (depth {max_depth})  [{support._linux(meta)}]",
                       "90", color))


def cmd_path(args, support):
    """Print the on-disk path, so `$EDITOR $(ka path tcp_sendmsg)` just works."""
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    support._require_unique_symbol_identity(res, args.target)
    t = res.target
    if args.line and t.kind != "symbol":
        support._die("--line only applies to symbols")
    tree = support.source_tree(meta)
    full = support.source_member(tree, t.path)
    if not full.exists() and not full.is_symlink():
        support._die(f"{full} is missing from the source tree")
    if args.line and t.kind == "symbol":
        print(f"{full}:{t.line}")
    else:
        print(full)


def cmd_show(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    support._require_unique_symbol_identity(res, args.target)
    t = res.target
    if t.kind == "dir":
        prefix = support._command_prefix(args, meta)
        target = shlex.quote(support._target_spec(t))
        support._die(f"{t.path} is a directory; try '{prefix} ls {target}'")
    if t.kind == "symbol" and args.lines:
        support._die("--lines applies to files; use --context for a symbol")
    if t.kind != "symbol" and args.context:
        support._die("--context applies to symbols; use --lines for a file")
    tree = support.source_tree(meta)
    full = support.source_member(tree, t.path)
    if not full.is_file():
        support._die(f"{full} is missing from the source tree")
    try:
        with full.open("rb") as fh:
            head = fh.read(8192)
        if b"\0" in head:
            support._die(f"{t.path} looks like a binary file")
    except OSError as exc:
        support._die(f"cannot read {full}: {exc}")

    if t.kind == "symbol":
        start = max(1, (t.line or 1) - args.context)
        end: int | None = (t.end_line or t.line or 1) + args.context
    elif args.lines:
        m = re.fullmatch(r"(\d+)(?:[:-](\d+))?", args.lines)
        if not m:
            support._die(f"--lines wants N or N:M, not {args.lines!r}")
        try:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
        except ValueError:
            support._die("--lines contains a line number that is too large")
        if start < 1 or end < 1:
            support._die("--lines line numbers must be >= 1")
        if end < start:
            support._die(f"--lines {args.lines!r}: end is before start")
    else:
        size = full.stat().st_size
        if size > support._MAX_SHOW:
            prefix = support._command_prefix(args, meta)
            support._die(f"{t.path} is {size:,} bytes; pass --lines N:M or open it with "
                 f"$EDITOR $({prefix} path {shlex.quote(t.path)})")
        start, end = 1, None

    color = render.use_color(args.color)
    if not args.bare:
        sub = query.subsystem_for_target(conn, t)
        head = f"{t.path}:{start}" + (f"-{end}" if end else "")
        if t.kind == "symbol":
            head = f"{t.path}:{t.line}  {t.name}"
        label = sub["name"] if sub and sub["name"] not in query.CATCH_ALL else None
        print(render.paint(head, "1;36", color)
              + (render.paint(f"   [{label}]", "35", color) if label else "")
              + render.paint(f"   [{support._linux(meta)}]", "90", color))
    printed = 0
    try:
        with full.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i < start:
                    continue
                if end is not None and i > end:
                    break
                prefix = "" if args.bare else render.paint(f"{i:6} ", "90", color)
                print(prefix + line.rstrip("\n"))
                printed += 1
    except OSError as exc:
        support._die(f"cannot read {full}: {exc}")
    if printed == 0:
        support._die(f"{t.path} has no line {start}")
