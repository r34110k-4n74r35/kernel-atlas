"""Command line interface for kernel-atlas."""

from __future__ import annotations

import argparse
import csv
import re
import shlex
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import (config, cparse, db, indexer, kernelsrc, links, maintainers,
               query, relationships, render)
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


def emit(entries: list[Entry], args, kinds_listed: set[str], with_subsystem: bool,
         header: str = "", index: str | None = None,
         default_columns: tuple[str, ...] | None = None):
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


def _require_unique_symbol_identity(res: query.Resolution, spec: str) -> None:
    """Reject a guessed definition for commands whose output uses its line.

    ``info`` intentionally ranks and explains alternatives, but ``show``,
    ``path --line``, and ``web`` act on one concrete source identity.  A
    ``path:symbol`` qualifier is still ambiguous when conditional definitions
    repeat a name in the same file; ``path:line`` is the lossless spelling.
    """
    tail = (spec or "").strip().rpartition(":")[2]
    line_qualified = re.fullmatch(r"[+]?[1-9][0-9]*", tail) is not None
    target = res.target
    if line_qualified and (target is None or target.kind != "symbol"):
        # ``resolve`` deliberately falls back to the containing file so that
        # informational commands can still describe a real path.  Commands
        # that act on a concrete source identity must not silently reinterpret
        # a failed ``path:line`` selector as the whole file.
        _die(res.note or f"no symbol spans line {tail}")
    if target is None or target.kind != "symbol":
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
        ident=(t.name if t.kind == "symbol" else None))


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
    except (OSError, ValueError) as exc:
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
    kinds = _split_list(args.kinds) or list(cparse.DEFAULT_KINDS)
    bad = [kind for kind in kinds if kind not in cparse.ALL_KINDS]
    if bad:
        _die(f"unknown symbol kind(s): {', '.join(bad)} "
             f"(valid: {', '.join(cparse.ALL_KINDS)})")
    if args.with_calls and not ({"function", "syscall"} & set(kinds)):
        _die("--with-calls requires indexing function and/or syscall symbols")
    missing_call_kinds = {"macro", "variable"} - set(kinds)
    if args.with_calls and missing_call_kinds:
        _die("--with-calls requires macro and variable symbols so indirect or "
             "macro calls are not falsely linked to unrelated functions")

    if args.src:
        if args.keep_tarball or args.no_verify:
            _die("--keep-tarball and --no-verify only apply to downloaded source")
        tree = Path(args.src).expanduser().resolve()
        if not (tree / "MAINTAINERS").is_file():
            _die(f"{tree} does not look like a kernel tree (no MAINTAINERS file)")
        if args.version and args.version.lower() in {
                "lts", "longterm", "stable", "mainline", "latest"}:
            _die(f"version alias {args.version!r} does not apply with --src; "
                 "omit it to read the tree's Makefile")
        version = args.version or kernelsrc.detect_version(tree)
        if version is None:
            _die(f"could not detect a kernel version from {tree / 'Makefile'}; "
                 "pass an explicit version before --src")
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            _die(str(exc))
        source = str(tree)
    else:
        spec = args.version or "lts"
        try:
            rel = kernelsrc.resolve_version(spec)
        except (OSError, LookupError, ValueError) as exc:
            _die(str(exc))
        version = rel.version
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            _die(str(exc))
        if not quiet:
            print(f"kernel {version} ({rel.moniker})", file=sys.stderr)

    out = Path(args.output).expanduser() if args.output else config.index_path(version)
    if out.is_dir():
        _die(f"index output {out} is a directory")
    if out.exists() and not args.force:
        _die(f"index already exists at {out} (use --force to rebuild)")
    # Fail before a large download when the eventual managed source location
    # is already enough to prove that the output would be scanned as input.
    if not args.src and _path_inside(out, config.source_path(version)):
        _die(f"index output {out} is inside the source tree "
             f"{config.source_path(version)}; choose a path outside the tree")

    if not args.src:
        source = rel.source or kernelsrc.tarball_url(version)
        try:
            tree = kernelsrc.ensure_source(version, keep_tarball=args.keep_tarball,
                                           quiet=quiet, verify=not args.no_verify,
                                           source_url=source)
        except (OSError, RuntimeError) as exc:
            _die(f"could not obtain kernel source: {exc}")

    # Also check the actual tree returned by source discovery.  This covers
    # custom cache layouts and prevents the changing SQLite scratch file from
    # becoming one of the files being indexed.
    if _path_inside(out, tree):
        _die(f"index output {out} is inside the source tree {tree}; "
             "choose a path outside the tree")

    if not quiet:
        print(f"indexing {tree}", file=sys.stderr)
    try:
        stats = indexer.build(
            tree, out, version, kinds=kinds, want_calls=args.with_calls,
            jobs=args.jobs, quiet=quiet, source=source)
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        _die(f"could not build index: {exc}")
    size_mb = out.stat().st_size / (1024 * 1024)
    try:
        selectable_by_kernel = out.resolve() == config.index_path(version).resolve()
    except OSError:
        selectable_by_kernel = False
    if selectable_by_kernel:
        query_cmd = f"{PROG} -K {shlex.quote(out.stem)}"
    else:
        query_cmd = f"{PROG} --db {shlex.quote(str(out.resolve()))}"
    print(
        f"\nBuilt index for Linux {version}\n"
        f"  {stats.dirs:,} directories, {stats.files:,} files\n"
        f"  {stats.symbols:,} symbols from {stats.parsed:,} C/H files\n"
        + (f"  {stats.skipped:,} parse inputs skipped, {stats.failed:,} failed"
           f" ({stats.oversize:,} oversized)\n"
           if stats.skipped or stats.failed else "")
        + (f"  {stats.symlinks:,} symlinks recorded\n" if stats.symlinks else "")
        + (f"  {stats.calls:,} call edges: {stats.calls_resolved:,} resolved, "
           f"{stats.calls_ambiguous:,} ambiguous, {stats.calls_macro:,} macro, "
           f"{stats.calls_indirect:,} indirect, "
           f"{stats.calls_unresolved:,} unresolved\n" if stats.calls else "")
        + f"  {stats.subsystems:,} subsystems from MAINTAINERS\n"
        f"  {out}  ({size_mb:.0f} MB, {stats.seconds:.0f}s)\n"
        f"\nTry:  {query_cmd} info mm\n"
        f"      {query_cmd} siblings mm/page_alloc.c"
    )


