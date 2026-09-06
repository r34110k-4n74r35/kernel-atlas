"""CLI handlers for browsing indexed paths, symbols, and subsystems."""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import replace

from .. import query, render
from ..query import Entry
from ..presentation.terminal import Console


def cmd_info(args, support):
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    t = res.target

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

    console = Console(args.color)
    console.heading(t.display)
    if res.note:
        console.note(res.note)
    console.blank()

    def field(k, v, tone=None):
        if v is not None and v != "":
            console.field(k, v, tone=tone)

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
            field("index status", path_row["index_status"],
                  "error" if path_row["index_error"] else None)
            if path_row["is_symlink"]:
                field("symlink to", path_row["link_target"] or "unknown")
            field("index error", path_row["index_error"], "error")
            if symbols_by_kind:
                field("defines", ", ".join(
                    f"{count} {kind}" for kind, count in symbols_by_kind.items()))

    if source_exists:
        field("on disk", on_disk)
    elif on_disk is not None:
        field("source path", f"{on_disk} (missing)", "warning")
    field("index", support._linux(meta))
    field("elixir", lnks.get("elixir"))
    if lnks.get("docs"):
        field("docs", lnks["docs"])
    if lnks.get("ident"):
        field("ident", lnks["ident"])

    if area:
        console.blank()
        console.heading(f"Area: {area[0]}", tone="success")
        console.text(area[1])

    if subs or unclassified is not None or unmatched_files:
        console.blank()
        heading = ("Subsystem composition (from descendant files)"
                   if t.kind == "dir" else "Subsystem (from MAINTAINERS)")
        console.heading(heading, tone="accent")
        for i, s in enumerate(subs[:args.max_subsystems]):
            primary = i == 0 if t.kind == "dir" else bool(s["is_primary"])
            marker = "*" if primary else "-"
            if t.kind == "dir":
                detail = (f"{s['n_primary']:,} primary / {s['n_claimed']:,} "
                          f"claimed descendant files ({s['coverage']:.0%})")
            else:
                detail = f"{s['n_files']:,} claimed files"
            if i:
                console.blank()
            console.text(f"{marker} {s['name']}", tone="bold")
            console.field("status", s["status"] or "unknown")
            console.field("ownership", detail)
            f = query.subsystem_json_fields(s)
            for who in f["maintainers"][:3]:
                console.field("maintainer", who)
            for lst in f["lists"][:2]:
                console.field("list", lst)
        if unclassified is not None:
            if t.kind == "dir":
                detail = (f"{unclassified['n_primary']:,} primary descendant "
                          f"files ({unclassified['coverage']:.0%})")
            else:
                detail = "the only primary ownership match for this file"
            console.note(f"Unclassified: {detail}; represented only by the "
                         f"{unclassified['name']} catch-all")
        if unmatched_files:
            if t.kind == "dir":
                detail = (f"{unmatched_files:,} descendant file"
                          f"{'s have' if unmatched_files != 1 else ' has'}")
            else:
                detail = "the containing file has"
            console.note(f"Unclassified: {detail} no primary MAINTAINERS match")
        if len(subs) > args.max_subsystems:
            console.text(f"... and {len(subs) - args.max_subsystems} more "
                         "(--max-subsystems to show)", tone="muted")
    elif not area:
        console.blank()
        console.note("No MAINTAINERS section claims this path.")

    anc = query.ancestry(conn, t.path)
    if anc:
        console.blank()
        console.heading("Path breakdown")
        console.table(["PATH", "SUBSYSTEM"],
                      [(p + "/", s or "-") for p, s in anc],
                      tones={0: "info", 1: "accent"})

    if res.candidates:
        console.blank()
        console.heading(f"{len(res.candidates)} other candidate(s) "
                        "for this name", tone="warning")
        for c in res.candidates[:args.max_candidates]:
            console.text(f"{c.display}  ({c.symbol_kind or c.kind})")
        if len(res.candidates) > args.max_candidates:
            console.text(f"... and {len(res.candidates) - args.max_candidates} more",
                         tone="muted")

    prefix = support._command_prefix(args, meta)
    target_arg = shlex.quote(support._target_spec(t))
    next_lines = [f"{prefix} siblings {target_arg}"]
    if t.kind == "symbol" and t.symbol_kind in {"struct", "union"}:
        next_lines.append(f"{prefix} struct {target_arg}")
    if lnks:
        next_lines.append(f"{prefix} web {target_arg}")
    console.commands(next_lines)


