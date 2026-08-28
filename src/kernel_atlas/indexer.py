"""Build a kernel index: walk the tree, parse C, attach subsystems."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config, cparse, db, maintainers

PARSE_EXTS = {".c", ".h"}
SKIP_DIRS = {".git", ".github", ".svn", "__pycache__"}
# Files bigger than this are almost certainly generated blobs.
MAX_READ = 2 * 1024 * 1024
BATCH = 250

_W_KINDS: frozenset[str] = frozenset()
_W_CALLS = False
_W_ROOT = ""


def _init_worker(root: str, kinds: list[str], want_calls: bool) -> None:
    global _W_KINDS, _W_CALLS, _W_ROOT
    _W_KINDS = frozenset(kinds)
    _W_CALLS = want_calls
    _W_ROOT = root
    cparse._ensure_parser()


def _work(batch: list[tuple[int, str, bool]]):
    """Read each file once: count lines, and parse it when it is C."""
    out = []
    for file_id, rel, parse in batch:
        try:
            with open(os.path.join(_W_ROOT, rel), "rb") as fh:
                data = fh.read(MAX_READ + 1)
                if b"\0" in data[:8192]:
                    status = "skipped_binary" if parse else "binary"
                    out.append((file_id, 0, (), status, None, parse))
                    continue
                if len(data) > MAX_READ:
                    # Large generated headers are intentionally not handed to
                    # tree-sitter, but their line count remains useful and the
                    # skip is explicitly represented in the database.
                    n_bytes = len(data)
                    newlines = data.count(b"\n")
                    last = data[-1:]
                    while chunk := fh.read(1 << 20):
                        n_bytes += len(chunk)
                        newlines += chunk.count(b"\n")
                        last = chunk[-1:]
                    lines = newlines + (1 if n_bytes and last != b"\n" else 0)
                    status = "skipped_oversize" if parse else "indexed"
                    out.append((file_id, lines, (), status, None, parse))
                    continue
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"[:400]
            out.append((file_id, 0, (), "read_error", error, parse))
            continue
        lines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        syms: tuple = ()
        status = "indexed"
        error = None
        if parse:
            try:
                parsed = cparse.parse_source(data, _W_KINDS, _W_CALLS)
            except Exception as exc:
                parsed = []
                status = "parse_error"
                error = f"{type(exc).__name__}: {exc}"[:400]
            else:
                status = "parsed"
            syms = tuple(
                (s.name, s.kind, s.start_line, s.end_line, s.signature,
                 int(s.is_static), int(s.is_inline), int(s.is_exported), s.calls)
                for s in parsed
            )
        out.append((file_id, lines, syms, status, error, parse))
    return out


@dataclass
class BuildStats:
    dirs: int = 0
    files: int = 0
    parsed: int = 0
    symbols: int = 0
    calls: int = 0
    subsystems: int = 0
    skipped: int = 0
    failed: int = 0
    oversize: int = 0
    symlinks: int = 0
    seconds: float = 0.0


def _scan_tree(tree: Path, conn: sqlite3.Connection, quiet: bool):
    """Insert every directory and file, breadth-first so parents exist first."""
    dir_rows: list[tuple] = [(1, "", None, tree.name, 0)]
    file_rows: list[tuple] = []
    pending: list[tuple[int, str, bool]] = []

    next_dir_id = 2
    next_file_id = 1
    n_symlinks = 0
    n_symlink_parse = 0
    queue = deque([(tree, "", 1)])

    while queue:
        abs_dir, rel_dir, parent_id = queue.popleft()
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise RuntimeError(f"could not scan source directory {abs_dir}: {exc}") from exc
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"could not inspect source path {entry.path}: {exc}") from exc
            if is_symlink:
                ext = os.path.splitext(entry.name)[1].lower()
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise RuntimeError(
                        f"could not stat source file {entry.path}: {exc}") from exc
                try:
                    link_target = os.readlink(entry.path)
                except OSError as exc:
                    link_target = None
                    link_error = f"{type(exc).__name__}: {exc}"[:400]
                else:
                    link_error = None
                file_rows.append((next_file_id, rel, parent_id, entry.name, ext, size,
                                  1, link_target, "symlink", link_error))
                n_symlinks += 1
                if ext in PARSE_EXTS:
                    n_symlink_parse += 1
                next_file_id += 1
            elif is_dir:
                if entry.name in SKIP_DIRS:
                    continue
                dir_rows.append((next_dir_id, rel, parent_id, entry.name,
                                 rel.count("/") + 1))
                queue.append((entry.path, rel, next_dir_id))
                next_dir_id += 1
            elif is_file:
                ext = os.path.splitext(entry.name)[1].lower()
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise RuntimeError(
                        f"could not stat source file {entry.path}: {exc}") from exc
                file_rows.append((next_file_id, rel, parent_id, entry.name, ext, size,
                                  0, None, "pending", None))
                pending.append((next_file_id, rel, ext in PARSE_EXTS))
                next_file_id += 1

    conn.executemany(
        "INSERT INTO dirs(id, path, parent_id, name, depth) VALUES (?,?,?,?,?)", dir_rows)
    conn.executemany(
        "INSERT INTO files(id, path, dir_id, name, ext, size, is_symlink,"
        " link_target, index_status, index_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        file_rows)
    conn.commit()
    if not quiet:
        print(f"  tree: {len(dir_rows):,} directories, {len(file_rows):,} files",
              file=sys.stderr)
    return len(dir_rows), len(file_rows), pending, n_symlinks, n_symlink_parse


def _parse_all(tree: Path, conn: sqlite3.Connection, pending, kinds, want_calls,
               jobs: int, quiet: bool) -> tuple[int, int, int, int, int, int]:
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]
    total_files = len(pending)
    n_parsed = n_sym = n_calls = 0
    n_skipped = n_failed = n_oversize = 0
    done = 0
    started = time.monotonic()

    sym_rows: list[tuple] = []
    call_rows: list[tuple] = []
    next_sym_id = 1

    def flush() -> None:
        nonlocal sym_rows, call_rows
        if sym_rows:
            conn.executemany(
                "INSERT INTO symbols(id, file_id, name, kind, start_line, end_line,"
                " signature, is_static, is_inline, is_exported)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", sym_rows)
            sym_rows = []
        if call_rows:
            conn.executemany("INSERT INTO calls(caller_id, callee) VALUES (?,?)",
                             call_rows)
            call_rows = []

    with ProcessPoolExecutor(
        max_workers=jobs,
        initializer=_init_worker,
        initargs=(str(tree), list(kinds), want_calls),
    ) as pool:
        for result in pool.map(_work, batches):
            for file_id, lines, syms, status, error, parse in result:
                conn.execute(
                    "UPDATE files SET lines=?, n_symbols=?, index_status=?,"
                    " index_error=? WHERE id=?",
                    (lines, len(syms), status, error, file_id),
                )
                if status == "read_error" or status == "parse_error":
                    n_failed += 1
                elif parse and status == "parsed":
                    n_parsed += 1
                elif parse and status.startswith("skipped_"):
                    n_skipped += 1
                    if status == "skipped_oversize":
                        n_oversize += 1
                for (name, kind, start, end, sig, st, inl, exp, calls) in syms:
                    sym_rows.append((next_sym_id, file_id, name, kind, start, end,
                                     sig, st, inl, exp))
                    for callee in calls:
                        call_rows.append((next_sym_id, callee))
                        n_calls += 1
                    next_sym_id += 1
                    n_sym += 1
            done += len(result)
            if len(sym_rows) > 50_000:
                flush()
            if not quiet and done % 5000 < BATCH:
                pct = done * 100 // max(total_files, 1)
                print(f"\r  parsing {done:,}/{total_files:,} files ({pct}%) "
                      f"— {n_sym:,} symbols", end="", file=sys.stderr, flush=True)
    flush()
    conn.commit()
    if not quiet:
        print(f"\r  parsed {n_parsed:,} C files — {n_sym:,} symbols in "
              f"{time.monotonic() - started:.1f}s"
              + (f"; {n_skipped:,} skipped, {n_failed:,} failed"
                 if n_skipped or n_failed else "")
              + f"{' ' * 20}", file=sys.stderr)
    return n_parsed, n_sym, n_calls, n_skipped, n_failed, n_oversize


def _attach_subsystems(tree: Path, conn: sqlite3.Connection, quiet: bool,
                       max_per_path: int | None = None) -> int:
    smap = maintainers.load(tree)
    if not smap.sections:
        if not quiet:
            print("  no MAINTAINERS file found; skipping subsystem mapping",
                  file=sys.stderr)
        return 0

    conn.executemany(
        "INSERT INTO subsystems(id, name, status, maintainers, reviewers, lists,"
        " trees, web) VALUES (?,?,?,?,?,?,?,?)",
        [(s.id, s.name, s.status, json.dumps(s.maintainers), json.dumps(s.reviewers),
          json.dumps(s.lists), json.dumps(s.trees), s.web) for s in smap.sections],
    )

    rows: list[tuple] = []
    for ref_kind, table in (("file", "files"), ("dir", "dirs")):
        for rid, path in conn.execute(f"SELECT id, path FROM {table}").fetchall():
            if not path:
                continue
            matches = smap.match(path)
            if max_per_path is not None:
                matches = matches[:max_per_path]
            for rank, (sec, score) in enumerate(matches):
                rows.append((ref_kind, rid, sec.id, score, rank))
            if len(rows) > 200_000:
                conn.executemany(
                    "INSERT INTO path_subsys(ref_kind, ref_id, subsystem_id, score,"
                    " rank) VALUES (?,?,?,?,?)", rows)
                rows = []
    if rows:
        conn.executemany(
            "INSERT INTO path_subsys(ref_kind, ref_id, subsystem_id, score, rank)"
            " VALUES (?,?,?,?,?)", rows)
    conn.commit()
    if not quiet:
        print(f"  subsystems: {len(smap.sections):,} sections from MAINTAINERS",
              file=sys.stderr)
    return len(smap.sections)


def _rollup(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        UPDATE dirs SET
          n_files   = (SELECT COUNT(*) FROM files f WHERE f.dir_id = dirs.id),
          n_subdirs = (SELECT COUNT(*) FROM dirs d  WHERE d.parent_id = dirs.id);
        UPDATE subsystems SET n_files = (
          SELECT COUNT(*) FROM path_subsys p
          WHERE p.subsystem_id = subsystems.id AND p.ref_kind = 'file');
        """
    )
    conn.commit()