def cmd_indexes(args):
    paths = config.list_indexes()
    if not paths:
        print(f"no indexes yet — run '{PROG} build lts'")
        return
    active = default_index() if paths else None
    rows = []
    for p in paths:
        conn = None
        error = None
        try:
            conn = db.connect(p, readonly=True)
            meta = db.validate_schema(conn)
        except (sqlite3.DatabaseError, OSError) as exc:
            meta = {}
            error = str(exc)
        finally:
            if conn is not None:
                conn.close()
        source_here = find_source_tree({
            "index_stem": p.stem,
            "kernel_version": meta.get("kernel_version", p.stem),
            "tree_path": meta.get("tree_path"),
        }) is not None
        version = meta.get("kernel_version") or p.stem
        rows.append({
            # The filename is a selection alias; the build metadata is the
            # identity used by links and every query result.
            "version": version,
            "alias": p.stem,
            "files": meta.get("n_files", "?"),
            "symbols": meta.get("n_symbols", "?"),
            "calls": meta.get("has_calls") == "1",
            "source": source_here,
            "built_at": meta.get("built_at", "?"),
            "size": f"{p.stat().st_size / 1048576:.0f} MB",
            "default": _same_path(p, active),
            "path": str(p),
            "error": error,
        })
    rows.sort(
        key=lambda row: _version_key(Path(f"{row['version']}.db")),
        reverse=True,
    )
    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return
    color = render.use_color(args.color)
    show_alias = any(r["alias"] != r["version"] for r in rows)
    alias_head = f" {'INDEX':<12}" if show_alias else ""
    print(f"    {'VERSION':<12}{alias_head} {'STATE':<7} {'FILES':>8} "
          f"{'SYMBOLS':>10} {'CALLS':<6} {'SOURCE':<7} {'BUILT':<20} "
          f"{'SIZE':>8}")
    for r in rows:
        mark = "*" if r["default"] else " "
        alias = f" {r['alias']:<12}" if show_alias else ""
        state = "broken" if r["error"] else "ok"
        line = (f"  {mark} {r['version']:<12}{alias} {state:<7} "
                f"{r['files']:>8} {r['symbols']:>10} "
                f"{'yes' if r['calls'] else '-':<6} "
                f"{'yes' if r['source'] else '-':<7} {r['built_at']:<20} "
                f"{r['size']:>8}")
        print(render.paint(line, "1", color) if r["default"] else line)
        if r["error"]:
            print(render.paint(f"      unusable: {r['error']} (rebuild this index)",
                               "31", color))
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
        conn = None
        try:
            conn = db.connect(active, readonly=True)
            db.validate_schema(conn)
        except (OSError, sqlite3.DatabaseError) as exc:
            _die(f"active index {active} is not usable ({exc}); rebuild it or "
                 f"select another with '{PROG} use <version>'")
        finally:
            if conn is not None:
                conn.close()
        print(f"active index: {active.stem}  ({active})")
        return
    path = resolve_index_spec(args.version)
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        db.validate_schema(conn)
    except (OSError, sqlite3.DatabaseError) as exc:
        _die(f"cannot use {path.stem!r}: {path} is not a usable index ({exc})")
    finally:
        if conn is not None:
            conn.close()
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


