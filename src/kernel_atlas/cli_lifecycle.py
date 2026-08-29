"""CLI handlers for kernel source and index lifecycle operations.

The public command functions remain in :mod:`kernel_atlas.cli`.  They pass that
module as ``support`` so command code can reuse its stable selection, error, and
path-safety helpers without introducing an import cycle.
"""

from __future__ import annotations

import shlex
import shutil
import sqlite3
import sys
from pathlib import Path

from . import config, cparse, db, indexer, kernelsrc, maintainers, render


def cmd_versions(args, support):
    try:
        releases = kernelsrc.list_releases()
    except (OSError, ValueError) as exc:
        support._die(f"could not reach kernel.org ({exc})")
    color = render.use_color(args.color)
    if args.format == "json":
        sys.stdout.write(render.render_json([r.__dict__ for r in releases]))
        return
    print(render.paint("Current kernel.org releases", "1", color))
    print(f"  {'MONIKER':<12} {'VERSION':<16} {'RELEASED':<12}")
    for release in releases:
        note = ("  <- good default for learning"
                if release.moniker == "longterm" else "")
        print(f"  {release.moniker:<12} {release.version:<16} "
              f"{release.released or '-':<12}"
              + render.paint(note, "32", color))
    print(f"\nBuild one with:  {support.PROG} "
          "build <version|lts|stable|mainline>")


def cmd_build(args, support):
    quiet = args.quiet
    kinds = support._split_list(args.kinds) or list(cparse.DEFAULT_KINDS)
    bad = [kind for kind in kinds if kind not in cparse.ALL_KINDS]
    if bad:
        support._die(f"unknown symbol kind(s): {', '.join(bad)} "
                     f"(valid: {', '.join(cparse.ALL_KINDS)})")
    if args.with_calls and not ({"function", "syscall"} & set(kinds)):
        support._die(
            "--with-calls requires indexing function and/or syscall symbols")
    missing_call_kinds = {"macro", "variable"} - set(kinds)
    if args.with_calls and missing_call_kinds:
        support._die(
            "--with-calls requires macro and variable symbols so indirect or "
            "macro calls are not falsely linked to unrelated functions")

    if args.src:
        if args.keep_tarball or args.no_verify:
            support._die(
                "--keep-tarball and --no-verify only apply to downloaded source")
        tree = Path(args.src).expanduser().resolve()
        if not (tree / "MAINTAINERS").is_file():
            support._die(
                f"{tree} does not look like a kernel tree (no MAINTAINERS file)")
        if args.version and args.version.lower() in {
                "lts", "longterm", "stable", "mainline", "latest"}:
            support._die(
                f"version alias {args.version!r} does not apply with --src; "
                "omit it to read the tree's Makefile")
        version = args.version or kernelsrc.detect_version(tree)
        if version is None:
            support._die(
                f"could not detect a kernel version from {tree / 'Makefile'}; "
                "pass an explicit version before --src")
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            support._die(str(exc))
        source = str(tree)
    else:
        spec = args.version or "lts"
        try:
            release = kernelsrc.resolve_version(spec)
        except (OSError, LookupError, ValueError) as exc:
            support._die(str(exc))
        version = release.version
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            support._die(str(exc))
        if not quiet:
            print(f"kernel {version} ({release.moniker})", file=sys.stderr)

    out = (Path(args.output).expanduser()
           if args.output else config.index_path(version))
    if out.is_dir():
        support._die(f"index output {out} is a directory")
    if out.exists() and not args.force:
        support._die(f"index already exists at {out} (use --force to rebuild)")
    if not args.src and support._path_inside(out, config.source_path(version)):
        support._die(f"index output {out} is inside the source tree "
                     f"{config.source_path(version)}; choose a path outside "
                     "the tree")

    if not args.src:
        source = release.source or kernelsrc.tarball_url(version)
        try:
            tree = kernelsrc.ensure_source(
                version, keep_tarball=args.keep_tarball, quiet=quiet,
                verify=not args.no_verify, source_url=source)
        except (OSError, RuntimeError) as exc:
            support._die(f"could not obtain kernel source: {exc}")

    if support._path_inside(out, tree):
        support._die(f"index output {out} is inside the source tree {tree}; "
                     "choose a path outside the tree")

    if not quiet:
        print(f"indexing {tree}", file=sys.stderr)
    try:
        stats = indexer.build(
            tree, out, version, kinds=kinds, want_calls=args.with_calls,
            jobs=args.jobs, quiet=quiet, source=source)
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        support._die(f"could not build index: {exc}")
    size_mb = out.stat().st_size / (1024 * 1024)
    try:
        selectable_by_kernel = (
            out.resolve() == config.index_path(version).resolve())
    except OSError:
        selectable_by_kernel = False
    if selectable_by_kernel:
        query_cmd = f"{support.PROG} -K {shlex.quote(out.stem)}"
    else:
        query_cmd = (
            f"{support.PROG} --db {shlex.quote(str(out.resolve()))}")
    print(
        f"\nBuilt index for Linux {version}\n"
        f"  {stats.dirs:,} directories, {stats.files:,} files\n"
        f"  {stats.symbols:,} symbols from {stats.parsed:,} C/H files\n"
        + (f"  {stats.skipped:,} parse inputs skipped, {stats.failed:,} failed"
           f" ({stats.oversize:,} oversized)\n"
           if stats.skipped or stats.failed else "")
        + (f"  {stats.symlinks:,} symlinks recorded\n"
           if stats.symlinks else "")
        + (f"  {stats.calls:,} call edges: {stats.calls_resolved:,} resolved, "
           f"{stats.calls_ambiguous:,} ambiguous, {stats.calls_macro:,} macro, "
           f"{stats.calls_indirect:,} indirect, "
           f"{stats.calls_unresolved:,} unresolved\n" if stats.calls else "")
        + f"  {stats.subsystems:,} subsystems from MAINTAINERS\n"
        f"  {out}  ({size_mb:.0f} MB, {stats.seconds:.0f}s)\n"
        f"\nTry:  {query_cmd} info mm\n"
        f"      {query_cmd} siblings mm/page_alloc.c"
    )