def cmd_siblings(args, support):
    support._validate_listing_output(args)
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    t = res.target
    scope = query.build_scope(conn, t, args.level)
    if scope.dir_sql is None and scope.file_sql is None and scope.sym_where is None:
        support._die(f"cannot build a '{args.level}' scope for {t.display} ({scope.label})")

    kinds = support.symbol_filter_kinds(args, support.kinds_from_args(args, t))
    support._reject_symbol_size_sort(args, kinds)
    root_subtree = (
        args.level == "subtree"
        and not (t.path if t.kind == "dir" else query.parent_path(t.path))
    )
    if ((args.level == "tree" or root_subtree)
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
    want_subsystem = (
        support._listing_has_columns(args)
        and (args.with_subsystem
             or "subsystem" in support._split_list(args.columns))
    )
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
    support._validate_listing_output(args)
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
    want_subsystem = (
        support._listing_has_columns(args)
        and (args.with_subsystem
             or "subsystem" in support._split_list(args.columns))
    )
    entries = query.collect(conn, scope, kinds, limit=args.limit,
                            grep=support._checked_grep(args.grep),
                            exported_only=args.exported, static=support._static_mode(args),
                            with_subsystem=want_subsystem, sort=args.sort)
    support.emit(entries, args, set(kinds), want_subsystem,
         f"{scope.label}  [{support._linux(meta)}]\n", index=support.index_version(meta))


def cmd_find(args, support):
    support._validate_listing_output(args)
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
    want_subsystem = (
        support._listing_has_columns(args)
        and (args.with_subsystem or not explicit_columns
             or "subsystem" in explicit_columns)
    )
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
    console = Console(args.color)
    console.heading(f"{len(rows)} subsystems  [{support._linux(meta)}]")
    console.table(
        ["CLAIMED", "PRIMARY", "STATUS", "NAME"],
        [(f"{r['n_files']:,}", f"{r['n_primary_files']:,}",
          r["status"] or "?", r["name"]) for r in rows],
        align_right=(0, 1), tones={0: "bold", 1: "bold", 3: "accent"},
    )
    if not rows:
        console.note("No subsystems match the requested filters.")


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
        console = Console(args.color)
        console.heading(f"{len(rows)} subsystems match {args.name!r}:",
                        tone="warning")
        console.table(
            ["CLAIMED", "PRIMARY", "NAME"],
            [(f"{r['n_files']:,}", f"{r['n_primary_files']:,}", r["name"])
             for r in rows],
            align_right=(0, 1), tones={0: "bold", 1: "bold", 2: "accent"},
        )
        console.text("Use the complete subsystem name to see its details.",
                     tone="muted")
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
    console = Console(args.color)
    console.heading(s["name"], tone="accent")
    console.field("index", support._linux(meta))
    console.field("status", s["status"] or "unknown")
    for who in f["maintainers"]:
        console.field("maintainer", who)
    for who in f["reviewers"][:5]:
        console.field("reviewer", who)
    for lst in f["lists"]:
        console.field("list", lst)
    for tree in f["trees"][:3]:
        console.field("git", tree)
    for website in f["websites"]:
        console.field("web", website)
    for url in f["patchwork"]:
        console.field("patchwork", url)
    for url in f["bugs"]:
        console.field("bugs", url)
    for chat in f["chats"]:
        console.field("chat", chat)
    for profile in f["profiles"]:
        console.field("profile", profile)
    if f["keywords"]:
        console.field("keywords", ", ".join(f["keywords"]))
    console.field("files", f"{s['n_files']:,} claimed, "
                  f"{s['n_primary_files']:,} primary")

    console.blank()
    console.heading("Directory composition")
    console.table(
        ["DIRECTORY", "PRIMARY", "CLAIMED", "COVERAGE"],
        [(r["path"] + "/", f"{r['n_primary']:,}", f"{r['n_claimed']:,}",
          f"{r['coverage']:.1%}") for r in directory_rows],
        align_right=(1, 2, 3), tones={0: "info", 1: "bold", 3: "success"},
    )
    if not directory_rows:
        console.text("No indexed subdirectories.", tone="muted")

    if args.files:
        console.blank()
        console.heading("Files")
        for r in conn.execute(
            "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
            " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path", (s["id"],)
        ):
            console.text(r["path"], tone="info")


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
        " WHERE (path = ? OR path GLOB ?) AND depth <= ? ORDER BY path",
        (base, query.glob_under(base), base_depth + max_depth)).fetchall()
    entries = [Entry(kind="dir", name=r["name"], path=r["path"],
                     n_files=r["n_files"], n_subdirs=r["n_subdirs"])
               for r in rows if r["path"]]
    if args.files:
        # Visual depth: files `max_depth` components below `base`, not one
        # extra level deeper than the directories (the old Python filter).
        slash_max = (base.count("/") + max_depth) if base else (max_depth - 1)
        pattern = query.glob_under(base)
        if slash_max >= 0:
            frows = conn.execute(
                "SELECT path, name, size, lines, n_symbols FROM files"
                f" WHERE path GLOB ? AND {support._SLASH_COUNT} <= ? ORDER BY path",
                (pattern, slash_max)).fetchall()
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
    console = Console(args.color)
    console.heading(f"{base or 'kernel root'}/")
    sys.stdout.write(render.render_tree(relative, color))
    console.blank()
    console.text(f"{len(entries)} entries (depth {max_depth})  [{support._linux(meta)}]",
                 tone="muted", indent=0)