def _managed_source_recorded_by(path: Path) -> Path | None:
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
        source = meta.get("source")
        recorded_path = Path(recorded).expanduser()
        if isinstance(source, str) and source \
                and _same_path(Path(source).expanduser(), recorded_path):
            # ``build --src`` records the local input tree as its source.  A
            # coincidental conventional cache name does not make that
            # user-owned tree disposable.
            return None
        expected = config.source_path(version)
        return expected if _same_path(recorded_path, expected) else None
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def cmd_remove(args):
    # Resolve everything first so 'remove 6.18 6.18.45' (the second is the
    # expansion of the first) does not fail halfway through.
    unique: list[Path] = []
    for spec in args.versions:
        path = resolve_index_spec(spec)
        if path not in unique:
            unique.append(path)

    # Read provenance before unlinking the databases.  In particular, an
    # alias.db containing Linux 6.x metadata must not target linux-alias/.
    managed_sources = {
        path: _managed_source_recorded_by(path) if args.source else None
        for path in unique
    }

    freed = 0
    for path in unique:
        alias = path.stem
        size = _unlink_index(path)
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
            # This message is only a convenience.  It is safe to use the
            # filename alias here because no deletion follows from it.
            try:
                tree = config.source_path(alias)
            except ValueError:
                tree = None
        if not args.source and tree is not None and tree.is_dir():
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
    if meta.get("has_calls") == "1":
        total_calls = int(meta.get("n_calls", 0))
        resolved_calls = int(meta.get("n_calls_resolved", 0))
        print(f"  call edges   {total_calls:,} ({resolved_calls:,} resolved identities)")
        print(f"  call gaps    {int(meta.get('n_calls_ambiguous', 0)):,} ambiguous, "
              f"{int(meta.get('n_calls_macro', 0)):,} macro-only, "
              f"{int(meta.get('n_calls_indirect', 0)):,} indirect, "
              f"{int(meta.get('n_calls_unresolved', 0)):,} unresolved")
    skipped = int(meta.get("n_parse_skipped", 0))
    failed = int(meta.get("n_parse_failed", 0))
    if skipped or failed:
        print(f"  parse gaps   {skipped:,} skipped, {failed:,} failed"
              f" ({int(meta.get('n_oversize', 0)):,} oversized)")
    if int(meta.get("n_symlinks", 0)):
        print(f"  symlinks     {int(meta['n_symlinks']):,}")
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


def cmd_check(args):
    """Run the full row-level integrity audit on an index."""
    conn, meta = open_index(args)
    try:
        db.validate_schema(conn, deep=True)
    except (db.SchemaError, sqlite3.DatabaseError) as exc:
        _die(f"index integrity check failed: {exc}")
    payload = {
        "ok": True,
        "index": index_version(meta),
        "files": int(meta["n_files"]),
        "symbols": int(meta["n_symbols"]),
        "calls": int(meta["n_calls"]),
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return
    print(f"{_linux(meta)} index is structurally and semantically consistent")
    print(f"  {payload['files']:,} files, {payload['symbols']:,} symbols, "
          f"{payload['calls']:,} call edges checked")


def cmd_info(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target, meta)
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
    lnks = _links_for(meta, t)

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

    tree = find_source_tree(meta)
    source_entry = source_member(tree, t.path) if tree is not None else None
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
                _subsystem_payload(s)
                for s in subs[:args.max_subsystems]],
            "n_subsystems": len(subs),
            "unclassified_ownership": unclassified_payload,
            "ancestry": [{"path": p, "subsystem": s}
                         for p, s in query.ancestry(conn, t.path)],
            "links": lnks,
            "source_path": on_disk,
            "source_exists": source_exists,
            "index": index_version(meta),
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

    prefix = _command_prefix(args, meta)
    target_arg = shlex.quote(_target_spec(t))
    next_lines = [f"\n  Next:  {prefix} siblings {target_arg}"]
    if t.kind == "symbol" and t.symbol_kind in {"struct", "union"}:
        next_lines.append(f"         {prefix} struct {target_arg}")
    next_lines.append(f"         {prefix} web {target_arg}")
    print("\n".join(next_lines))


