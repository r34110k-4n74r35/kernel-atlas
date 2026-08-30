"""Command line interface for kernel-atlas."""

from __future__ import annotations

import argparse
import re
import shlex
import sqlite3
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import __version__
from . import (cli_aggregate, cli_browse, cli_calls, cli_lifecycle,
               cli_resources, config, cparse, db, links, query, render)
from . import indexer  # noqa: F401 - public monkeypatch seam for build tests
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
    """Sort by the leading numeric release, including vendor/local suffixes.

    For one numeric base, final releases sort above deterministic vendor/local
    suffixes, which sort above release candidates.  The suffixes themselves do
    not have a universal version scheme, but they must not make Linux 6.6 sort
    below every plain numeric version such as Linux 6.1.
    """
    stem = path.stem
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", stem)
    if not m:
        return (0, (), 0, 0, stem)
    numbers = tuple(int(p) for p in m.group(1).split("."))
    suffix = m.group(2)
    rc = re.fullmatch(r"-rc(\d+)", suffix)
    phase = 2 if not suffix else (0 if rc else 1)
    # For the same numeric release: final > local/vendor > rc10 > rc2.
    return (1, numbers, phase, int(rc.group(1)) if rc else 0, suffix, stem)


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


def _index_version_key(path: Path) -> tuple:
    """Sort usable indexes by recorded version, ahead of corrupt aliases."""
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        version = db.validate_schema(conn).get("kernel_version")
        version = config.validate_version(version)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return (0, _version_key(path))
    finally:
        if conn is not None:
            conn.close()
    return (1, _version_key(Path(f"{version}.db")))