def build(tree: Path, out: Path, version: str, kinds=cparse.DEFAULT_KINDS,
          want_calls: bool = False, jobs: int | None = None,
          quiet: bool = False, source: str = "kernel.org") -> BuildStats:
    started = time.monotonic()
    version = config.validate_version(version)
    tree = Path(tree).resolve()
    out = Path(out).expanduser()
    if not tree.is_dir():
        raise ValueError(f"kernel source tree does not exist: {tree}")
    try:
        publication = out.parent.resolve() / out.name
        publication.relative_to(tree)
    except ValueError:
        pass
    else:
        raise ValueError(
            f"index output {out} is inside the source tree {tree}")
    if jobs is None:
        jobs = min(os.cpu_count() or 4, 16)
    elif jobs < 1:
        raise ValueError("jobs must be at least 1")
    kinds = tuple(kinds)
    stats = BuildStats()

    # A unique same-directory scratch file keeps publication atomic without
    # letting two concurrent builds unlink or publish one another's work.
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch_name = tempfile.mkstemp(
        prefix=f".{out.name}.", suffix=".building", dir=out.parent)
    os.close(fd)
    scratch = Path(scratch_name)
    conn: sqlite3.Connection | None = None
    try:
        conn = db.create(scratch)
        (stats.dirs, stats.files, pending, stats.symlinks,
         symlink_parse) = _scan_tree(tree, conn, quiet)
        (stats.parsed, stats.symbols, stats.calls, stats.skipped,
         stats.failed, stats.oversize) = _parse_all(
            tree, conn, pending, kinds, want_calls, jobs, quiet)
        stats.skipped += symlink_parse
        stats.subsystems = _attach_subsystems(tree, conn, quiet)
        _rollup(conn)

        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?,?)",
            [
                ("schema_version", db.SCHEMA_VERSION),
                ("kernel_version", version),
                ("source", source),
                ("tree_path", str(tree)),
                ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
                ("kinds", ",".join(kinds)),
                ("has_calls", "1" if want_calls else "0"),
                ("n_dirs", str(stats.dirs)),
                ("n_files", str(stats.files)),
                ("n_symbols", str(stats.symbols)),
                ("n_subsystems", str(stats.subsystems)),
                ("n_parse_skipped", str(stats.skipped)),
                ("n_parse_failed", str(stats.failed)),
                ("n_oversize", str(stats.oversize)),
                ("n_symlinks", str(stats.symlinks)),
            ],
        )
        db.finalize(conn)
        # SQL index creation and ANALYZE are a material part of a large build;
        # include them in the persisted/reportable duration.
        stats.seconds = time.monotonic() - started
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)",
                     ("build_seconds", f"{stats.seconds:.1f}"))
        conn.commit()
    except BaseException:
        if conn is not None:
            conn.close()
        scratch.unlink(missing_ok=True)
        raise
    assert conn is not None
    conn.close()
    try:
        scratch.replace(out)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return stats