def cmd_struct(args):
    """Explain an indexed C struct/union and every retained member."""
    conn, meta = open_index(args)
    requested = (args.target or "").strip()
    kind_hint = None
    target_text = requested
    for aggregate_kind in ("struct", "union"):
        prefix = aggregate_kind + " "
        if target_text.startswith(prefix):
            kind_hint = aggregate_kind
            target_text = target_text[len(prefix):].strip()
            break
    spec = _normalize_target_spec(meta, target_text)
    lookup_spec = f"{kind_hint} {spec}" if kind_hint else spec
    res = query.resolve_structure(conn, lookup_spec)
    if res.target is None:
        kinds = set(filter(None, meta.get("kinds", "").split(",")))
        needed = {kind_hint} if kind_hint else {"struct", "union"}
        suffix = ("; rebuild the index with struct/union symbols enabled"
                  if not (needed & kinds) else "")
        _die((res.note or f"could not resolve aggregate {requested!r}") + suffix)

    if res.candidates and not args.all:
        candidates = [res.target, *res.candidates]
        selectors = [query.structure_selector(conn, candidate)
                     for candidate in candidates]
        exact = [selector for selector in selectors if selector is not None]
        examples = ", ".join(shlex.quote(selector) for selector in exact[:5])
        more = (f", and {len(exact) - 5} more"
                if len(exact) > 5 else "")
        if len(exact) != len(candidates):
            example_note = (f"; available exact selectors: {examples}{more}"
                            if examples else "")
            _die(f"{len(candidates)} aggregate definitions match {requested!r}; "
                 "at least one cannot be isolated by path:name or path:line"
                 f"{example_note}; pass --all")
        forms = {"path:line" if selector.rpartition(":")[2].isdigit()
                 else "path:name" for selector in exact}
        qualifier = " or ".join(sorted(forms))
        _die(f"{len(candidates)} aggregate definitions match {requested!r}; "
             f"use {qualifier} for one exact identity "
             f"(for example: {examples}{more}), or pass --all")

    targets = ([res.target, *res.candidates] if args.all else [res.target])
    definitions = []
    source_root = find_source_tree(meta)
    for target in targets:
        detail = query.structure_detail(conn, target)
        composition = query.all_subsystems(conn, "file", target.file_id or target.id)
        subsystems = [row for row in composition
                      if row["name"] not in query.CATCH_ALL]
        catch_all = next((
            row for row in composition
            if row["name"] in query.CATCH_ALL and bool(row["is_primary"])
        ), None)
        unmatched = not any(bool(row["is_primary"]) for row in composition)
        unclassified = None
        if catch_all is not None or unmatched:
            unclassified = {
                "is_primary": bool(catch_all["is_primary"])
                if catch_all is not None else False,
                "unmatched": unmatched,
                "match_score": (catch_all["score"]
                                if catch_all is not None else None),
                "match_rank": (catch_all["rank"]
                               if catch_all is not None else None),
                "maintainers_section": (catch_all["name"]
                                        if catch_all is not None else None),
            }
        area = query.describe_area(target.path)
        related = (query.documentation_for(conn, target, limit=args.max_docs)
                   if args.max_docs else [])
        recorded_root = meta.get("tree_path")
        display_root = (source_root if source_root is not None else
                        Path(recorded_root).expanduser()
                        if recorded_root else None)
        source = (source_member(display_root, target.path)
                  if display_root is not None else None)
        detail.update({
            "index": index_version(meta),
            "area": ({"name": area[0], "description": area[1]}
                     if area else None),
            "subsystems": [_subsystem_payload(row) for row in subsystems],
            "unclassified_ownership": unclassified,
            "links": _links_for(meta, target),
            "related_documentation": [
                {
                    "path": entry.path,
                    "name": entry.name,
                    "lines": entry.lines,
                    "size": entry.size,
                    "links": links.links(index_version(meta), entry.path),
                }
                for entry in related
            ],
            "source_path": str(source) if source is not None else None,
            "source_exists": ((source.exists() or source.is_symlink())
                              if source is not None else None),
            "layout_limits": [
                "All preprocessor alternatives are source possibilities, not "
                "members proven to coexist in one configured kernel.",
                "Byte offsets, padding, alignment, and sizeof require a concrete "
                "configuration, architecture, compiler ABI, and expanded macros.",
            ],
        })
        definitions.append(detail)

    payload = {
        "query": args.target,
        "index": index_version(meta),
        "n_definitions": len(definitions),
        "definitions": definitions,
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return

    color = render.use_color(args.color)
    width = shutil.get_terminal_size((100, 24)).columns
    for index, detail in enumerate(definitions):
        if index:
            print()
        sys.stdout.write(render.render_structure(detail, color, width))
    target_spec = shlex.quote(
        f"{definitions[0]['path']}:{definitions[0]['line']}")
    prefix = _command_prefix(args, meta)
    print(f"\n  Next:  {prefix} show {target_spec}"
          f"\n         {prefix} docs {target_spec}"
          f"\n         {prefix} web {target_spec}")


def cmd_siblings(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target, meta)
    t = res.target
    scope = query.build_scope(conn, t, args.level)
    if scope.dir_sql is None and scope.file_sql is None and scope.sym_where is None:
        _die(f"cannot build a '{args.level}' scope for {t.display} ({scope.label})")

    kinds = symbol_filter_kinds(args, kinds_from_args(args, t))
    _reject_symbol_size_sort(args, kinds)
    if (args.level == "tree"
            and any(k in query.SYMBOL_KINDS for k in kinds)
            and not args.limit):
        _die("listing symbols across the whole tree needs -n N "
             "(there are millions; try -n 50, or 'find' for a name search)")
    sub = query.subsystem_for_target(conn, t)
    if (args.level == "subsystem" and sub is not None
            and sub["name"] in query.CATCH_ALL and not args.limit):
        _die("the target is claimed only by the catch-all THE REST subsystem; "
             "this scope is almost the whole tree and needs -n N")
    # Fetch one extra row so that dropping the target itself does not eat one
    # of the requested rows; subsystems are looked up only for what survives.
    grep = _checked_grep(args.grep)
    entries = query.collect(
        conn, scope, kinds, limit=args.limit + 1 if args.limit else 0,
        grep=grep,
        exported_only=args.exported, static=_static_mode(args),
        with_subsystem=False, sort=args.sort)

    target_entry = next((e for e in entries if _entry_is_target(e, t)), None)
    others = [e for e in entries if not _entry_is_target(e, t)]
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
                      or "subsystem" in _split_list(args.columns))
    if want_subsystem:
        query.annotate_subsystems(conn, entries)

    label = sub["name"] if sub else None
    if label in query.CATCH_ALL:
        area = query.describe_area(t.path)
        label = area[0] if area else None
    header = (f"Siblings of {t.display}  [{_linux(meta)}]\n"
              f"  level: {scope.label}"
              + (f"   subsystem: {label}" if label else "")
              + f"   showing: {', '.join(kinds)}\n")
    emit(entries, args, set(kinds), want_subsystem, header,
         index=index_version(meta))