def _same_path(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def resolve_index_spec(spec: str) -> Path:
    """Turn a version or unique version prefix into an index path, or die."""
    try:
        spec = config.validate_version(spec)
        path = config.index_path(spec)
    except ValueError as exc:
        _die(str(exc))
    if path.is_file() or path.is_symlink():
        return path
    matches = [p for p in config.list_indexes() if version_prefix_match(p.stem, spec)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _die(f"{spec!r} is ambiguous: " + ", ".join(p.stem for p in matches))
    have = ", ".join(p.stem for p in config.list_indexes()) or "none built yet"
    _die(f"no index for {spec!r} (built: {have})")


def _default_version_pin() -> str | None:
    """Read the configured pin with a concise CLI error on corruption."""
    try:
        return config.get_default_version()
    except (OSError, ValueError) as exc:
        _die(f"cannot read the default version pin: {exc}")


def default_index(*, warn: bool = True) -> Path:
    """The index used when neither --db nor -K is given.

    Precedence: the version pinned with `{PROG} use`, then the highest built
    version — which is predictable, unlike file modification times.
    """
    available = config.list_indexes()
    if not available:
        _die(f"no index built yet — run '{PROG} build lts' first")
    pinned = _default_version_pin()
    if pinned:
        path = config.index_path(pinned)
        if path.is_file():
            return path
        if warn:
            print(f"{PROG}: pinned version {pinned} has no index any more; "
                  f"falling back to the highest built version "
                  f"(fix with '{PROG} use <version>' or '{PROG} use --clear')",
                  file=sys.stderr)
    return max(available, key=_index_version_key)


def selected_index(args) -> Path:
    """The index `-K` / `--db` / `use` would open, without connecting."""
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    if getattr(args, "kernel", None):
        return resolve_index_spec(args.kernel)
    return default_index()


def index_version(meta: dict) -> str:
    """Kernel version recorded by the build.

    An index filename is only a selection alias.  In particular, a custom
    ``--output indexes/other-name.db`` must not generate links for a kernel
    version that was never indexed.
    """
    stem = meta.get("index_stem") or ""
    kver = meta.get("kernel_version") or ""
    return kver or stem or "?"


def open_index(args) -> tuple[sqlite3.Connection, dict]:
    path = selected_index(args)
    if not path.is_file():
        _die(f"no index at {path} — run '{PROG} build <version>' first")
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        meta = db.validate_schema(conn)
    except (sqlite3.DatabaseError, OSError) as exc:
        if conn is not None:
            conn.close()
        _die(f"{path} is not a usable index ({exc}) — rebuild it with "
             f"'{PROG} build <version> --force'")
    meta["index_stem"] = path.stem
    # Keep the resolved selection identity alongside the persisted metadata.
    # A filename may be a custom alias for another kernel version, so rebuild
    # hints must not derive the publication path from ``kernel_version``.
    meta["index_path"] = str(path.expanduser().resolve())
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


def pick_columns(args, kinds_listed: set[str], with_subsystem: bool) -> list[str]:
    if getattr(args, "columns", None) is not None:
        cols = _split_list(args.columns)
        if not cols:
            _die("--columns must name at least one column")
        bad = [c for c in cols if c not in render.COLUMNS]
        if bad:
            _die(f"unknown column(s): {', '.join(bad)}"
                 f" — valid: {', '.join(render.COLUMNS)}")
        if with_subsystem and "subsystem" not in cols:
            cols.append("subsystem")
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


_COLUMN_OUTPUT_FORMATS = {"table", "json", "csv"}


def _validate_listing_output(args) -> None:
    """Reject column controls when the selected format has a fixed shape."""
    if args.format in _COLUMN_OUTPUT_FORMATS:
        return
    if getattr(args, "columns", None) is not None:
        _die(f"--columns does not apply to --format {args.format}")
    if getattr(args, "with_subsystem", False):
        _die(f"--with-subsystem does not apply to --format {args.format}")


def _listing_has_columns(args) -> bool:
    """Whether the selected listing renderer can expose chosen columns."""
    return args.format in _COLUMN_OUTPUT_FORMATS


def emit(entries: list[Entry], args, kinds_listed: set[str], with_subsystem: bool,
         header: str = "", index: str | None = None,
         default_columns: tuple[str, ...] | None = None):
    _validate_listing_output(args)
    fmt = args.format
    machine = fmt in ("json", "csv", "names", "plain")
    color = render.use_color(args.color)
    explicit_columns = getattr(args, "columns", None) is not None
    cols = pick_columns(args, kinds_listed, with_subsystem)
    if not explicit_columns and default_columns:
        cols = list(default_columns)
    if not machine and header:
        print(render.paint(header, "1", color))
    if fmt == "json":
        rows = [render.entry_dict(e, cols if explicit_columns else None)
                for e in entries]
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


def _entry_is_target(e: Entry, t: query.Target) -> bool:
    if e.ref_id is not None:
        if t.kind == "symbol":
            return e.kind == t.symbol_kind and e.ref_id == t.id
        return e.kind == t.kind and e.ref_id == t.id
    return (e.path == t.path and e.name == t.name
            and (t.kind != "symbol"
                 or (e.kind == t.symbol_kind and e.line == t.line)))


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


def symbol_filter_kinds(args, kinds: tuple[str, ...]) -> tuple[str, ...]:
    """Make linkage filters explicit instead of silently ignoring path rows."""
    enabled = []
    if getattr(args, "exported", False):
        enabled.append("--exported")
    if getattr(args, "static_only", False):
        enabled.append("--static-only")
    if getattr(args, "no_static", False):
        enabled.append("--no-static")
    if not enabled:
        return kinds
    symbols = tuple(k for k in kinds if k in query.SYMBOL_KINDS)
    if not symbols:
        _die(f"{'/'.join(enabled)} only applies to symbols; choose a symbol kind")
    return symbols


def find_source_tree(meta: dict) -> Path | None:
    """Find the exact source tree recorded for an index when it still exists.

    An index filename chooses the index, not a different source snapshot.  Its
    recorded tree is therefore the only tree guaranteed to match symbol lines.
    """
    recorded = meta.get("tree_path")
    kernel_version = meta.get("kernel_version") or ""
    if recorded:
        recorded_path = Path(recorded).expanduser()
        if (recorded_path / "MAINTAINERS").is_file():
            return recorded_path
        # A recorded path identifies the exact tree that produced the index.
        # Substituting a managed tree merely because its version string matches
        # can pair stale/different source with these symbol line numbers.
        return None

    for version in (kernel_version,):
        if not version:
            continue
        try:
            tree = config.tree_for(version, None)
        except ValueError:
            continue
        if tree is not None:
            return tree
    return None


_TARGET_SUFFIX_RE = re.compile(r":(?:[+-]?\d+|[A-Za-z_][A-Za-z0-9_]*)\Z")


def _normalize_target_spec(meta: dict, spec: str) -> str:
    """Translate an absolute source path into the index's relative namespace.

    Absolute editor/compiler locations are useful inputs, but only the exact
    tree recorded by the selected index gives them a safe, unambiguous meaning.
    Preserve an optional ``:line`` or ``:symbol`` suffix after normalizing the
    filesystem portion.
    """
    raw = (spec or "").strip()
    if not raw:
        return raw

    path_text = raw
    suffix = ""
    suffix_match = _TARGET_SUFFIX_RE.search(raw)
    if suffix_match:
        possible_path = raw[:suffix_match.start()]
        if Path(possible_path).expanduser().is_absolute():
            path_text = possible_path
            suffix = suffix_match.group(0)

    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        return raw

    tree = find_source_tree(meta)
    if tree is None:
        _die("cannot use an absolute target because the index's recorded "
             "source tree is not available")
    try:
        root = tree.expanduser().resolve()
        # Resolve parent components for containment, but preserve the leaf.
        # The leaf may itself be an indexed symlink (for example
        # Documentation/Changes); following it would silently change the
        # requested index identity to its target.
        if candidate == tree.expanduser():
            normalized_candidate = root
        else:
            normalized_candidate = candidate.parent.resolve() / candidate.name
        relative = normalized_candidate.relative_to(root)
    except ValueError:
        _die(f"absolute target {path_text!r} is outside the recorded source "
             f"tree {tree}")
    except (OSError, RuntimeError) as exc:
        _die(f"cannot safely resolve absolute target {path_text!r}: {exc}")

    normalized = relative.as_posix()
    return (normalized or ".") + suffix


def source_tree(meta: dict) -> Path:
    tree = find_source_tree(meta)
    if tree is None:
        version = index_version(meta)
        try:
            expected = config.source_path(version)
        except ValueError:
            expected = config.sources_dir() / f"linux-{version!r}"
        _die(f"the source for Linux {version} is not on disk "
             f"(expected {expected})\n"
             "  the index still answers offline queries; restore its recorded "
             "tree or rebuild this index from the intended source snapshot")
    return tree


def source_member(tree: Path, indexed_path: str) -> Path:
    """Return an indexed path only when it stays inside its recorded tree.

    Normal indexes contain paths produced by ``os.scandir``, but ``--db`` also
    accepts hand-built and third-party SQLite files.  Treat those paths as
    untrusted: an absolute path, ``..`` component, Windows separator/drive, or
    symlink which resolves outside the source tree must never let ``show`` read
    an arbitrary file (or make ``path`` advertise one).
    """
    if not isinstance(indexed_path, str) or "\0" in indexed_path \
            or "\\" in indexed_path:
        _die(f"unsafe path in index: {indexed_path!r}")
    rel = PurePosixPath(indexed_path)
    if (rel.is_absolute() or PureWindowsPath(indexed_path).drive
            or any(part in (".", "..") for part in rel.parts)):
        _die(f"unsafe path in index: {indexed_path!r}")

    try:
        root = tree.expanduser().resolve()
        candidate = tree.joinpath(*rel.parts)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except ValueError:
        _die(f"indexed path {indexed_path!r} escapes the recorded source tree")
    except (OSError, RuntimeError) as exc:
        _die(f"cannot safely resolve indexed path {indexed_path!r}: {exc}")
    return candidate


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


_MAX_CLI_COUNT = 2**31 - 1
_MAX_JOBS = 256


def _nonempty_arg(value: str) -> str:
    """Reject empty option values before truthiness can turn them into defaults."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _nonneg_int(value: str) -> int:
    try:
        i = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, not {value!r}")
    if i < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    if i > _MAX_CLI_COUNT:
        raise argparse.ArgumentTypeError(f"must be <= {_MAX_CLI_COUNT}")
    return i


def _positive_int(value: str) -> int:
    i = _nonneg_int(value)
    if i < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return i


def _jobs_int(value: str) -> int:
    i = _positive_int(value)
    if i > _MAX_JOBS:
        raise argparse.ArgumentTypeError(f"must be <= {_MAX_JOBS}")
    return i


def _path_inside(path: Path, directory: Path) -> bool:
    """Whether publishing ``path`` creates an entry inside ``directory``.

    Resolve the parent but deliberately not the leaf.  ``os.replace`` replaces
    a leaf symlink itself; following that symlink here would misidentify where
    the SQLite scratch file and final directory entry are actually created.
    """
    try:
        path = path.expanduser()
        publication = path.parent.resolve() / path.name
        publication.relative_to(directory.expanduser().resolve())
    except ValueError:
        return False
    except OSError as exc:
        _die(f"cannot resolve output/source paths: {exc}")
    return True


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


# How many bytes of a file `show` will dump without --lines.  This interactive
# display guard is intentionally stricter than the parser's 4 MiB input cap.
_MAX_SHOW = 2 * 1024 * 1024
_SLASH_COUNT = "(LENGTH(path) - LENGTH(REPLACE(path, '/', '')))"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_versions(args):
    return cli_lifecycle.cmd_versions(args, sys.modules[__name__])


def cmd_build(args):
    return cli_lifecycle.cmd_build(args, sys.modules[__name__])


def cmd_indexes(args):
    return cli_lifecycle.cmd_indexes(args, sys.modules[__name__])


def cmd_use(args):
    return cli_lifecycle.cmd_use(args, sys.modules[__name__])


def _unlink_index(path: Path) -> int:
    """Delete one regular index and its sidecars, or an index symlink leaf."""

    def inspect(leaf: Path):
        try:
            return leaf.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None

    primary = inspect(path)
    if primary is None:
        raise FileNotFoundError(path)
    if stat.S_ISLNK(primary.st_mode):
        # SQLite sidecars belong beside the symlink target, not beside the
        # alias.  Removing an alias must never guess ownership of adjacent data.
        path.unlink()
        return 0
    if not stat.S_ISREG(primary.st_mode):
        raise OSError(f"refusing to remove non-regular index entry {path}")

    leaves = (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
              path.with_suffix(".db-journal"))
    observed = []
    for leaf in leaves:
        info = inspect(leaf)
        if info is None:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise OSError(f"refusing non-regular SQLite sidecar {leaf}")
        observed.append((leaf, info))

    freed = 0
    for leaf, expected in observed:
        current = inspect(leaf)
        if (current is None
                or (current.st_dev, current.st_ino, current.st_mode)
                != (expected.st_dev, expected.st_ino, expected.st_mode)):
            raise OSError(f"index entry changed while being removed: {leaf}")
        if stat.S_ISREG(current.st_mode):
            freed += current.st_size
        leaf.unlink()
    return freed


def _managed_source_record(path: Path) -> tuple[Path, dict[str, str]] | None:
    """The safely removable managed source identified by an index.

    Selection aliases need not equal the indexed kernel version, and a custom
    ``--src`` tree must never be recursively deleted by ``remove --source``.
    Only return the conventional managed path when the index recorded that
    exact path for its validated metadata version.
    """
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        meta = db.validate_schema(conn)
        version = config.validate_version(meta.get("kernel_version", ""))
        recorded = meta.get("tree_path")
        if not isinstance(recorded, str) or not recorded:
            return None
        recorded_path = Path(recorded).expanduser()
        identity_keys = (
            "managed_tree_id", "managed_tree_device", "managed_tree_inode",
            "managed_tree_digest",
        )
        identity = {key: meta.get(key) for key in identity_keys}
        if any(not isinstance(value, str) or not value
               for value in identity.values()):
            # Custom --src indexes never receive this acquisition nonce, even
            # when their tree happens to use the conventional cache spelling.
            return None
        expected = config.source_path(version)
        return ((expected, identity)
                if _same_path(recorded_path, expected) else None)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _managed_source_recorded_by(path: Path) -> Path | None:
    """Compatibility path-only view of a managed source authorization."""
    record = _managed_source_record(path)
    return record[0] if record is not None else None


def cmd_remove(args):
    return cli_lifecycle.cmd_remove(args, sys.modules[__name__])


def cmd_stats(args):
    return cli_lifecycle.cmd_stats(args, sys.modules[__name__])


def cmd_check(args):
    return cli_lifecycle.cmd_check(args, sys.modules[__name__])


def cmd_info(args):
    return cli_browse.cmd_info(args, sys.modules[__name__])


def cmd_struct(args):
    return cli_aggregate.cmd_struct(args, sys.modules[__name__])


def cmd_siblings(args):
    return cli_browse.cmd_siblings(args, sys.modules[__name__])


def cmd_ls(args):
    return cli_browse.cmd_ls(args, sys.modules[__name__])


def _post_filter(entries, args):
    """Apply --grep and the static filters to already-fetched entries."""
    pattern = _checked_grep(getattr(args, "grep", None))
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        entries = [e for e in entries if rx.search(e.name)]
    if getattr(args, "static_only", False):
        entries = [e for e in entries if e.is_static is True]
    elif getattr(args, "no_static", False):
        entries = [e for e in entries if e.is_static is not True]
    if getattr(args, "exported", False):
        entries = [e for e in entries if e.is_exported]
    if _split_list(getattr(args, "kinds", None)):
        allowed = set(k for k in kinds_from_args(args, None)
                      if k in query.SYMBOL_KINDS)
        entries = [e for e in entries if e.kind in allowed]
    return entries


def _reject_symbol_size_sort(args, kinds) -> None:
    if (args.sort == "size" and kinds
            and all(kind in query.SYMBOL_KINDS for kind in kinds)):
        _die("--sort size does not apply to symbols; use --sort lines for "
             "definition span")


def cmd_find(args):
    return cli_browse.cmd_find(args, sys.modules[__name__])


def cmd_subsystems(args):
    return cli_browse.cmd_subsystems(args, sys.modules[__name__])


def cmd_subsystem(args):
    return cli_browse.cmd_subsystem(args, sys.modules[__name__])


def cmd_tree(args):
    return cli_browse.cmd_tree(args, sys.modules[__name__])


def cmd_path(args):
    return cli_browse.cmd_path(args, sys.modules[__name__])


def cmd_show(args):
    return cli_browse.cmd_show(args, sys.modules[__name__])


_FRAME_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_0-9]+)*\s*\+\s*0x")
_CLONE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_0-9]+)+")
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _frames_from_text(text: str) -> list[str]:
    """Pull symbol names out of an oops / ftrace / gdb style backtrace."""
    frames: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Kernel oops section delimiters are metadata, not frames.  Once bare
        # uppercase identifiers are accepted, <TASK>/</TASK> must be excluded
        # explicitly rather than accidentally becoming symbol names.
        if re.fullmatch(r"</?[A-Za-z_][A-Za-z0-9_]*>", line):
            continue
        m = _FRAME_RE.findall(line)
        if m:
            frames.extend(m)
            continue
        clone = _CLONE_RE.fullmatch(line)
        if clone:
            frames.append(clone.group(1))
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
    return frames


def cmd_trace(args):
    return cli_calls.cmd_trace(args, sys.modules[__name__])


def cmd_calls(args):
    _validate_listing_output(args)
    return cli_calls.cmd_calls(args, sys.modules[__name__])


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


def cmd_relationships(args):
    return cli_calls.cmd_relationships(args, sys.modules[__name__])


def cmd_web(args):
    return cli_resources.cmd_web(args, sys.modules[__name__])


def cmd_docs(args):
    return cli_resources.cmd_docs(args, sys.modules[__name__])


def cmd_locate(args):
    return cli_resources.cmd_locate(args, sys.modules[__name__])


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
                       choices=("name", "path", "kind", "line", "size", "lines"),
                       help="sort key; size applies to path rows, while lines "
                            "is the definition span for symbols")
    g.add_argument("--with-subsystem", "-S", action="store_true",
                   help="add a subsystem column")


def _add_filter_opts(p, *, kinds_help: str | None = None):
    g = p.add_argument_group("filters")
    g.add_argument("--kinds", "-k",
                   help=kinds_help or
                   "what to list: dir,file,function,syscall,struct,union,enum,"
                   "typedef,macro,variable,prototype — or all/symbols/paths/"
                   "functions/types")
    g.add_argument("--exported", action="store_true",
                   help="only EXPORT_SYMBOL'd symbols")
    linkage = g.add_mutually_exclusive_group()
    linkage.add_argument("--static-only", action="store_true",
                         help="only static symbols")
    linkage.add_argument("--no-static", action="store_true",
                         help="hide static symbols")


def _global_opts(parser, suppress: bool):
    """Accept --kernel/--db/--color before *or* after the subcommand.

    Subcommand copies use SUPPRESS so they only override when actually given,
    instead of clobbering the top-level value with their own default.
    """
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    g = parser.add_argument_group("index selection")
    g.add_argument("--kernel", "-K", type=_nonempty_arg,
                   help="which built index to use (e.g. 6.12.104)", **kw)
    g.add_argument("--db", type=_nonempty_arg,
                   help="path to a specific index file", **kw)
    g.add_argument("--color", choices=("auto", "always", "never"),
                   **(kw or {"default": "auto"}))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Index a Linux kernel tree and explore its structure, "
                    "symbols and subsystems.",
        epilog=f"Start with:  {PROG} build lts     then:  {PROG} info mm",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    _global_opts(p, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _global_opts(common, suppress=True)
    lifecycle_common = argparse.ArgumentParser(add_help=False)
    lifecycle_display = lifecycle_common.add_argument_group("display")
    lifecycle_display.add_argument(
        "--color", choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
    )

    subs = p.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return subs.add_parser(name, parents=[common], **kwargs)

    def add_lifecycle(name, **kwargs):
        # Lifecycle commands do not select an index to query.  Keep --color
        # usable after the subcommand without advertising meaningless -K/--db
        # options in their help.
        return subs.add_parser(name, parents=[lifecycle_common], **kwargs)

    sp = add_lifecycle(
        "versions", help="list kernel versions available on kernel.org")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_versions)

    sp = add_lifecycle("build", help="download a kernel and build its index")
    # No hardcoded default: with --src the version comes from the tree's own
    # Makefile, otherwise the alias 'lts' is applied in cmd_build.
    sp.add_argument("version", nargs="?", default=None, type=_nonempty_arg,
                    help="version or alias: lts (default), stable, mainline, 6.12.104")
    sp.add_argument("--src", type=_nonempty_arg,
                    help="index an existing local kernel tree instead")
    sp.add_argument("--output", "-o", type=_nonempty_arg,
                    help="write the index here")
    sp.add_argument("--jobs", "-j", type=_jobs_int, help="parallel parser processes")
    sp.add_argument("--kinds", type=_nonempty_arg,
                    help="symbol kinds to index (default: "
                         + ",".join(cparse.DEFAULT_KINDS) + ")")
    sp.add_argument("--with-calls", action="store_true",
                    help="also record a call graph (bigger index, enables 'calls')")
    sp.add_argument("--keep-tarball", action="store_true",
                    help="keep a downloaded source archive after extraction")
    sp.add_argument("--no-verify", action="store_true",
                    help="skip the sha256 check against kernel.org")
    sp.add_argument("--force", action="store_true", help="rebuild if it already exists")
    sp.add_argument("--quiet", "-q", action="store_true",
                    help="suppress download and indexing progress")
    sp.set_defaults(func=cmd_build)

    sp = add_lifecycle("indexes", help="list indexes you have built")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_indexes)

    sp = add_lifecycle(
        "use", help="pin which kernel version commands use by default")
    sp.add_argument("version", nargs="?",
                    help="version or unique prefix; omit to show the current one")
    sp.add_argument("--clear", action="store_true",
                    help="unpin; go back to the highest built version")
    sp.set_defaults(func=cmd_use)

    sp = add_lifecycle(
        "remove", aliases=["rm"], help="delete built indexes")
    sp.add_argument("versions", nargs="+", metavar="VERSION",
                    help="one or more versions (or unique prefixes) to delete")
    sp.add_argument("--source", action="store_true",
                    help="also delete the kernel source tree under kernels/")
    sp.set_defaults(func=cmd_remove)

    sp = add("stats", help="overview of an index")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_stats)

    sp = add("check", aliases=["doctor"],
             help="deep-check index counts and call identities")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_check)

    sp = add("info", help="explain one folder, file or symbol")
    sp.add_argument("target", help="mm | mm/page_alloc.c | tcp_sendmsg | "
                                   "tcp.c:tcp_sendmsg | mm/page_alloc.c:5268")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.add_argument("--max-subsystems", type=_nonneg_int, default=3,
                    help="maximum ownership matches to show (default: 3)")
    sp.add_argument("--max-candidates", type=_nonneg_int, default=10,
                    help="maximum ambiguous target candidates (default: 10)")
    sp.set_defaults(func=cmd_info)

    sp = add("struct", aliases=["structure"],
             help="explain a C struct/union and all indexed members")
    sp.add_argument("target", help="usb_device | struct usb_device | "
                                   "union perf_mem_data_src | "
                                   "include/linux/usb.h:usb_device | "
                                   "include/linux/usb.h:661")
    sp.add_argument("--all", action="store_true",
                    help="show every matching definition instead of requiring one")
    sp.add_argument("--max-docs", type=_nonneg_int, default=5,
                    help="maximum related Documentation/ files (0 disables)")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_struct)

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
    match = sp.add_mutually_exclusive_group()
    match.add_argument("--exact", action="store_true",
                       help="match the complete, case-sensitive name")
    match.add_argument("--glob", action="store_true",
                       help="pattern is a glob (tcp_*)")
    match.add_argument("--prefix", action="store_true",
                       help="match a case-insensitive name prefix")
    _add_filter_opts(
        sp,
        kinds_help="symbol kinds to search (path kinds are not accepted)",
    )
    _add_output_opts(sp, limit_default=50)
    sp.set_defaults(func=cmd_find)

    sp = add("subsystems", help="list subsystems from MAINTAINERS")
    sp.add_argument("--grep", "-g", help="only names matching this regex")
    sp.add_argument("--sort", default="size",
                    choices=("size", "claimed", "primary", "name"),
                    help="sort key (default: size)")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=0,
                    help="max subsystems (default: all)")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystems)

    sp = add("subsystem", help="detail for one subsystem")
    sp.add_argument("name")
    sp.add_argument("--files", action="store_true", help="also list every file")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=15,
                    help="max directory rows (default: 15; 0 = all); does not "
                         "limit the --files list")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_subsystem)

    sp = add("path", help="print the on-disk path of a folder, file or symbol")
    sp.add_argument("target")
    sp.add_argument("--line", action="store_true",
                    help="append :LINE for symbols")
    sp.set_defaults(func=cmd_path)

    sp = add("show", help="print the source of a symbol or file")
    sp.add_argument("target")
    show_range = sp.add_mutually_exclusive_group()
    show_range.add_argument("--context", "-C", type=_nonneg_int, default=0,
                            help="extra lines around a symbol")
    show_range.add_argument("--lines", "-L",
                            help="line range for a file, e.g. 100:140")
    sp.add_argument("--bare", action="store_true",
                    help="no header and no line numbers")
    sp.set_defaults(func=cmd_show)

    sp = add("tree", help="draw the directory tree")
    sp.add_argument("target", nargs="?", default="")
    sp.add_argument("--depth", "-d", type=_nonneg_int, default=2,
                    help="maximum directory depth (default: 2; 0 = target only)")
    sp.add_argument("--files", action="store_true", help="include files")
    sp.add_argument("--format", "-f", default="tree", choices=("tree", "json"))
    sp.set_defaults(func=cmd_tree)

    sp = add("web", help="print Elixir / git.kernel.org / GitHub / docs URLs")
    sp.add_argument("target")
    sp.add_argument("--url", choices=("elixir", "ident", "git", "github", "docs"),
                    help='print just this URL, for `open "$(ka web … '
                         '--url elixir)"`')
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
    _add_filter_opts(
        sp,
        kinds_help="resolved result identities to keep: function,syscall",
    )
    _add_output_opts(sp, limit_default=200)
    sp.set_defaults(func=cmd_calls)

    sp = add(
        "relationships", aliases=["rels"],
        help="ownership overlap and call flow between subsystems")
    sp.add_argument(
        "target",
        help="a subsystem name or any folder, file, or symbol in that subsystem")
    sp.add_argument("--via", choices=("all", "ownership", "calls"), default="all",
                    help="relationship evidence to show (default: all)")
    sp.add_argument("--direction", choices=("both", "outgoing", "incoming"),
                    default="both", help="call-flow direction (default: both)")
    sp.add_argument("--include-internal", action="store_true",
                    help="include calls which stay inside the selected subsystem")
    sp.add_argument("--min-shared", type=_positive_int, default=1,
                    help="minimum shared files for an ownership row")
    sp.add_argument("--min-calls", type=_positive_int, default=1,
                    help="minimum resolved edges for a call-flow row")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=20,
                    help="max rows per ownership/direction group "
                         "(default: 20; 0 = all)")
    sp.add_argument("--format", "-f", default="table",
                    choices=("table", "json", "csv"))
    sp.set_defaults(func=cmd_relationships)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_arg = getattr(args, "db", None)
    kernel_arg = getattr(args, "kernel", None)
    if db_arg and kernel_arg:
        _die("--db and --kernel are mutually exclusive")
    if args.command in {"versions", "build", "indexes", "use", "remove", "rm"}:
        selection = "--db" if db_arg else ("--kernel" if kernel_arg else None)
        if selection:
            _die(f"{selection} does not apply to {args.command!r}")
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
