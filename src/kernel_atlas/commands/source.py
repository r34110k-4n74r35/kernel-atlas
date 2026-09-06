"""Recorded source paths, containment checks, and source display commands."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..storage import config
from ..queries import query
from ..presentation import render
from ..presentation.terminal import Console, clean
from .output import _die
from .selection import index_version


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


_MAX_SHOW = 2 * 1024 * 1024


def cmd_path(args, support):
    """Print the on-disk path for use with `$EDITOR "$(ka path target)"`."""
    conn, meta = support.open_index(args)
    res = support.resolve_or_die(conn, args.target, meta)
    support._require_unique_symbol_identity(res, args.target, conn)
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
    support._require_unique_symbol_identity(res, args.target, conn)
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
            support._die(
                f"{t.path} is {size:,} bytes; pass --lines N:M or open it "
                f'with $EDITOR "$({prefix} path {shlex.quote(t.path)})"')
        start, end = 1, None

    color = render.use_color(args.color)
    if not args.bare:
        console = Console(args.color)
        sub = query.subsystem_for_target(conn, t)
        head = f"{t.path}:{start}" + (f"-{end}" if end else "")
        if t.kind == "symbol":
            head = f"{t.path}:{t.line}  {t.name}"
        label = sub["name"] if sub and sub["name"] not in query.CATCH_ALL else None
        console.heading(head)
        console.field("index", support._linux(meta), tone="muted")
        if label:
            console.field("subsystem", label, tone="accent")
        console.blank()
    printed = 0
    try:
        with full.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i < start:
                    continue
                if end is not None and i > end:
                    break
                prefix = "" if args.bare else render.paint(f"{i:6} | ", "90", color)
                # Keep --bare source untouched. In the numbered view retain tabs
                # for code indentation, but do not let source text inject ANSI.
                content = line.rstrip("\n")
                if not args.bare:
                    content = "\t".join(clean(part) for part in content.split("\t"))
                print(prefix + content)
                printed += 1
    except OSError as exc:
        support._die(f"cannot read {full}: {exc}")
    if printed == 0:
        support._die(f"{t.path} has no line {start}")