def cmd_indexes(args, support):
    paths = config.list_indexes()
    if not paths:
        print(f"no indexes yet — run '{support.PROG} build lts'")
        return
    active = support.default_index() if paths else None
    rows = []
    for path in paths:
        conn = None
        error = None
        try:
            conn = db.connect(path, readonly=True)
            meta = db.validate_schema(conn)
        except (sqlite3.DatabaseError, OSError) as exc:
            meta = {}
            error = str(exc)
        finally:
            if conn is not None:
                conn.close()
        source_here = support.find_source_tree({
            "index_stem": path.stem,
            "kernel_version": meta.get("kernel_version", path.stem),
            "tree_path": meta.get("tree_path"),
        }) is not None
        version = meta.get("kernel_version") or path.stem
        rows.append({
            "version": version,
            "alias": path.stem,
            "files": meta.get("n_files", "?"),
            "symbols": meta.get("n_symbols", "?"),
            "calls": meta.get("has_calls") == "1",
            "source": source_here,
            "built_at": meta.get("built_at", "?"),
            "size": f"{path.stat().st_size / 1048576:.0f} MB",
            "default": support._same_path(path, active),
            "path": str(path),
            "error": error,
        })
    rows.sort(
        key=lambda row: support._version_key(Path(f"{row['version']}.db")),
        reverse=True,
    )
    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return
    color = render.use_color(args.color)
    show_alias = any(row["alias"] != row["version"] for row in rows)
    alias_head = f" {'INDEX':<12}" if show_alias else ""
    print(f"    {'VERSION':<12}{alias_head} {'STATE':<7} {'FILES':>8} "
          f"{'SYMBOLS':>10} {'CALLS':<6} {'SOURCE':<7} {'BUILT':<20} "
          f"{'SIZE':>8}")
    for row in rows:
        mark = "*" if row["default"] else " "
        alias = f" {row['alias']:<12}" if show_alias else ""
        state = "broken" if row["error"] else "ok"
        line = (f"  {mark} {row['version']:<12}{alias} {state:<7} "
                f"{row['files']:>8} {row['symbols']:>10} "
                f"{'yes' if row['calls'] else '-':<6} "
                f"{'yes' if row['source'] else '-':<7} "
                f"{row['built_at']:<20} {row['size']:>8}")
        print(render.paint(line, "1", color) if row["default"] else line)
        if row["error"]:
            print(render.paint(
                f"      unusable: {row['error']} (rebuild this index)",
                "31", color))
    pinned = config.get_default_version()
    note = (f"pinned with '{support.PROG} use {pinned}'" if pinned
            else f"highest version (pin one with "
                 f"'{support.PROG} use <version>')")
    print(render.paint(f"\n  * = default index — {note}", "90", color))


