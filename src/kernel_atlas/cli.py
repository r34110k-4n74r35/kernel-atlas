"""Command line interface for kernel-atlas."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from . import config, cparse, db, indexer, kernelsrc, maintainers, query, render
from .query import Entry

PROG = "kernel-atlas"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _die(msg: str, code: int = 1):
    print(f"{PROG}: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]


def _version_key(path: Path) -> tuple:
    """Sort '7.2' above '6.18.45'. Non-numeric stems sort last."""
    try:
        return (1, tuple(int(p) for p in path.stem.split("-")[0].split(".")))
    except ValueError:
        return (0, ())


def resolve_index_spec(spec: str) -> Path:
    """Turn a version or unique version prefix into an index path, or die."""
    path = config.index_path(spec)
    if path.is_file():
        return path
    matches = [p for p in config.list_indexes() if p.stem.startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _die(f"{spec!r} is ambiguous: " + ", ".join(p.stem for p in matches))
    have = ", ".join(p.stem for p in config.list_indexes()) or "none built yet"
    _die(f"no index for {spec!r} (built: {have})")


def default_index(*, warn: bool = True) -> Path:
    """The index used when neither --db nor -K is given.

    Precedence: the version pinned with `{PROG} use`, then the highest built
    version — which is predictable, unlike file modification times.
    """
    available = config.list_indexes()
    if not available:
        _die(f"no index built yet — run '{PROG} build lts' first")
    pinned = config.get_default_version()
    if pinned:
        path = config.index_path(pinned)
        if path.is_file():
            return path
        if warn:
            print(f"{PROG}: pinned version {pinned} has no index any more; "
                  f"falling back to the highest built version "
                  f"(fix with '{PROG} use <version>' or '{PROG} use --clear')",
                  file=sys.stderr)
    return max(available, key=_version_key)


def open_index(args) -> tuple[sqlite3.Connection, dict]:
    if getattr(args, "db", None):
        path = Path(args.db).expanduser()
    elif getattr(args, "kernel", None):
        path = resolve_index_spec(args.kernel)
    else:
        path = default_index()
    if not path.is_file():
        _die(f"no index at {path} — run '{PROG} build <version>' first")
    try:
        conn = db.connect(path, readonly=True)
        meta = db.get_meta(conn)
    except sqlite3.DatabaseError as exc:
        _die(f"{path} is not a usable index ({exc}) — rebuild it with "
             f"'{PROG} build <version> --force'")
    if not meta.get("kernel_version"):
        _die(f"{path} looks like an interrupted build — rebuild it with "
             f"'{PROG} build <version> --force'")
    return conn, meta


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
            like = trimmed.replace("%", "\\%") + "%"
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


def resolve_or_die(conn, spec: str) -> query.Resolution:
    res = query.resolve(conn, spec)
    if res.target is None:
        near = _suggestions(conn, spec)
        hint = "\n  did you mean: " + ", ".join(near) if near else ""
        _die(res.note + hint)
    return res


def pick_columns(args, kinds_listed: set[str], with_subsystem: bool) -> list[str]:
    if getattr(args, "columns", None):
        cols = _split_list(args.columns)
        bad = [c for c in cols if c not in render.COLUMNS]
        if bad:
            _die(f"unknown column(s): {', '.join(bad)}"
                 f" — valid: {', '.join(render.COLUMNS)}")
        return cols
    only_dirs = kinds_listed <= {"dir"}
    only_files = kinds_listed <= {"file"}
    only_syms = kinds_listed and not (kinds_listed & {"dir", "file"})
    if only_dirs:
        cols = ["kind", "name", "subdirs", "files"]
    elif only_files:
        cols = ["kind", "name", "lines", "size", "symbols"]
    elif only_syms:
        cols = ["kind", "name", "line", "span", "flags", "signature"]
    else:
        cols = ["kind", "name", "path", "line"]
    if with_subsystem and "subsystem" not in cols:
        cols.insert(2 if len(cols) > 2 else len(cols), "subsystem")
    return cols


def emit(entries: list[Entry], args, kinds_listed: set[str], with_subsystem: bool,
         header: str = ""):
    fmt = args.format
    machine = fmt in ("json", "csv", "names", "plain")
    color = render.use_color(args.color)
    cols = pick_columns(args, kinds_listed, with_subsystem)
    if not machine and header:
        print(render.paint(header, "1", color))
    text = render.render(entries, cols, fmt, color, render.term_width())
    sys.stdout.write(text)
    if not machine:
        n = len(entries)
        print(render.paint(f"\n{n} result{'s' if n != 1 else ''}", "90", color))


def kinds_from_args(args, target) -> tuple[str, ...]:
    raw = _split_list(getattr(args, "kinds", None))
    if not raw:
        return query.default_kinds(target) if target else ("dir", "file")
    out: list[str] = []
    for k in raw:
        k = k.lower()
        if k == "all":
            return query.ALL_KINDS
        if k in ("symbol", "symbols"):
            out.extend(query.SYMBOL_KINDS)
        elif k in ("path", "paths"):
            out.extend(query.PATH_KINDS)
        elif k in ("func", "fn", "functions"):
            out.extend(("function", "syscall"))
        elif k in ("type", "types"):
            out.extend(("struct", "union", "enum", "typedef"))
        elif k.rstrip("s") in query.ALL_KINDS:
            out.append(k.rstrip("s"))
        elif k in query.ALL_KINDS:
            out.append(k)
        else:
            _die(f"unknown kind {k!r} (valid: {', '.join(query.ALL_KINDS)}, "
                 f"or all/symbols/paths/functions/types)")
    return tuple(dict.fromkeys(out))


def source_tree(meta: dict) -> Path:
    tree = config.tree_for(meta.get("kernel_version", ""), meta.get("tree_path"))
    if tree is None:
        version = meta.get("kernel_version", "?")
        _die(f"the source for Linux {version} is not on disk "
             f"(expected {config.source_path(version)})\n"
             f"  the index still answers queries; to get the source back run:\n"
             f"    {PROG} build {version} --force")
    return tree


def _static_mode(args) -> str:
    if getattr(args, "static_only", False):
        return "only"
    if getattr(args, "no_static", False):
        return "exclude"
    return "any"


def _checked_grep(pattern: str | None) -> str | None:
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            _die(f"--grep {pattern!r} is not a valid regex: {exc}")
    return pattern


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_versions(args):
    try:
        releases = kernelsrc.list_releases()
    except OSError as exc:
        _die(f"could not reach kernel.org ({exc})")
    color = render.use_color(args.color)
    if args.format == "json":
        sys.stdout.write(render.render_json([r.__dict__ for r in releases]))
        return
    print(render.paint("Current kernel.org releases", "1", color))
    print(f"  {'MONIKER':<12} {'VERSION':<16} {'RELEASED':<12}")
    for r in releases:
        note = "  <- good default for learning" if r.moniker == "longterm" else ""
        print(f"  {r.moniker:<12} {r.version:<16} {r.released or '-':<12}"
              + render.paint(note, "32", color))
    print(f"\nBuild one with:  {PROG} build <version|lts|stable|mainline>")


def cmd_build(args):
    quiet = args.quiet
    if args.src:
        tree = Path(args.src).expanduser().resolve()
        if not (tree / "MAINTAINERS").is_file():
            _die(f"{tree} does not look like a kernel tree (no MAINTAINERS file)")
        version = args.version or kernelsrc.detect_version(tree) or tree.name
        source = str(tree)
    else:
        spec = args.version or "lts"
        try:
            rel = kernelsrc.resolve_version(spec)
        except (OSError, LookupError, ValueError) as exc:
            _die(str(exc))
        version = rel.version
        if not quiet:
            print(f"kernel {version} ({rel.moniker})", file=sys.stderr)
        out_existing = config.index_path(version)
        if out_existing.is_file() and not args.force:
            _die(f"index for {version} already exists at {out_existing} "
                 f"(use --force to rebuild)")
        try:
            tree = kernelsrc.ensure_source(version, keep_tarball=args.keep_tarball,
                                           quiet=quiet, verify=not args.no_verify)
        except (OSError, RuntimeError) as exc:
            _die(f"could not obtain kernel source: {exc}")
        source = kernelsrc.tarball_url(version)

    out = Path(args.output).expanduser() if args.output else config.index_path(version)
    if out.is_file() and not args.force:
        _die(f"index already exists at {out} (use --force to rebuild)")

    kinds = _split_list(args.kinds) or list(cparse.DEFAULT_KINDS)
    bad = [k for k in kinds if k not in cparse.ALL_KINDS]
    if bad:
        _die(f"unknown symbol kind(s): {', '.join(bad)} "
             f"(valid: {', '.join(cparse.ALL_KINDS)})")

    if not quiet:
        print(f"indexing {tree}", file=sys.stderr)
    stats = indexer.build(tree, out, version, kinds=kinds, want_calls=args.with_calls,
                          jobs=args.jobs, quiet=quiet, source=source)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(
        f"\nBuilt index for Linux {version}\n"
        f"  {stats.dirs:,} directories, {stats.files:,} files\n"
        f"  {stats.symbols:,} symbols from {stats.parsed:,} C files\n"
        + (f"  {stats.calls:,} call edges\n" if stats.calls else "")
        + f"  {stats.subsystems:,} subsystems from MAINTAINERS\n"
        f"  {out}  ({size_mb:.0f} MB, {stats.seconds:.0f}s)\n"
        f"\nTry:  {PROG} info net/ipv4\n"
        f"      {PROG} siblings net/ipv4/tcp.c"
    )


def cmd_indexes(args):
    paths = config.list_indexes()
    if not paths:
        print(f"no indexes yet — run '{PROG} build lts'")
        return
    active = default_index() if paths else None
    rows = []
    for p in sorted(paths, key=_version_key, reverse=True):
        try:
            conn = db.connect(p, readonly=True)
            meta = db.get_meta(conn)
            conn.close()
        except Exception:
            meta = {}
        source_here = config.tree_for(meta.get("kernel_version", p.stem),
                                      meta.get("tree_path")) is not None
        rows.append({
            "version": meta.get("kernel_version", p.stem),
            "files": meta.get("n_files", "?"),
            "symbols": meta.get("n_symbols", "?"),
            "calls": meta.get("has_calls") == "1",
            "source": source_here,
            "built_at": meta.get("built_at", "?"),
            "size": f"{p.stat().st_size / 1048576:.0f} MB",
            "default": p == active,
            "path": str(p),
        })
    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return
    color = render.use_color(args.color)
    print(f"    {'VERSION':<12} {'FILES':>8} {'SYMBOLS':>10} {'CALLS':<6} "
          f"{'SOURCE':<7} {'BUILT':<20} {'SIZE':>8}")
    for r in rows:
        mark = "*" if r["default"] else " "
        line = (f"  {mark} {r['version']:<12} {r['files']:>8} {r['symbols']:>10} "
                f"{'yes' if r['calls'] else '-':<6} "
                f"{'yes' if r['source'] else '-':<7} {r['built_at']:<20} "
                f"{r['size']:>8}")
        print(render.paint(line, "1", color) if r["default"] else line)
    pinned = config.get_default_version()
    note = (f"pinned with '{PROG} use {pinned}'" if pinned
            else f"highest version (pin one with '{PROG} use <version>')")
    print(render.paint(f"\n  * = default index — {note}", "90", color))


def cmd_use(args):
    if args.clear and args.version:
        _die("pass a version or --clear, not both")
    if args.clear:
        was = config.get_default_version()
        config.clear_default_version()
        if was:
            print(f"cleared pin on {was}; the highest built version is the default again")
        else:
            print("nothing was pinned")
        return
    if not args.version:
        available = config.list_indexes()
        pinned = config.get_default_version()
        if not available:
            print("no indexes built yet — run "
                  f"'{PROG} build lts', then '{PROG} use <version>'")
            return
        if pinned:
            pin_path = config.index_path(pinned)
            if pin_path.is_file():
                print(f"pinned: {pinned}")
            else:
                print(f"pinned: {pinned}  (index is gone — "
                      f"'{PROG} use --clear' or '{PROG} use <version>')")
        else:
            print("nothing pinned; defaulting to the highest built version")
        active = default_index(warn=False)
        print(f"active index: {active.stem}  ({active})")
        return
    path = resolve_index_spec(args.version)
    config.set_default_version(path.stem)
    print(f"default index is now {path.stem}\n"
          f"  every command without -K/--db will use it; "
          f"undo with '{PROG} use --clear'")


def _unlink_index(path: Path) -> int:
    """Delete an index and any SQLite sidecar files. Returns bytes freed."""
    freed = 0
    for extra in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
                  path.with_suffix(".db-journal")):
        if extra.is_file():
            freed += extra.stat().st_size
            extra.unlink()
    return freed


def cmd_remove(args):
    # Resolve everything first so 'remove 6.18 6.18.45' (the second is the
    # expansion of the first) does not fail halfway through.
    unique: list[Path] = []
    for spec in args.versions:
        path = resolve_index_spec(spec)
        if path not in unique:
            unique.append(path)

    freed = 0
    for path in unique:
        version = path.stem
        size = _unlink_index(path)
        freed += size
        print(f"removed index   {path}  ({size / 1048576:.0f} MB)")

        if config.get_default_version() == version:
            config.clear_default_version()
            print("  (it was the pinned default; the pin has been cleared)")

        tree = config.source_path(version)
        if args.source:
            if tree.is_dir():
                try:
                    shutil.rmtree(tree)
                except OSError as exc:
                    print(f"  could not remove source {tree}: {exc}",
                          file=sys.stderr)
                else:
                    print(f"removed source  {tree}")
            else:
                print(f"no source tree at {tree}")
        elif tree.is_dir():
            print(f"  (source kept at {tree}; remove it too with --source)")
    print(f"\nfreed {freed / 1048576:.0f} MB of index files"
          + (" (source trees not counted)" if args.source else ""))


def cmd_stats(args):
    conn, meta = open_index(args)
    if args.format == "json":
        extra = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind")}
        sys.stdout.write(render.render_json({"meta": meta, "symbols_by_kind": extra}))
        return
    color = render.use_color(args.color)
    print(render.paint(f"Linux {meta.get('kernel_version', '?')} index", "1", color))
    print(f"  built        {meta.get('built_at', '?')}")
    print(f"  source       {meta.get('source', '?')}")
    print(f"  directories  {int(meta.get('n_dirs', 0)):,}")
    print(f"  files        {int(meta.get('n_files', 0)):,}")
    print(f"  subsystems   {int(meta.get('n_subsystems', 0)):,}")
    print(f"  symbols      {int(meta.get('n_symbols', 0)):,}")
    for r in conn.execute("SELECT kind, COUNT(*) n FROM symbols GROUP BY kind"
                          " ORDER BY n DESC"):
        print(f"      {r['kind']:<12} {r['n']:>9,}")
    print(render.paint("\n  largest top-level areas", "1", color))
    for r in conn.execute(
        "SELECT d.name, COUNT(f.id) n FROM dirs d JOIN files f"
        " ON f.path LIKE d.path || '/%' WHERE d.depth = 1"
        " GROUP BY d.id ORDER BY n DESC LIMIT 8"
    ):
        area = maintainers.TOP_LEVEL_AREAS.get(r["name"])
        label = f"{area[0]}" if area else ""
        print(f"      {r['name']:<14} {r['n']:>7,} files   {label}")


def cmd_info(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    color = render.use_color(args.color)

    subs = query.all_subsystems(
        conn, "dir" if t.kind == "dir" else "file",
        t.id if t.kind == "dir" else (t.file_id or t.id))
    area = query.describe_area(t.path)

    if args.format == "json":
        payload = {
            "target": {
                "kind": t.kind, "symbol_kind": t.symbol_kind, "name": t.name,
                "path": t.path, "line": t.line, "end_line": t.end_line,
                "signature": t.signature, "is_static": t.is_static,
                "is_exported": t.is_exported,
            },
            "area": {"name": area[0], "description": area[1]} if area else None,
            "subsystems": [
                dict(name=s["name"], status=s["status"], n_files=s["n_files"],
                     **query.subsystem_json_fields(s)) for s in subs],
            "ancestry": [{"path": p, "subsystem": s}
                         for p, s in query.ancestry(conn, t.path)],
            "note": res.note,
            "other_candidates": [c.display for c in res.candidates[:20]],
        }
        sys.stdout.write(render.render_json(payload))
        return

    print(render.paint(t.display, "1;36", color))
    if res.note:
        print(render.paint(f"  ({res.note})", "33", color))
    print()

    def field(k, v):
        if v:
            print(f"  {k:<12} {v}")

    if t.kind == "symbol":
        field("kind", t.symbol_kind)
        field("defined in", f"{t.path}:{t.line}"
              + (f"-{t.end_line} ({t.end_line - t.line + 1} lines)"
                 if t.end_line and t.line else ""))
        field("signature", t.signature)
        field("linkage", "EXPORT_SYMBOL (available to modules)" if t.is_exported
              else ("static (file-local)" if t.is_static else "global"))
    else:
        row = conn.execute(
            f"SELECT * FROM {'dirs' if t.kind == 'dir' else 'files'} WHERE id = ?",
            (t.id,)).fetchone()
        field("kind", "directory" if t.kind == "dir" else "file")
        field("path", t.path or "<kernel root>")
        if t.kind == "dir":
            field("contains", f"{row['n_subdirs']} subdirectories, "
                              f"{row['n_files']} files")
            total = conn.execute(
                "SELECT COUNT(*) n FROM files WHERE path LIKE ?",
                (f"{t.path}/%" if t.path else "%",)).fetchone()["n"]
            if total != row["n_files"]:
                field("subtree", f"{total:,} files in total")
        else:
            field("size", f"{row['size']:,} bytes, {row['lines']:,} lines")
            by_kind = conn.execute(
                "SELECT kind, COUNT(*) n FROM symbols WHERE file_id = ?"
                " GROUP BY kind ORDER BY n DESC", (t.id,)).fetchall()
            if by_kind:
                field("defines", ", ".join(f"{r['n']} {r['kind']}" for r in by_kind))

    tree = config.tree_for(meta.get("kernel_version", ""), meta.get("tree_path"))
    if tree is not None:
        field("on disk", str(tree / t.path if t.path else tree))

    if area:
        print()
        print(render.paint(f"  Area: {area[0]}", "1;32", color))
        print(f"    {area[1]}")

    if subs:
        print()
        print(render.paint("  Subsystem (from MAINTAINERS)", "1;35", color))
        for i, s in enumerate(subs[:args.max_subsystems]):
            marker = "*" if i == 0 else " "
            print(f"   {marker} {render.paint(s['name'], '1', color)}"
                  f"   [{s['status'] or 'unknown'}]  {s['n_files']:,} files")
            f = query.subsystem_json_fields(s)
            for who in f["maintainers"][:3]:
                print(f"       maintainer  {who}")
            for lst in f["lists"][:2]:
                print(f"       list        {lst}")
        if len(subs) > args.max_subsystems:
            print(f"     ... and {len(subs) - args.max_subsystems} more "
                  f"(--max-subsystems to show)")
    else:
        print("\n  No MAINTAINERS section claims this path.")

    anc = query.ancestry(conn, t.path)
    if anc:
        print()
        print(render.paint("  Path breakdown", "1", color))
        for p, s in anc:
            print(f"    {p + '/':<38} {s or '-'}")

    if res.candidates:
        print()
        print(render.paint(f"  {len(res.candidates)} other definition(s) "
                           f"of this name", "33", color))
        for c in res.candidates[:args.max_candidates]:
            print(f"    {c.display}  ({c.symbol_kind or c.kind})")
        if len(res.candidates) > args.max_candidates:
            print(f"    ... and {len(res.candidates) - args.max_candidates} more")

    print(f"\n  Next:  {PROG} siblings {t.display}")


def cmd_siblings(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    scope = query.build_scope(conn, t, args.level)
    if scope.dir_sql is None and scope.file_sql is None and scope.sym_where is None:
        _die(f"cannot build a '{args.level}' scope for {t.display} ({scope.label})")

    kinds = kinds_from_args(args, t)
    # Fetch one extra row so that dropping the target itself does not eat one
    # of the requested rows; subsystems are looked up only for what survives.
    entries = query.collect(
        conn, scope, kinds, limit=args.limit + 1 if args.limit else 0,
        grep=_checked_grep(args.grep),
        exported_only=args.exported, static=_static_mode(args),
        with_subsystem=False, sort=args.sort)

    if not args.include_self:
        entries = [e for e in entries
                   if not (e.path == t.path and e.name == t.name
                           and (t.kind != "symbol" or e.line == t.line))]
    else:
        for e in entries:
            if e.path == t.path and e.name == t.name:
                e.is_target = True

    if args.limit:
        entries = entries[:args.limit]
    if args.with_subsystem:
        query.annotate_subsystems(conn, entries)

    sub = query.subsystem_for_target(conn, t)
    label = sub["name"] if sub else None
    if label in query.CATCH_ALL:
        area = query.describe_area(t.path)
        label = area[0] if area else label
    header = (f"Siblings of {t.display}\n"
              f"  level: {scope.label}"
              + (f"   subsystem: {label}" if label else "")
              + f"   showing: {', '.join(kinds)}\n")
    emit(entries, args, set(kinds), args.with_subsystem, header)


def cmd_ls(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target or "")
    t = res.target
    if t.kind == "symbol":
        _die(f"{t.display} is a symbol; try '{PROG} siblings {t.display}'")

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

    kinds = kinds_from_args(args, t) if _split_list(args.kinds) else default
    entries = query.collect(conn, scope, kinds, limit=args.limit,
                            grep=_checked_grep(args.grep),
                            exported_only=args.exported, static=_static_mode(args),
                            with_subsystem=args.with_subsystem, sort=args.sort)
    emit(entries, args, set(kinds), args.with_subsystem, f"{scope.label}\n")


def _post_filter(entries, args):
    """Apply --grep and the static filters to already-fetched entries."""
    pattern = _checked_grep(getattr(args, "grep", None))
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        entries = [e for e in entries if rx.search(e.name)]
    if getattr(args, "static_only", False):
        entries = [e for e in entries if e.is_static]
    elif getattr(args, "no_static", False):
        entries = [e for e in entries if not e.is_static]
    return entries


def cmd_find(args):
    conn, meta = open_index(args)
    mode = "exact" if args.exact else ("glob" if args.glob else
                                       ("prefix" if args.prefix else "substring"))
    if _split_list(args.kinds):
        kinds = [k for k in kinds_from_args(args, None) if k in query.SYMBOL_KINDS]
        if not kinds:
            _die("find only searches symbols; try --kinds function,struct,...")
    else:
        kinds = []
    # Over-fetch when a Python-side filter will shrink the result set.
    narrowing = args.grep or args.static_only or args.no_static
    limit = args.limit or 50
    entries = query.search(conn, args.pattern, kinds=kinds, mode=mode,
                           limit=limit * 20 if narrowing else limit,
                           exported_only=args.exported,
                           with_subsystem=False)
    entries = _post_filter(entries, args)[:limit]
    if args.format != "names":
        query.annotate_subsystems(conn, entries)
    cols = _split_list(args.columns) or ["kind", "name", "path", "line", "subsystem"]
    args.columns = ",".join(cols)
    emit(entries, args, {"function"}, True,
         f"Symbols matching {args.pattern!r} ({mode})\n")


def cmd_subsystems(args):
    conn, meta = open_index(args)
    rows = conn.execute(
        "SELECT * FROM subsystems "
        f"ORDER BY {'n_files DESC' if args.sort == 'size' else 'name'}").fetchall()
    pattern = _checked_grep(args.grep)
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        rows = [r for r in rows if rx.search(r["name"] or "")]
    if args.limit:
        rows = rows[:args.limit]
    if args.format == "json":
        sys.stdout.write(render.render_json(
            [dict(name=r["name"], status=r["status"], n_files=r["n_files"],
                  **query.subsystem_json_fields(r)) for r in rows]))
        return
    color = render.use_color(args.color)
    print(render.paint(f"{len(rows)} subsystems", "1", color))
    for r in rows:
        print(f"  {r['n_files']:>6,}  {r['status'] or '?':<16} {r['name']}")


def cmd_subsystem(args):
    conn, meta = open_index(args)
    rows = query.subsystem_by_name(conn, args.name)
    if not rows:
        _die(f"no subsystem matching {args.name!r} "
             f"(try '{PROG} subsystems --grep {args.name}')")
    if len(rows) > 1 and rows[0]["name"].lower() != args.name.lower():
        color = render.use_color(args.color)
        print(render.paint(f"{len(rows)} subsystems match {args.name!r}:", "1", color))
        for r in rows:
            print(f"  {r['n_files']:>6,}  {r['name']}")
        return
    s = rows[0]
    f = query.subsystem_json_fields(s)
    if args.format == "json":
        files = [r["path"] for r in conn.execute(
            "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
            " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path", (s["id"],))]
        sys.stdout.write(render.render_json(
            dict(name=s["name"], status=s["status"], n_files=s["n_files"],
                 web=s["web"], files=files, **f)))
        return
    color = render.use_color(args.color)
    print(render.paint(s["name"], "1;35", color))
    print(f"  status       {s['status'] or 'unknown'}")
    for who in f["maintainers"]:
        print(f"  maintainer   {who}")
    for who in f["reviewers"][:5]:
        print(f"  reviewer     {who}")
    for lst in f["lists"]:
        print(f"  list         {lst}")
    for tree in f["trees"][:3]:
        print(f"  git          {tree}")
    if s["web"]:
        print(f"  web          {s['web']}")
    print(f"  files        {s['n_files']:,}")

    print(render.paint("\n  Top directories", "1", color))
    for r in conn.execute(
        "SELECT d.path, d.n_files FROM dirs d JOIN path_subsys p"
        " ON p.ref_kind='dir' AND p.ref_id=d.id WHERE p.subsystem_id=?"
        " ORDER BY d.n_files DESC, d.path LIMIT ?", (s["id"], args.limit or 15)
    ):
        print(f"    {r['path'] + '/':<50} {r['n_files']:>5} files")

    if args.files:
        print(render.paint("\n  Files", "1", color))
        for r in conn.execute(
            "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
            " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path", (s["id"],)
        ):
            print(f"    {r['path']}")


def cmd_tree(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target or "")
    t = res.target
    base = t.path if t.kind == "dir" else t.path.rsplit("/", 1)[0]
    color = render.use_color(args.color)
    max_depth = args.depth
    base_depth = base.count("/") + 1 if base else 0

    rows = conn.execute(
        "SELECT path, name, depth, n_files, n_subdirs FROM dirs"
        " WHERE (path = ? OR path LIKE ?) AND depth <= ? ORDER BY path",
        (base, f"{base}/%" if base else "%", base_depth + max_depth)).fetchall()
    entries = [Entry(kind="dir", name=r["name"], path=r["path"],
                     n_files=r["n_files"], n_subdirs=r["n_subdirs"])
               for r in rows if r["path"]]
    if args.files:
        frows = conn.execute(
            "SELECT path, name, size, lines, n_symbols FROM files"
            " WHERE path LIKE ? ORDER BY path",
            (f"{base}/%" if base else "%",)).fetchall()
        entries += [Entry(kind="file", name=r["name"], path=r["path"],
                          size=r["size"], lines=r["lines"], n_symbols=r["n_symbols"])
                    for r in frows
                    if r["path"].count("/") <= base_depth + max_depth]
    entries = [e for e in entries if e.path != base]
    if args.format == "json":
        sys.stdout.write(render.render_json([render.entry_dict(e) for e in entries]))
        return

    # render_tree nests on path components, so strip the base to avoid redrawing
    # the ancestors of the directory the user asked about.
    prefix = f"{base}/" if base else ""
    relative = [replace(e, path=e.path[len(prefix):]) for e in entries]
    print(render.paint(f"{base or 'kernel root'}/", "1;34", color))
    sys.stdout.write(render.render_tree(relative, color))
    print(render.paint(f"\n{len(entries)} entries (depth {max_depth})", "90", color))


def cmd_path(args):
    """Print the on-disk path, so `$EDITOR $(ka path tcp_sendmsg)` just works."""
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    tree = source_tree(meta)
    full = tree / t.path if t.path else tree
    if args.line and t.kind == "symbol":
        print(f"{full}:{t.line}")
    else:
        print(full)


def cmd_show(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    if t.kind == "dir":
        _die(f"{t.path} is a directory; try '{PROG} ls {t.path}'")
    tree = source_tree(meta)
    full = tree / t.path
    if not full.is_file():
        _die(f"{full} is missing from the source tree")

    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
    if t.kind == "symbol":
        start = max(1, (t.line or 1) - args.context)
        end = min(len(lines), (t.end_line or t.line or 1) + args.context)
    elif args.lines:
        m = re.fullmatch(r"(\d+)(?:[:-](\d+))?", args.lines)
        if not m:
            _die(f"--lines wants N or N:M, not {args.lines!r}")
        start = max(1, int(m.group(1)))
        end = min(len(lines), int(m.group(2)) if m.group(2) else start)
    else:
        start, end = 1, len(lines)

    color = render.use_color(args.color)
    if not args.bare:
        sub = query.subsystem_for_target(conn, t)
        head = f"{t.path}:{start}-{end}"
        if t.kind == "symbol":
            head = f"{t.path}:{t.line}  {t.name}"
        print(render.paint(head, "1;36", color)
              + (render.paint(f"   [{sub['name']}]", "35", color) if sub else ""))
    for i in range(start, end + 1):
        prefix = "" if args.bare else render.paint(f"{i:6} ", "90", color)
        print(prefix + lines[i - 1])


_FRAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\+\s*0x")
_IDENT_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\b")


def _frames_from_text(text: str) -> list[str]:
    """Pull symbol names out of an oops / ftrace / gdb style backtrace."""
    frames: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _FRAME_RE.findall(line)
        if m:
            frames.extend(m)
            continue
        # 'tcp_sendmsg' or '#3  0x... in tcp_sendmsg (...)' or a bare name per line
        if " in " in line:
            tail = line.split(" in ", 1)[1]
            cand = _IDENT_RE.findall(tail)
            if cand:
                frames.append(cand[0])
                continue
        cand = _IDENT_RE.findall(line)
        if len(cand) == 1:
            frames.append(cand[0])
    seen: dict[str, None] = {}
    for f in frames:
        seen.setdefault(f, None)
    return list(seen)


def cmd_trace(args):
    conn, meta = open_index(args)
    if args.frames:
        text = "\n".join(args.frames)
    else:
        if sys.stdin.isatty():
            _die("paste a backtrace on stdin, or pass frame names as arguments")
        text = sys.stdin.read()
    frames = _frames_from_text(text)
    if not frames:
        _die("could not find any symbol names in that input")

    results = []
    for name in frames[:args.limit or 100]:
        res = query.resolve(conn, name)
        t = res.target
        if t is None or t.kind != "symbol":
            results.append({"frame": name, "found": False})
            continue
        sub = query.subsystem_for_target(conn, t)
        area = query.describe_area(t.path)
        name_of = sub["name"] if sub else None
        specific = name_of if name_of not in query.CATCH_ALL else None
        results.append({
            "frame": name, "found": True, "symbol_kind": t.symbol_kind,
            "path": t.path, "line": t.line,
            "subsystem": name_of,
            "status": sub["status"] if sub else None,
            "area": area[0] if area else None,
            # What to show: a precise subsystem beats the catch-all section.
            "label": specific or (area[0] if area else name_of) or "?",
            "ambiguous": len(res.candidates),
        })

    if args.format == "json":
        sys.stdout.write(render.render_json(results))
        return

    color = render.use_color(args.color)
    print(render.paint(f"Backtrace across {len(results)} frames "
                       f"(Linux {meta.get('kernel_version', '?')})\n", "1", color))
    wname = max((len(r["frame"]) for r in results), default=10)
    for i, r in enumerate(results):
        idx = render.paint(f"#{i:<2}", "90", color)
        if not r["found"]:
            print(f"  {idx} {r['frame']:<{wname}}  {render.paint('not in index', '90', color)}")
            continue
        loc = f"{r['path']}:{r['line']}"
        sub = r["label"]
        amb = f"  (+{r['ambiguous']} more defs)" if r["ambiguous"] else ""
        print(f"  {idx} {render.paint(r['frame'].ljust(wname), '32', color)}  "
              f"{loc:<44} {render.paint(sub, '35', color)}{amb}")

    counts: dict[str, int] = {}
    for r in results:
        if r["found"]:
            key = r["area"] or r["subsystem"] or "?"
            counts[key] = counts.get(key, 0) + 1
    if counts:
        print(render.paint("\n  Areas touched", "1", color))
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<24} {v} frame{'s' if v != 1 else ''}")


def cmd_calls(args):
    conn, meta = open_index(args)
    if db.get_meta(conn).get("has_calls") != "1":
        _die(f"this index has no call graph — rebuild with "
             f"'{PROG} build <version> --with-calls'")
    res = resolve_or_die(conn, args.target)
    t = res.target
    if t.kind != "symbol":
        _die(f"{t.display} is not a symbol")

    if args.callers:
        entries = _post_filter(query.callers(conn, t.name, limit=args.limit or 100),
                               args)
        query.annotate_subsystems(conn, entries)
        args.columns = args.columns or "kind,name,path,line,subsystem"
        emit(entries, args, {"function"}, True, f"Functions that call {t.name}\n")
        return

    names = query.callees(conn, t.id, limit=args.limit or 200)
    entries: list[Entry] = []
    for n in names:
        r = query.resolve(conn, n)
        if r.target is not None and r.target.kind == "symbol":
            entries.append(Entry(kind=r.target.symbol_kind or "function", name=n,
                                 path=r.target.path, line=r.target.line))
        else:
            # A callee with no definition anywhere in the index: usually a
            # compiler builtin or a macro the parser could not attribute.
            entries.append(Entry(kind="?", name=n, path="-"))
    entries = _post_filter(entries, args)
    query.annotate_subsystems(conn, entries)
    args.columns = args.columns or "kind,name,path,line,subsystem"
    emit(entries, args, {"function"}, True, f"Called by {t.display}\n")


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def _add_output_opts(p, sorts=True):
    g = p.add_argument_group("output")
    g.add_argument("--format", "-f", default="table",
                   choices=("table", "plain", "names", "json", "csv", "tree"),
                   help="output format (default: table)")
    g.add_argument("--columns", "-c",
                   help="comma-separated columns: " + ",".join(render.COLUMNS))
    g.add_argument("--limit", "-n", type=int, default=0, help="max rows (0 = all)")
    g.add_argument("--grep", "-g", help="only names matching this regex")
    if sorts:
        g.add_argument("--sort", default="name",
                       choices=("name", "path", "kind", "line", "size", "lines"))
    g.add_argument("--with-subsystem", "-S", action="store_true",
                   help="add a subsystem column")


def _add_filter_opts(p):
    g = p.add_argument_group("filters")
    g.add_argument("--kinds", "-k",
                   help="what to list: dir,file,function,syscall,struct,union,enum,"
                        "typedef,macro,variable,prototype — or all/symbols/paths/"
                        "functions/types")
    g.add_argument("--exported", action="store_true",
                   help="only EXPORT_SYMBOL'd symbols")
    g.add_argument("--static-only", action="store_true", help="only static symbols")
    g.add_argument("--no-static", action="store_true", help="hide static symbols")


def _global_opts(parser, suppress: bool):
    """Accept --kernel/--db/--color before *or* after the subcommand.

    Subcommand copies use SUPPRESS so they only override when actually given,
    instead of clobbering the top-level value with their own default.
    """
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    g = parser.add_argument_group("index selection")
    g.add_argument("--kernel", "-K", help="which built index to use (e.g. 6.12.104)",
                   **kw)
    g.add_argument("--db", help="path to a specific index file", **kw)
    g.add_argument("--color", choices=("auto", "always", "never"),
                   **(kw or {"default": "auto"}))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Index a Linux kernel tree and explore its structure, "
                    "symbols and subsystems.",
        epilog=f"Start with:  {PROG} build lts     then:  {PROG} info net/ipv4",
    )
    _global_opts(p, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _global_opts(common, suppress=True)

    subs = p.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return subs.add_parser(name, parents=[common], **kwargs)

    sp = add("versions", help="list kernel versions available on kernel.org")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_versions)

    sp = add("build", help="download a kernel and build its index")
    # No hardcoded default: with --src the version comes from the tree's own
    # Makefile, otherwise the alias 'lts' is applied in cmd_build.
    sp.add_argument("version", nargs="?", default=None,
                    help="version or alias: lts (default), stable, mainline, 6.12.104")
    sp.add_argument("--src", help="index an existing local kernel tree instead")
    sp.add_argument("--output", "-o", help="write the index here")
    sp.add_argument("--jobs", "-j", type=int, help="parallel parser processes")
    sp.add_argument("--kinds", help="symbol kinds to index (default: "
                                    + ",".join(cparse.DEFAULT_KINDS) + ")")
    sp.add_argument("--with-calls", action="store_true",
                    help="also record a call graph (bigger index, enables 'calls')")
    sp.add_argument("--keep-tarball", action="store_true")
    sp.add_argument("--no-verify", action="store_true",
                    help="skip the sha256 check against kernel.org")
    sp.add_argument("--force", action="store_true", help="rebuild if it already exists")
    sp.add_argument("--quiet", "-q", action="store_true")
    sp.set_defaults(func=cmd_build)

    sp = add("indexes", help="list indexes you have built")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_indexes)

    sp = add("use", help="pin which kernel version commands use by default")
    sp.add_argument("version", nargs="?",
                    help="version or unique prefix; omit to show the current one")
    sp.add_argument("--clear", action="store_true",
                    help="unpin; go back to the highest built version")
    sp.set_defaults(func=cmd_use)

    sp = add("remove", aliases=["rm"], help="delete built indexes")
    sp.add_argument("versions", nargs="+", metavar="VERSION",
                    help="one or more versions (or unique prefixes) to delete")
    sp.add_argument("--source", action="store_true",
                    help="also delete the kernel source tree under kernels/")
    sp.set_defaults(func=cmd_remove)

    sp = add("stats", help="overview of an index")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_stats)

    sp = add("info", help="explain one folder, file or symbol")
    sp.add_argument("target", help="net/ipv4 | net/ipv4/tcp.c | tcp_sendmsg | "
                                   "net/ipv4/tcp.c:tcp_sendmsg | net/ipv4/tcp.c:120")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.add_argument("--max-subsystems", type=int, default=3)
    sp.add_argument("--max-candidates", type=int, default=10)
    sp.set_defaults(func=cmd_info)

    sp = add("siblings", aliases=["sib"],
                        help="what sits at the same level as this?")
    sp.add_argument("target")
    sp.add_argument("--level", "-l", default="auto", choices=query.LEVELS,
                    help="how wide to look (default: auto — the containing "
                         "directory, or the containing file for a symbol)")
    sp.add_argument("--include-self", action="store_true")
    _add_filter_opts(sp)
    _add_output_opts(sp)
    sp.set_defaults(func=cmd_siblings)

    sp = add("ls", help="list what is inside a folder or file")
    sp.add_argument("target", nargs="?", default="")
    _add_filter_opts(sp)
    _add_output_opts(sp)
    sp.set_defaults(func=cmd_ls)

    sp = add("find", help="search for a symbol by name")
    sp.add_argument("pattern")
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--glob", action="store_true", help="pattern is a glob (tcp_*)")
    sp.add_argument("--prefix", action="store_true")
    _add_filter_opts(sp)
    _add_output_opts(sp, sorts=False)
    sp.set_defaults(func=cmd_find, sort="name")

    sp = add("subsystems", help="list subsystems from MAINTAINERS")
    sp.add_argument("--grep", "-g")
    sp.add_argument("--sort", default="size", choices=("size", "name"))
    sp.add_argument("--limit", "-n", type=int, default=0)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystems)

    sp = add("subsystem", help="detail for one subsystem")
    sp.add_argument("name")
    sp.add_argument("--files", action="store_true", help="also list every file")
    sp.add_argument("--limit", "-n", type=int, default=15)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystem)

    sp = add("path", help="print the on-disk path of a folder, file or symbol")
    sp.add_argument("target")
    sp.add_argument("--line", action="store_true",
                    help="append :LINE for symbols")
    sp.set_defaults(func=cmd_path)

    sp = add("show", help="print the source of a symbol or file")
    sp.add_argument("target")
    sp.add_argument("--context", "-C", type=int, default=0,
                    help="extra lines around a symbol")
    sp.add_argument("--lines", "-L", help="line range for a file, e.g. 100:140")
    sp.add_argument("--bare", action="store_true",
                    help="no header and no line numbers")
    sp.set_defaults(func=cmd_show)

    sp = add("tree", help="draw the directory tree")
    sp.add_argument("target", nargs="?", default="")
    sp.add_argument("--depth", "-d", type=int, default=2)
    sp.add_argument("--files", action="store_true", help="include files")
    sp.add_argument("--format", "-f", default="tree", choices=("tree", "json"))
    sp.set_defaults(func=cmd_tree)

    sp = add("trace",
                        help="annotate a backtrace: which subsystem is each frame in?")
    sp.add_argument("frames", nargs="*",
                    help="frame names, or pipe an oops/ftrace log on stdin")
    sp.add_argument("--limit", "-n", type=int, default=100)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_trace)

    sp = add("calls", help="call graph (needs an index built --with-calls)")
    sp.add_argument("target")
    sp.add_argument("--callers", action="store_true",
                    help="show callers instead of callees")
    _add_output_opts(sp, sorts=False)
    sp.set_defaults(func=cmd_calls, sort="name", kinds=None, exported=False,
                    static_only=False, no_static=False)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
