"""Build a kernel index: walk the tree, parse C, attach subsystems."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import cparse, db, maintainers

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
        except OSError:
            out.append((file_id, 0, ()))
            continue
        if len(data) > MAX_READ or b"\0" in data[:8192]:
            out.append((file_id, 0, ()))
            continue
        lines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        syms: tuple = ()
        if parse:
            try:
                parsed = cparse.parse_source(data, _W_KINDS, _W_CALLS)
            except Exception:
                parsed = []
            syms = tuple(
                (s.name, s.kind, s.start_line, s.end_line, s.signature,
                 int(s.is_static), int(s.is_inline), int(s.is_exported), s.calls)
                for s in parsed
            )
        out.append((file_id, lines, syms))
    return out


@dataclass
class BuildStats:
    dirs: int = 0
    files: int = 0
    parsed: int = 0
    symbols: int = 0
    calls: int = 0
    subsystems: int = 0
    seconds: float = 0.0


def _scan_tree(tree: Path, conn: sqlite3.Connection, quiet: bool):
    """Insert every directory and file, breadth-first so parents exist first."""
    dir_rows: list[tuple] = [(1, "", None, tree.name, 0)]
    file_rows: list[tuple] = []
    pending: list[tuple[int, str, bool]] = []

    next_dir_id = 2
    next_file_id = 1
    queue = deque([(tree, "", 1)])

    while queue:
        abs_dir, rel_dir, parent_id = queue.popleft()
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
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
                except OSError:
                    size = 0
                file_rows.append((next_file_id, rel, parent_id, entry.name, ext, size))
                pending.append((next_file_id, rel, ext in PARSE_EXTS))
                next_file_id += 1

    conn.executemany(
        "INSERT INTO dirs(id, path, parent_id, name, depth) VALUES (?,?,?,?,?)", dir_rows)
    conn.executemany(
        "INSERT INTO files(id, path, dir_id, name, ext, size) VALUES (?,?,?,?,?,?)",
        file_rows)
    conn.commit()
    if not quiet:
        print(f"  tree: {len(dir_rows):,} directories, {len(file_rows):,} files",
              file=sys.stderr)
    return len(dir_rows), len(file_rows), pending


def _parse_all(tree: Path, conn: sqlite3.Connection, pending, kinds, want_calls,
               jobs: int, quiet: bool) -> tuple[int, int, int]:
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]
    total_files = len(pending)
    n_parsed = sum(1 for _, _, parse in pending if parse)
    n_sym = n_calls = 0
    done = 0
    started = time.time()

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
            for file_id, lines, syms in result:
                conn.execute("UPDATE files SET lines=?, n_symbols=? WHERE id=?",
                             (lines, len(syms), file_id))
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
              f"{time.time() - started:.1f}s{' ' * 20}", file=sys.stderr)
    return n_parsed, n_sym, n_calls


def _attach_subsystems(tree: Path, conn: sqlite3.Connection, quiet: bool,
                       max_per_path: int = 5) -> int:
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
            for rank, (sec, score) in enumerate(smap.match(path)[:max_per_path]):
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
    started = time.time()
    jobs = jobs or min(os.cpu_count() or 4, 16)
    stats = BuildStats()

    # Build into a scratch file and rename only on success, so an interrupted
    # build can never masquerade as a finished index.
    scratch = out.with_name(out.name + ".building")
    conn = db.create(scratch)
    try:
        stats.dirs, stats.files, pending = _scan_tree(tree, conn, quiet)
        stats.parsed, stats.symbols, stats.calls = _parse_all(
            tree, conn, pending, kinds, want_calls, jobs, quiet)
        stats.subsystems = _attach_subsystems(tree, conn, quiet)
        _rollup(conn)
        stats.seconds = time.time() - started

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
                ("build_seconds", f"{stats.seconds:.1f}"),
            ],
        )
        db.finalize(conn)
    except BaseException:
        conn.close()
        scratch.unlink(missing_ok=True)
        raise
    conn.close()
    scratch.replace(out)
    return stats