def cmd_use(args, support):
    if args.clear and args.version:
        support._die("pass a version or --clear, not both")
    if args.clear:
        was = config.get_default_version()
        config.clear_default_version()
        if was:
            print(f"cleared pin on {was}; the highest built version is the "
                  "default again")
        else:
            print("nothing was pinned")
        return
    if not args.version:
        available = config.list_indexes()
        pinned = config.get_default_version()
        if not available:
            print("no indexes built yet — run "
                  f"'{support.PROG} build lts', then "
                  f"'{support.PROG} use <version>'")
            return
        if pinned:
            pin_path = config.index_path(pinned)
            if pin_path.is_file():
                print(f"pinned: {pinned}")
            else:
                print(f"pinned: {pinned}  (index is gone — "
                      f"'{support.PROG} use --clear' or "
                      f"'{support.PROG} use <version>')")
        else:
            print("nothing pinned; defaulting to the highest built version")
        active = support.default_index(warn=False)
        conn = None
        try:
            conn = db.connect(active, readonly=True)
            db.validate_schema(conn)
        except (OSError, sqlite3.DatabaseError) as exc:
            support._die(
                f"active index {active} is not usable ({exc}); rebuild it or "
                f"select another with '{support.PROG} use <version>'")
        finally:
            if conn is not None:
                conn.close()
        print(f"active index: {active.stem}  ({active})")
        return
    path = support.resolve_index_spec(args.version)
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        db.validate_schema(conn)
    except (OSError, sqlite3.DatabaseError) as exc:
        support._die(
            f"cannot use {path.stem!r}: {path} is not a usable index ({exc})")
    finally:
        if conn is not None:
            conn.close()
    config.set_default_version(path.stem)
    print(f"default index is now {path.stem}\n"
          "  every command without -K/--db will use it; "
          f"undo with '{support.PROG} use --clear'")


def cmd_remove(args, support):
    unique: list[Path] = []
    for spec in args.versions:
        path = support.resolve_index_spec(spec)
        if path not in unique:
            unique.append(path)

    managed_sources = {
        path: (support._managed_source_recorded_by(path)
               if args.source else None)
        for path in unique
    }

    freed = 0
    for path in unique:
        alias = path.stem
        size = support._unlink_index(path)
        freed += size
        print(f"removed index   {path}  ({size / 1048576:.0f} MB)")

        if config.get_default_version() == alias:
            config.clear_default_version()
            print("  (it was the pinned default; the pin has been cleared)")

        tree = managed_sources[path]
        if args.source:
            if tree is None:
                print("  source kept (the index does not identify a matching "
                      "managed source tree)")
            elif tree.is_symlink():
                try:
                    tree.unlink()
                except OSError as exc:
                    print(f"  could not remove source {tree}: {exc}",
                          file=sys.stderr)
                else:
                    print(f"removed source link  {tree}")
            elif tree.is_dir():
                try:
                    shutil.rmtree(tree)
                except OSError as exc:
                    print(f"  could not remove source {tree}: {exc}",
                          file=sys.stderr)
                else:
                    print(f"removed source  {tree}")
            else:
                print(f"no source tree at {tree}")
        else:
            try:
                tree = config.source_path(alias)
            except ValueError:
                tree = None
        if not args.source and tree is not None and tree.is_dir():
            print(f"  (source kept at {tree}; remove it too with --source)")
    print(f"\nfreed {freed / 1048576:.0f} MB of index files"
          + (" (source trees not counted)" if args.source else ""))


