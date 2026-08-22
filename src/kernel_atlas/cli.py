"""Command line interface for kernel-atlas."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from . import config, cparse, db, indexer, kernelsrc, links, maintainers, query, render
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


def version_prefix_match(stem: str, spec: str) -> bool:
    """True if `spec` is `stem` or a prefix of it at a version-component boundary.

    ``6.18`` matches ``6.18.45``; ``6.1`` does not. ``next`` matches
    ``next-20260101``. String ``startswith`` would treat ``6.1`` as a prefix of
    ``6.18.45``, which is how you accidentally pin the wrong LTS.
    """
    if not spec or not stem:
        return False
    if stem == spec:
        return True
    return stem.startswith(spec + ".") or stem.startswith(spec + "-")


def _same_path(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def resolve_index_spec(spec: str) -> Path:
    """Turn a version or unique version prefix into an index path, or die."""
    path = config.index_path(spec)
    if path.is_file():
        return path
    matches = [p for p in config.list_indexes() if version_prefix_match(p.stem, spec)]
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


def selected_index(args) -> Path:
    """The index `-K` / `--db` / `use` would open, without connecting."""
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    if getattr(args, "kernel", None):
        return resolve_index_spec(args.kernel)
    return default_index()


def _looks_like_kernel_version(name: str) -> bool:
    if not name:
        return False
    if name.startswith("next-"):
        return True
    return _version_key(Path(name))[0] == 1


def index_version(meta: dict) -> str:
    """Version string that matches `ka use` / `-K` / the index filename.

    `--db scratch.db` keeps the version recorded in the index; a file named
    `6.18.45.db` is 6.18.45 even if `meta` was copied from another build.
    """
    stem = meta.get("index_stem") or ""
    kver = meta.get("kernel_version") or ""
    if _looks_like_kernel_version(stem):
        return stem
    return kver or stem or "?"


def open_index(args) -> tuple[sqlite3.Connection, dict]:
    path = selected_index(args)
    if not path.is_file():
        _die(f"no index at {path} — run '{PROG} build <version>' first")
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        meta = db.get_meta(conn)
    except (sqlite3.DatabaseError, OSError) as exc:
        if conn is not None:
            conn.close()
        _die(f"{path} is not a usable index ({exc}) — rebuild it with "
             f"'{PROG} build <version> --force'")
    if not meta.get("kernel_version"):
        conn.close()
        _die(f"{path} looks like an interrupted build — rebuild it with "
             f"'{PROG} build <version> --force'")
    meta["index_stem"] = path.stem
    _OPEN_INDEXES.append(conn)
    return conn, meta


_OPEN_INDEXES: list[sqlite3.Connection] = []


def _close_indexes() -> None:
    while _OPEN_INDEXES:
        try:
            _OPEN_INDEXES.pop().close()
        except sqlite3.Error:
            pass


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


def resolve_or_die(conn, spec: str) -> query.Resolution:
    res = query.resolve(conn, spec)
    if res.target is None:
        near = _suggestions(conn, spec)
        hint = "\n  did you mean: " + ", ".join(near) if near else ""
        _die(res.note + hint)
    return res


def _resolve_area(conn, spec: str) -> query.Resolution:
    """Prefer a directory named `spec` over a symbol that happens to share it.

    `bpf` is a variable in security/bpf/hooks.c *and* the directory kernel/bpf/.
    For commands about an area (docs), the directory is the useful answer.
    `kernel/bpf` is preferred over deeper homonyms like security/bpf/.
    """
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
                return (2, len(p), p)
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
         header: str = "", index: str | None = None):
    fmt = args.format
    machine = fmt in ("json", "csv", "names", "plain")
    color = render.use_color(args.color)
    cols = pick_columns(args, kinds_listed, with_subsystem)
    if not machine and header:
        print(render.paint(header, "1", color))
    if fmt == "json":
        rows = [render.entry_dict(e) for e in entries]
        if index:
            for row in rows:
                row["index"] = index
        sys.stdout.write(render.render_json(rows))
        return
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
        elif k in query.ALL_KINDS:
            out.append(k)
        elif k.endswith("s") and k[:-1] in query.ALL_KINDS:
            out.append(k[:-1])
        else:
            _die(f"unknown kind {k!r} (valid: {', '.join(query.ALL_KINDS)}, "
                 f"or all/symbols/paths/functions/types)")
    return tuple(dict.fromkeys(out))


def find_source_tree(meta: dict) -> Path | None:
    """Source tree for this index: the `ka use` name first, then recorded path."""
    recorded = meta.get("tree_path")
    tried: list[str] = []
    for version in (index_version(meta), meta.get("index_stem"),
                    meta.get("kernel_version")):
        if not version or version in tried:
            continue
        tried.append(version)
        tree = config.tree_for(version, None)
        if tree is not None:
            return tree
    return config.tree_for(meta.get("kernel_version") or "", recorded)


def source_tree(meta: dict) -> Path:
    tree = find_source_tree(meta)
    if tree is None:
        version = index_version(meta)
        _die(f"the source for Linux {version} is not on disk "
             f"(expected {config.source_path(version)})\n"
             f"  the index still answers queries; to get the source back run:\n"
             f"    {PROG} build {version} --force")
    return tree


def _linux(meta: dict) -> str:
    return f"Linux {index_version(meta)}"


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


def _nonneg_int(value: str) -> int:
    try:
        i = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, not {value!r}")
    if i < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return i


def _positive_int(value: str) -> int:
    i = _nonneg_int(value)
    if i < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return i


def _target_spec(t: query.Target) -> str:
    """A spec `ka` will accept again (`.` for the kernel root)."""
    if t.kind == "symbol":
        return t.display
    return t.path or "."


def _links_for(meta: dict, t: query.Target) -> dict[str, str]:
    return links.links(
        index_version(meta), t.path, t.line,
        is_dir=(t.kind == "dir"),
        ident=(t.name if t.kind == "symbol" else None))


# How many bytes of a file `show` will dump without --lines. Matches the
# indexer: anything bigger is almost certainly generated.
_MAX_SHOW = 2 * 1024 * 1024
_SLASH_COUNT = "(LENGTH(path) - LENGTH(REPLACE(path, '/', '')))"


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
        f"\nTry:  {PROG} info mm\n"
        f"      {PROG} siblings mm/page_alloc.c"
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
        source_here = find_source_tree({
            "index_stem": p.stem,
            "kernel_version": meta.get("kernel_version", p.stem),
            "tree_path": meta.get("tree_path"),
        }) is not None
        rows.append({
            "version": p.stem,
            "files": meta.get("n_files", "?"),
            "symbols": meta.get("n_symbols", "?"),
            "calls": meta.get("has_calls") == "1",
            "source": source_here,
            "built_at": meta.get("built_at", "?"),
            "size": f"{p.stat().st_size / 1048576:.0f} MB",
            "default": _same_path(p, active),
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
    print(render.paint(f"{_linux(meta)} index", "1", color))
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
        " ON substr(f.path, 1, length(d.path) + 1) = d.path || '/'"
        " WHERE d.depth = 1"
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

    subs = [s for s in query.all_subsystems(
        conn, "dir" if t.kind == "dir" else "file",
        t.id if t.kind == "dir" else (t.file_id or t.id))
        if s["name"] not in query.CATCH_ALL]
    area = query.describe_area(t.path)
    lnks = _links_for(meta, t)

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
            "links": lnks,
            "index": index_version(meta),
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
        if v is not None and v != "":
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
                "SELECT COUNT(*) n FROM files WHERE path LIKE ? ESCAPE '\\'",
                (query.like_under(t.path),)).fetchone()["n"]
            if total != row["n_files"]:
                field("subtree", f"{total:,} files in total")
        else:
            field("size", f"{row['size']:,} bytes, {row['lines']:,} lines")
            by_kind = conn.execute(
                "SELECT kind, COUNT(*) n FROM symbols WHERE file_id = ?"
                " GROUP BY kind ORDER BY n DESC", (t.id,)).fetchall()
            if by_kind:
                field("defines", ", ".join(f"{r['n']} {r['kind']}" for r in by_kind))

    tree = find_source_tree(meta)
    if tree is not None:
        field("on disk", str(tree / t.path if t.path else tree))
    field("index", _linux(meta))
    field("elixir", lnks.get("elixir"))
    if lnks.get("docs"):
        field("docs", lnks["docs"])
    if lnks.get("ident"):
        field("ident", lnks["ident"])

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
        print(render.paint(f"  {len(res.candidates)} other definition(s) "
                           f"of this name", "33", color))
        for c in res.candidates[:args.max_candidates]:
            print(f"    {c.display}  ({c.symbol_kind or c.kind})")
        if len(res.candidates) > args.max_candidates:
            print(f"    ... and {len(res.candidates) - args.max_candidates} more")

    print(f"\n  Next:  {PROG} siblings {_target_spec(t)}"
          f"\n         {PROG} web {_target_spec(t)}")


def cmd_siblings(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    scope = query.build_scope(conn, t, args.level)
    if scope.dir_sql is None and scope.file_sql is None and scope.sym_where is None:
        _die(f"cannot build a '{args.level}' scope for {t.display} ({scope.label})")

    kinds = kinds_from_args(args, t)
    if (args.level == "tree"
            and any(k in query.SYMBOL_KINDS for k in kinds)
            and not args.limit):
        _die("listing symbols across the whole tree needs -n N "
             "(there are millions; try -n 50, or 'find' for a name search)")
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
        label = area[0] if area else None
    header = (f"Siblings of {t.display}  [{_linux(meta)}]\n"
              f"  level: {scope.label}"
              + (f"   subsystem: {label}" if label else "")
              + f"   showing: {', '.join(kinds)}\n")
    emit(entries, args, set(kinds), args.with_subsystem, header,
         index=index_version(meta))


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
    emit(entries, args, set(kinds), args.with_subsystem,
         f"{scope.label}  [{_linux(meta)}]\n", index=index_version(meta))


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
    limit = args.limit  # default 50; 0 = all
    if limit and narrowing:
        fetch = limit * 20
    else:
        fetch = limit
    entries = query.search(conn, args.pattern, kinds=kinds, mode=mode,
                           limit=fetch,
                           exported_only=args.exported,
                           with_subsystem=False)
    filtered = _post_filter(entries, args)
    entries = filtered[:limit] if limit else filtered
    if args.format != "names":
        query.annotate_subsystems(conn, entries)
    cols = _split_list(args.columns) or ["kind", "name", "path", "line", "subsystem"]
    args.columns = ",".join(cols)
    emit(entries, args, {"function"}, True,
         f"Symbols matching {args.pattern!r} ({mode})  [{_linux(meta)}]\n",
         index=index_version(meta))


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
    print(render.paint(f"{len(rows)} subsystems  [{_linux(meta)}]", "1", color))
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
        payload = dict(name=s["name"], status=s["status"], n_files=s["n_files"],
                       web=s["web"], index=index_version(meta), **f)
        if args.files:
            payload["files"] = [r["path"] for r in conn.execute(
                "SELECT f.path FROM files f JOIN path_subsys p ON p.ref_kind='file'"
                " AND p.ref_id=f.id WHERE p.subsystem_id=? ORDER BY f.path",
                (s["id"],))]
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    print(render.paint(s["name"], "1;35", color))
    print(f"  index        {_linux(meta)}")
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
    n = args.limit if args.limit else 10**9
    for r in conn.execute(
        "SELECT d.path, d.n_files FROM dirs d JOIN path_subsys p"
        " ON p.ref_kind='dir' AND p.ref_id=d.id WHERE p.subsystem_id=?"
        " ORDER BY d.n_files DESC, d.path LIMIT ?", (s["id"], n)
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
                f" WHERE path LIKE ? ESCAPE '\\' AND {_SLASH_COUNT} <= ? ORDER BY path",
                (like, slash_max)).fetchall()
            entries += [Entry(kind="file", name=r["name"], path=r["path"],
                              size=r["size"], lines=r["lines"],
                              n_symbols=r["n_symbols"])
                        for r in frows]
    entries = [e for e in entries if e.path != base]
    if args.format == "json":
        payload = [render.entry_dict(e) for e in entries]
        ver = index_version(meta)
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
    print(render.paint(f"\n{len(entries)} entries (depth {max_depth})  [{_linux(meta)}]",
                       "90", color))


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
    try:
        with full.open("rb") as fh:
            head = fh.read(8192)
        if b"\0" in head:
            _die(f"{t.path} looks like a binary file")
    except OSError as exc:
        _die(f"cannot read {full}: {exc}")

    if t.kind == "symbol":
        start = max(1, (t.line or 1) - args.context)
        end: int | None = (t.end_line or t.line or 1) + args.context
    elif args.lines:
        m = re.fullmatch(r"(\d+)(?:[:-](\d+))?", args.lines)
        if not m:
            _die(f"--lines wants N or N:M, not {args.lines!r}")
        start = max(1, int(m.group(1)))
        end = int(m.group(2)) if m.group(2) else start
        if end < start:
            _die(f"--lines {args.lines!r}: end is before start")
    else:
        size = full.stat().st_size
        if size > _MAX_SHOW:
            _die(f"{t.path} is {size:,} bytes; pass --lines N:M or open it with "
                 f"$EDITOR $({PROG} path {t.path})")
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
              + render.paint(f"   [{_linux(meta)}]", "90", color))
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
        _die(f"cannot read {full}: {exc}")
    if printed == 0:
        _die(f"{t.path} has no line {start}")


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
    picked = frames if args.limit == 0 else frames[:args.limit]
    for name in picked:
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
                       f"({_linux(meta)})\n", "1", color))
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
        _die(f"this index ({index_version(meta)}) has no call graph — rebuild with "
             f"'{PROG} build {index_version(meta)} --with-calls'")
    res = resolve_or_die(conn, args.target)
    t = res.target
    if t.kind != "symbol":
        _die(f"{t.display} is not a symbol")

    narrowing = bool(args.grep or args.static_only or args.no_static)
    fetch = args.limit * 20 if args.limit and narrowing else args.limit

    if args.callers:
        entries = _post_filter(query.callers(conn, t.name, limit=fetch), args)
        if args.limit:
            entries = entries[:args.limit]
        query.annotate_subsystems(conn, entries)
        args.columns = args.columns or "kind,name,path,line,subsystem"
        emit(entries, args, {"function"}, True,
             f"Functions that call {t.name}  [{_linux(meta)}]\n",
             index=index_version(meta))
        return

    names = query.callees(conn, t.id, limit=fetch)
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
    if args.limit:
        entries = entries[:args.limit]
    query.annotate_subsystems(conn, entries)
    args.columns = args.columns or "kind,name,path,line,subsystem"
    emit(entries, args, {"function"}, True,
         f"Called by {t.display}  [{_linux(meta)}]\n",
         index=index_version(meta))


def cmd_web(args):
    """Print Elixir / git.kernel.org / GitHub / docs.kernel.org URLs."""
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target)
    t = res.target
    lnks = _links_for(meta, t)
    version = index_version(meta)

    if args.url:
        url = lnks.get(args.url)
        if not url:
            why = ""
            if args.url == "docs":
                why = " (not a Documentation/ file)"
            elif args.url == "ident":
                why = " (not a symbol)"
            _die(f"no {args.url} URL for {t.display}{why}")
        print(url)
        return

    if args.format == "json":
        sys.stdout.write(render.render_json({
            "target": t.display, "version": version, "links": lnks,
        }))
        return

    color = render.use_color(args.color)
    loc = t.path or "."
    if t.kind == "symbol" and t.line:
        loc = f"{t.path}:{t.line}"
    print(render.paint(f"{loc}  {t.name if t.kind == 'symbol' else ''}".rstrip(),
                       "1;36", color)
          + render.paint(f"   [Linux {version}]", "90", color))
    order = ("elixir", "ident", "git", "github", "docs")
    width = max(len(k) for k in order if k in lnks)
    for key in order:
        if key in lnks:
            print(f"  {key:<{width}}  {lnks[key]}")


def cmd_docs(args):
    conn, meta = open_index(args)
    res = _resolve_area(conn, args.target)
    t = res.target
    entries = query.documentation_for(conn, t, limit=args.limit)
    if not entries:
        _die(f"no Documentation/ files related to {t.display}")
    sub = query.subsystem_for_target(conn, t)
    label = sub["name"] if sub and sub["name"] not in query.CATCH_ALL else None
    version = index_version(meta)
    if args.format == "json":
        payload = []
        for e in entries:
            item = {"path": e.path, "name": e.name, "lines": e.lines, "size": e.size,
                    "index": version}
            item.update(links.links(version, e.path))
            payload.append(item)
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    head = f"Documentation related to {t.display}"
    if label:
        head += f"   [{label}]"
    head += f"   [{_linux(meta)}]"
    print(render.paint(head, "1", color))
    if res.note:
        print(render.paint(f"  ({res.note})", "33", color))
    for e in entries:
        print(f"  {e.path}")
    print(render.paint(f"\n{len(entries)} file{'s' if len(entries) != 1 else ''}"
                       f"   Next: {PROG} web {entries[0].path}", "90", color))


def cmd_locate(args):
    """Resolve a target in every built index, so you can see it move across versions."""
    if getattr(args, "db", None):
        db_path = Path(args.db).expanduser()
        if not db_path.is_file():
            _die(f"no index at {db_path}")
        available = [db_path]
        active = db_path
    else:
        available = config.list_indexes()
        if not available:
            _die(f"no index built yet — run '{PROG} build lts' first")
        active = selected_index(args)

    rest = [p for p in available if not _same_path(p, active)]
    rest.sort(key=_version_key, reverse=True)
    ordered = ([active] if active is not None and any(_same_path(p, active) for p in available)
               else []) + rest

    spec = args.target
    rows = []
    for path in ordered:
        conn = None
        is_active = _same_path(path, active)
        try:
            try:
                conn = db.connect(path, readonly=True)
                meta = db.get_meta(conn)
                version = path.stem
                if not meta.get("kernel_version"):
                    rows.append({"version": version, "found": False,
                                 "active": is_active, "error": "interrupted build"})
                    continue
                res = query.resolve(conn, spec)
            except sqlite3.Error as exc:
                rows.append({"version": path.stem, "found": False,
                             "active": is_active, "error": str(exc)})
                continue
            t = res.target
            if t is None:
                rows.append({"version": version, "found": False,
                             "active": is_active, "note": res.note})
            else:
                sub = query.subsystem_for_target(conn, t)
                label = (sub["name"] if sub and sub["name"] not in query.CATCH_ALL
                         else None)
                if not label:
                    area = query.describe_area(t.path)
                    label = area[0] if area else (sub["name"] if sub else None)
                rows.append({
                    "version": version, "found": True, "active": is_active,
                    "kind": t.symbol_kind or t.kind,
                    "name": t.name, "path": t.path or ".",
                    "line": t.line, "end_line": t.end_line,
                    "subsystem": label, "note": res.note or None,
                })
        finally:
            if conn is not None:
                conn.close()

    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return

    color = render.use_color(args.color)
    active_name = next((r["version"] for r in rows if r.get("active")), None)
    note = f"  * = {_linux({'index_stem': active_name})}" if active_name else ""
    print(render.paint(f"{spec}  across {len(rows)} index"
                       f"{'es' if len(rows) != 1 else ''}{note}\n", "1", color))
    wver = max((len(r["version"]) for r in rows), default=8)
    for r in rows:
        mark = "*" if r.get("active") else " "
        ver = r["version"].ljust(wver)
        prefix = f"  {mark} {ver}"
        if not r.get("found"):
            why = r.get("error") or "not in this index"
            print(f"{prefix}  {render.paint(why, '90', color)}")
            continue
        loc = r["path"]
        if r.get("line"):
            loc = f"{r['path']}:{r['line']}"
        sub = r.get("subsystem") or "-"
        print(f"{prefix}  {r['kind']:<10} "
              f"{loc:<42} {render.paint(sub, '35', color)}")


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def _add_output_opts(p, sorts=True, limit_default=0):
    g = p.add_argument_group("output")
    g.add_argument("--format", "-f", default="table",
                   choices=("table", "plain", "names", "json", "csv", "tree"),
                   help="output format (default: table)")
    g.add_argument("--columns", "-c",
                   help="comma-separated columns: " + ",".join(render.COLUMNS))
    g.add_argument("--limit", "-n", type=_nonneg_int, default=limit_default,
                   help="max rows (0 = all)" if limit_default == 0 else
                        f"max rows (default: {limit_default}; 0 = all)")
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
        epilog=f"Start with:  {PROG} build lts     then:  {PROG} info mm",
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
    sp.add_argument("--jobs", "-j", type=_positive_int, help="parallel parser processes")
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
    sp.add_argument("target", help="mm | mm/page_alloc.c | tcp_sendmsg | "
                                   "tcp.c:tcp_sendmsg | mm/page_alloc.c:5268")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.add_argument("--max-subsystems", type=_nonneg_int, default=3)
    sp.add_argument("--max-candidates", type=_nonneg_int, default=10)
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
    _add_output_opts(sp, sorts=False, limit_default=50)
    sp.set_defaults(func=cmd_find, sort="name")

    sp = add("subsystems", help="list subsystems from MAINTAINERS")
    sp.add_argument("--grep", "-g")
    sp.add_argument("--sort", default="size", choices=("size", "name"))
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=0)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystems)

    sp = add("subsystem", help="detail for one subsystem")
    sp.add_argument("name")
    sp.add_argument("--files", action="store_true", help="also list every file")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=15)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystem)

    sp = add("path", help="print the on-disk path of a folder, file or symbol")
    sp.add_argument("target")
    sp.add_argument("--line", action="store_true",
                    help="append :LINE for symbols")
    sp.set_defaults(func=cmd_path)

    sp = add("show", help="print the source of a symbol or file")
    sp.add_argument("target")
    sp.add_argument("--context", "-C", type=_nonneg_int, default=0,
                    help="extra lines around a symbol")
    sp.add_argument("--lines", "-L", help="line range for a file, e.g. 100:140")
    sp.add_argument("--bare", action="store_true",
                    help="no header and no line numbers")
    sp.set_defaults(func=cmd_show)

    sp = add("tree", help="draw the directory tree")
    sp.add_argument("target", nargs="?", default="")
    sp.add_argument("--depth", "-d", type=_nonneg_int, default=2)
    sp.add_argument("--files", action="store_true", help="include files")
    sp.add_argument("--format", "-f", default="tree", choices=("tree", "json"))
    sp.set_defaults(func=cmd_tree)

    sp = add("web", help="print Elixir / git.kernel.org / GitHub / docs URLs")
    sp.add_argument("target")
    sp.add_argument("--url", choices=("elixir", "ident", "git", "github", "docs"),
                    help="print just this URL, for `open $(ka web … --url elixir)`")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_web)

    sp = add("docs", help="Documentation/ files related to a target")
    sp.add_argument("target")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=30)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_docs)

    sp = add("locate",
             help="resolve a target in every built index (compare versions)")
    sp.add_argument("target")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_locate)

    sp = add("trace",
                        help="annotate a backtrace: which subsystem is each frame in?")
    sp.add_argument("frames", nargs="*",
                    help="frame names, or pipe an oops/ftrace log on stdin")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=100)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_trace)

    sp = add("calls", help="call graph (needs an index built --with-calls)")
    sp.add_argument("target")
    sp.add_argument("--callers", action="store_true",
                    help="show callers instead of callees")
    _add_output_opts(sp, sorts=False, limit_default=200)
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
    finally:
        _close_indexes()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