def cmd_ls(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target or "", meta)
    t = res.target
    if t.kind == "symbol":
        prefix = _command_prefix(args, meta)
        target = shlex.quote(_target_spec(t))
        _die(f"{t.display} is a symbol; try '{prefix} siblings {target}'")

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
    kinds = symbol_filter_kinds(args, kinds)
    _reject_symbol_size_sort(args, kinds)
    want_subsystem = (args.with_subsystem
                      or "subsystem" in _split_list(args.columns))
    entries = query.collect(conn, scope, kinds, limit=args.limit,
                            grep=_checked_grep(args.grep),
                            exported_only=args.exported, static=_static_mode(args),
                            with_subsystem=want_subsystem, sort=args.sort)
    emit(entries, args, set(kinds), want_subsystem,
         f"{scope.label}  [{_linux(meta)}]\n", index=index_version(meta))


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
    conn, meta = open_index(args)
    mode = "exact" if args.exact else ("glob" if args.glob else
                                       ("prefix" if args.prefix else "substring"))
    if _split_list(args.kinds):
        kinds = [k for k in kinds_from_args(args, None) if k in query.SYMBOL_KINDS]
        if not kinds:
            _die("find only searches symbols; try --kinds function,struct,...")
    else:
        kinds = []
    _reject_symbol_size_sort(args, kinds or query.SYMBOL_KINDS)
    grep = _checked_grep(args.grep)
    explicit_columns = _split_list(args.columns)
    want_subsystem = (args.format != "names"
                      and (args.with_subsystem or not explicit_columns
                           or "subsystem" in explicit_columns))
    entries = query.search(conn, args.pattern, kinds=kinds, mode=mode,
                           limit=args.limit,
                           exported_only=args.exported,
                           with_subsystem=want_subsystem, grep=grep,
                           static=_static_mode(args), sort=args.sort)
    emit(entries, args, {"function"}, want_subsystem,
         f"Symbols matching {args.pattern!r} ({mode})  [{_linux(meta)}]\n",
         index=index_version(meta),
         default_columns=("kind", "name", "path", "line", "subsystem"))


def cmd_subsystems(args):
    conn, meta = open_index(args)
    order = {
        "size": "n_files DESC, name",
        "claimed": "n_files DESC, name",
        "primary": "n_primary_files DESC, name",
        "name": "name",
    }[args.sort]
    rows = conn.execute(
        f"SELECT * FROM subsystems ORDER BY {order}").fetchall()
    pattern = _checked_grep(args.grep)
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        rows = [r for r in rows if rx.search(r["name"] or "")]
    if args.limit:
        rows = rows[:args.limit]
    if args.format == "json":
        payload = []
        for row in rows:
            item = _subsystem_payload(row)
            item["index"] = index_version(meta)
            payload.append(item)
        sys.stdout.write(render.render_json(payload))
        return
    color = render.use_color(args.color)
    print(render.paint(f"{len(rows)} subsystems  [{_linux(meta)}]", "1", color))
    print(f"  {'CLAIMED':>7} {'PRIMARY':>7}  {'STATUS':<16} NAME")
    for r in rows:
        print(f"  {r['n_files']:>7,} {r['n_primary_files']:>7,}  "
              f"{r['status'] or '?':<16} {r['name']}")