def cmd_stats(args, support):
    conn, meta = support.open_index(args)
    if args.format == "json":
        extra = {row["kind"]: row["n"] for row in conn.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind")}
        sys.stdout.write(render.render_json({
            "meta": meta, "symbols_by_kind": extra,
        }))
        return
    color = render.use_color(args.color)
    print(render.paint(f"{support._linux(meta)} index", "1", color))
    print(f"  built        {meta.get('built_at', '?')}")
    print(f"  source       {meta.get('source', '?')}")
    print(f"  directories  {int(meta.get('n_dirs', 0)):,}")
    print(f"  files        {int(meta.get('n_files', 0)):,}")
    print(f"  subsystems   {int(meta.get('n_subsystems', 0)):,}")
    print(f"  symbols      {int(meta.get('n_symbols', 0)):,}")
    if meta.get("has_calls") == "1":
        total_calls = int(meta.get("n_calls", 0))
        resolved_calls = int(meta.get("n_calls_resolved", 0))
        print(f"  call edges   {total_calls:,} "
              f"({resolved_calls:,} resolved identities)")
        print(f"  call gaps    {int(meta.get('n_calls_ambiguous', 0)):,} "
              f"ambiguous, {int(meta.get('n_calls_macro', 0)):,} macro-only, "
              f"{int(meta.get('n_calls_indirect', 0)):,} indirect, "
              f"{int(meta.get('n_calls_unresolved', 0)):,} unresolved")
    skipped = int(meta.get("n_parse_skipped", 0))
    failed = int(meta.get("n_parse_failed", 0))
    if skipped or failed:
        print(f"  parse gaps   {skipped:,} skipped, {failed:,} failed"
              f" ({int(meta.get('n_oversize', 0)):,} oversized)")
    if int(meta.get("n_symlinks", 0)):
        print(f"  symlinks     {int(meta['n_symlinks']):,}")
    for row in conn.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind"
            " ORDER BY n DESC"):
        print(f"      {row['kind']:<12} {row['n']:>9,}")
    print(render.paint("\n  largest top-level areas", "1", color))
    for row in conn.execute(
        "SELECT d.name, COUNT(f.id) n FROM dirs d JOIN files f"
        " ON substr(f.path, 1, length(d.path) + 1) = d.path || '/'"
        " WHERE d.depth = 1"
        " GROUP BY d.id ORDER BY n DESC LIMIT 8"
    ):
        area = maintainers.TOP_LEVEL_AREAS.get(row["name"])
        label = f"{area[0]}" if area else ""
        print(f"      {row['name']:<14} {row['n']:>7,} files   {label}")


def cmd_check(args, support):
    """Run the full row-level integrity audit on an index."""
    conn, meta = support.open_index(args)
    try:
        db.validate_schema(conn, deep=True)
    except (db.SchemaError, sqlite3.DatabaseError) as exc:
        support._die(f"index integrity check failed: {exc}")
    payload = {
        "ok": True,
        "index": support.index_version(meta),
        "files": int(meta["n_files"]),
        "symbols": int(meta["n_symbols"]),
        "calls": int(meta["n_calls"]),
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return
    print(f"{support._linux(meta)} index is structurally and semantically "
          "consistent")
    print(f"  {payload['files']:,} files, {payload['symbols']:,} symbols, "
          f"{payload['calls']:,} call edges checked")