def cmd_subsystem(args):
    conn, meta = open_index(args)
    rows = query.subsystem_by_name(conn, args.name)
    if not rows:
        prefix = _command_prefix(args, meta)
        _die(f"no subsystem matching {args.name!r} "
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
                "index": index_version(meta),
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
        payload = _subsystem_payload(s)
        payload["index"] = index_version(meta)
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


def cmd_tree(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target or "", meta)
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
    # Directory and file queries are separate; merge them by path before the
    # tree renderer records sibling insertion order.
    entries.sort(key=lambda e: (e.path, e.kind))
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
    res = resolve_or_die(conn, args.target, meta)
    _require_unique_symbol_identity(res, args.target)
    t = res.target
    if args.line and t.kind != "symbol":
        _die("--line only applies to symbols")
    tree = source_tree(meta)
    full = source_member(tree, t.path)
    if not full.exists() and not full.is_symlink():
        _die(f"{full} is missing from the source tree")
    if args.line and t.kind == "symbol":
        print(f"{full}:{t.line}")
    else:
        print(full)


def cmd_show(args):
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target, meta)
    _require_unique_symbol_identity(res, args.target)
    t = res.target
    if t.kind == "dir":
        prefix = _command_prefix(args, meta)
        target = shlex.quote(_target_spec(t))
        _die(f"{t.path} is a directory; try '{prefix} ls {target}'")
    if t.kind == "symbol" and args.lines:
        _die("--lines applies to files; use --context for a symbol")
    if t.kind != "symbol" and args.context:
        _die("--context applies to symbols; use --lines for a file")
    tree = source_tree(meta)
    full = source_member(tree, t.path)
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
        try:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
        except ValueError:
            _die("--lines contains a line number that is too large")
        if start < 1 or end < 1:
            _die("--lines line numbers must be >= 1")
        if end < start:
            _die(f"--lines {args.lines!r}: end is before start")
    else:
        size = full.stat().st_size
        if size > _MAX_SHOW:
            prefix = _command_prefix(args, meta)
            _die(f"{t.path} is {size:,} bytes; pass --lines N:M or open it with "
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
    version = index_version(meta)
    picked = frames if args.limit == 0 else frames[:args.limit]
    for name in picked:
        res = query.resolve_symbol(conn, name)
        t = res.target
        if (t is None or t.kind != "symbol"
                or t.symbol_kind not in ("function", "syscall")):
            results.append({"frame": name, "found": False, "index": version})
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
            "ambiguous": sum(
                candidate.symbol_kind in ("function", "syscall")
                for candidate in res.candidates),
            "index": version,
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
    _reject_symbol_size_sort(args, query.SYMBOL_KINDS)
    if meta.get("has_calls") != "1":
        advice = _call_graph_rebuild_advice(args, meta)
        _die(f"this index ({index_version(meta)}) has no call graph — {advice}")
    target_spec = _normalize_target_spec(meta, args.target)
    res = (resolve_or_die(conn, target_spec) if ":" in target_spec
           else query.resolve_symbol(conn, target_spec))
    if res.target is None:
        _die(res.note)
    t = res.target
    if t.kind != "symbol" or t.symbol_kind not in ("function", "syscall"):
        _die(f"{t.display} is not a function or syscall")
    callable_alternatives = [
        candidate for candidate in res.candidates
        if candidate.symbol_kind in ("function", "syscall")
    ]
    if callable_alternatives:
        candidates = [t, *callable_alternatives]
        same_file = len({candidate.path for candidate in candidates}) \
            < len(candidates)
        qualifier = "path:line" if same_file else "path:symbol"
        examples = ", ".join(
            f"{candidate.path}:{candidate.line}" if same_file
            else candidate.display
            for candidate in candidates[:3])
        _die(f"{len(callable_alternatives) + 1} callable definitions are named "
             f"{t.name!r}; qualify the target as {qualifier}"
             + (f" (for example: {examples})" if examples else ""))
    if _split_list(args.kinds):
        selected_kinds = kinds_from_args(args, None)
        invalid = [kind for kind in selected_kinds
                   if kind not in ("function", "syscall")]
        if not selected_kinds or invalid:
            _die("calls only lists function and syscall identities; use "
                 "--kinds function,syscall")

    narrowing = bool(args.grep or args.static_only or args.no_static
                     or args.exported or _split_list(args.kinds))
    fetch = 0 if narrowing or args.sort != "name" else args.limit
    explicit_columns = _split_list(args.columns)
    want_subsystem = (args.format != "names"
                      and (args.with_subsystem
                           or "subsystem" in explicit_columns))
    default_columns = ("kind", "name", "path", "line", "resolution") + (
        ("subsystem",) if want_subsystem else ())

    if args.callers:
        entries = _post_filter(query.callers(conn, t.id, limit=fetch), args)
        query.sort_entries(entries, args.sort)
        if args.limit:
            entries = entries[:args.limit]
        if want_subsystem:
            query.annotate_subsystems(conn, entries)
        emit(entries, args, {"function"}, want_subsystem,
             f"Functions that call {t.display}  [{_linux(meta)}]\n",
             index=index_version(meta),
             default_columns=default_columns)
        return

    entries = query.callee_entries(conn, t.id, limit=fetch)
    entries = _post_filter(entries, args)
    query.sort_entries(entries, args.sort)
    if args.limit:
        entries = entries[:args.limit]
    if want_subsystem:
        query.annotate_subsystems(conn, entries)
    emit(entries, args, {"function"}, want_subsystem,
         f"Functions called by {t.display}  [{_linux(meta)}]\n",
         index=index_version(meta),
         default_columns=default_columns)


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
    """Show ownership overlap and conservative direct-call flow."""
    if args.via == "ownership":
        if args.direction != "both" or args.include_internal or args.min_calls != 1:
            _die("--direction, --include-internal and --min-calls apply only to "
                 "call relationships")
    elif args.via == "calls" and args.min_shared != 1:
        _die("--min-shared applies only to ownership relationships")
    conn, meta = open_index(args)
    subsystem, note = _relationship_subsystem(conn, meta, args.target)
    version = index_version(meta)

    overlaps = []
    if args.via in {"all", "ownership"}:
        overlaps = relationships.ownership_overlaps(
            conn, subsystem["id"], min_files=args.min_shared, limit=args.limit)

    has_calls = meta.get("has_calls") == "1"
    if args.via == "calls" and not has_calls:
        advice = _call_graph_rebuild_advice(args, meta)
        _die(f"this index ({version}) has no call graph — {advice}")
    flows = []
    coverage = None
    if args.via in {"all", "calls"} and has_calls:
        flows = relationships.call_flows(
            conn, subsystem["id"], direction=args.direction,
            include_internal=args.include_internal,
            min_edges=args.min_calls, limit=args.limit)
        coverage = relationships.call_resolution_coverage(conn, subsystem["id"])

    summary = {
        "name": subsystem["name"],
        "status": subsystem["status"],
        "claimed_files": subsystem["n_files"],
        "primary_files": subsystem["n_primary_files"],
    }
    payload = {
        "subsystem": summary,
        "resolved_from": note,
        "index": version,
        "call_graph_available": has_calls,
        "ownership_overlaps": [row.as_dict() for row in overlaps],
        "call_flows": [row.as_dict() for row in flows],
        "outgoing_call_resolution": coverage,
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return

    if args.format == "csv":
        fields = (
            "relationship", "direction", "selected_subsystem", "subsystem",
            "source_subsystem", "target_subsystem", "unclassified", "edges",
            "shared_files", "selected_files", "other_files",
            "selected_coverage", "other_coverage", "jaccard",
            "callers", "callees", "source_files", "target_files", "internal",
            "total_calls", "resolved_calls", "same_file", "included_source",
            "unique_global", "ambiguous", "macro", "indirect", "unresolved",
            "index",
        )
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        for row in overlaps:
            writer.writerow({
                "relationship": "ownership", "direction": "overlap",
                "selected_subsystem": subsystem["name"],
                "subsystem": row.subsystem, "shared_files": row.shared_files,
                "selected_files": row.selected_files,
                "other_files": row.other_files,
                "selected_coverage": row.selected_coverage,
                "other_coverage": row.other_coverage, "jaccard": row.jaccard,
                "index": version,
            })
        for row in flows:
            other = row.subsystem or ""
            source_subsystem = (subsystem["name"]
                                if row.direction == "outgoing" else other)
            target_subsystem = (other if row.direction == "outgoing"
                                else subsystem["name"])
            writer.writerow({
                "relationship": "call", "direction": row.direction,
                "selected_subsystem": subsystem["name"],
                "subsystem": other, "source_subsystem": source_subsystem,
                "target_subsystem": target_subsystem,
                "unclassified": row.unclassified, "edges": row.edges,
                "callers": row.callers, "callees": row.callees,
                "source_files": row.source_files,
                "target_files": row.target_files, "internal": row.internal,
                "index": version,
            })
        if coverage is not None:
            writer.writerow({
                "relationship": "call_resolution", "direction": "outgoing",
                "selected_subsystem": subsystem["name"],
                "total_calls": coverage["total"],
                "resolved_calls": coverage["resolved"],
                "same_file": coverage["same_file"],
                "included_source": coverage["included_source"],
                "unique_global": coverage["unique_global"],
                "ambiguous": coverage["ambiguous"],
                "macro": coverage["macro"],
                "indirect": coverage["indirect"],
                "unresolved": coverage["unresolved"],
                "index": version,
            })
        return

    color = render.use_color(args.color)
    print(render.paint(f"{subsystem['name']} relationships  [{_linux(meta)}]",
                       "1;35", color))
    if note:
        print(render.paint(f"  ({note})", "33", color))
    print(f"  files  {subsystem['n_files']:,} claimed, "
          f"{subsystem['n_primary_files']:,} primary")

    if args.via in {"all", "ownership"}:
        print(render.paint("\n  Ownership overlap", "1", color))
        if overlaps:
            print(f"    {'SHARED':>6} {'THIS':>7} {'OTHER':>7} {'JACCARD':>8}  "
                  "SUBSYSTEM")
            for row in overlaps:
                print(f"    {row.shared_files:>6,} "
                      f"{row.selected_coverage:>7.1%} "
                      f"{row.other_coverage:>7.1%} {row.jaccard:>8.1%}  "
                      f"{row.subsystem}")
        else:
            print("    no overlap at this threshold")

    if args.via in {"all", "calls"}:
        print(render.paint("\n  Direct C invocation flow", "1", color))
        if not has_calls:
            advice = _call_graph_rebuild_advice(args, meta)
            print(f"    unavailable — {advice}")
        elif flows:
            print(f"    {'DIRECTION':<9} {'EDGES':>7} {'CALLERS':>7} "
                  f"{'CALLEES':>7}  SUBSYSTEM")
            for row in flows:
                label = ("unclassified (MAINTAINERS catch-all)"
                         if row.unclassified else (row.subsystem or "?"))
                if row.internal:
                    label += " (internal)"
                print(f"    {row.direction:<9} {row.edges:>7,} "
                      f"{row.callers:>7,} {row.callees:>7,}  {label}")
        else:
            print("    no resolved cross-subsystem calls at this threshold")
        if coverage is not None:
            excluded = (coverage["ambiguous"] + coverage["macro"]
                        + coverage["indirect"]
                        + coverage["unresolved"])
            print(render.paint(
                f"\n    outgoing resolution: {coverage['resolved']:,}/"
                f"{coverage['total']:,} edges resolved; {excluded:,} retained "
                "only as ambiguity/macro/indirect/unresolved coverage",
                "90", color))


def cmd_web(args):
    """Print Elixir / git.kernel.org / GitHub / docs.kernel.org URLs."""
    conn, meta = open_index(args)
    res = resolve_or_die(conn, args.target, meta)
    _require_unique_symbol_identity(res, args.target)
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
    res = _resolve_area(conn, args.target, meta)
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
    prefix = _command_prefix(args, meta)
    first = shlex.quote(entries[0].path)
    print(render.paint(f"\n{len(entries)} file{'s' if len(entries) != 1 else ''}"
                       f"   Next: {prefix} web {first}", "90", color))


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
    rest.sort(key=_index_version_key, reverse=True)
    ordered = ([active] if active is not None and any(_same_path(p, active) for p in available)
               else []) + rest

    spec = args.target
    resolved_spec = spec
    rows = []
    for path in ordered:
        conn = None
        is_active = _same_path(path, active)
        try:
            try:
                conn = db.connect(path, readonly=True)
                meta = db.validate_schema(conn)
                meta["index_stem"] = path.stem
                version = index_version(meta)
                # An absolute path belongs to the active index's recorded
                # source tree.  Normalize it once there, then reuse the
                # repository-relative target when comparing other versions.
                if is_active:
                    resolved_spec = _normalize_target_spec(meta, spec)
                res = query.resolve(conn, resolved_spec)
            except (sqlite3.Error, OSError) as exc:
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

    sp = add("check", aliases=["doctor"],
             help="deep-check index counts and call identities")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=cmd_check)

    sp = add("info", help="explain one folder, file or symbol")
    sp.add_argument("target", help="mm | mm/page_alloc.c | tcp_sendmsg | "
                                   "tcp.c:tcp_sendmsg | mm/page_alloc.c:5268")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.add_argument("--max-subsystems", type=_nonneg_int, default=3)
    sp.add_argument("--max-candidates", type=_nonneg_int, default=10)
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
    match.add_argument("--exact", action="store_true")
    match.add_argument("--glob", action="store_true",
                       help="pattern is a glob (tcp_*)")
    match.add_argument("--prefix", action="store_true")
    _add_filter_opts(sp)
    _add_output_opts(sp, limit_default=50)
    sp.set_defaults(func=cmd_find)

    sp = add("subsystems", help="list subsystems from MAINTAINERS")
    sp.add_argument("--grep", "-g")
    sp.add_argument("--sort", default="size",
                    choices=("size", "claimed", "primary", "name"))
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
    _add_filter_opts(sp)
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
