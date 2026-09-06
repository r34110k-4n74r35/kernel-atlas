"""Build a kernel index: walk the tree, parse C, attach subsystems."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import call_resolution, config, cparse, db, maintainers
from .progress import Progress

from .kbuild import (
    _make_logical_lines as _make_logical_lines,
    _make_assignments as _make_assignments,
    _expand_make_value as _expand_make_value,
    _source_token as _source_token,
    _include_directory as _include_directory,
    _kbuild_include_directories as _kbuild_include_directories,
    _explicit_rule_sources as _explicit_rule_sources,
    _is_program_list as _is_program_list,
    _is_kbuild_object_list as _is_kbuild_object_list,
    _special_call_domain as _special_call_domain,
    _record_source_includes as _record_source_includes,
    _assign_call_domains as _assign_call_domains,
)

PARSE_EXTS = {".c", ".h", ".c_shipped", ".h_shipped"}
SKIP_DIRS = {".git", ".github", ".svn", "__pycache__"}
# Compatibility name for the shared default. ``build(max_file_bytes=...)`` can
# tune the same contract used by both the reader and parser.
MAX_READ = cparse.MAX_FILE_BYTES
BATCH = 250

_W_KINDS: frozenset[str] = frozenset()
_W_CALLS = False
_W_ROOT = ""
_W_MAX_FILE_BYTES = cparse.MAX_FILE_BYTES


def _init_worker(
        root: str, kinds: list[str], want_calls: bool,
        max_file_bytes: int) -> None:
    global _W_KINDS, _W_CALLS, _W_ROOT, _W_MAX_FILE_BYTES
    _W_KINDS = frozenset(kinds)
    _W_CALLS = want_calls
    _W_ROOT = root
    _W_MAX_FILE_BYTES = max_file_bytes
    cparse._ensure_parser()


def _work(batch: list[tuple[int, str, bool]]):
    """Read each file once: count lines, and parse it when it is C."""
    out = []
    for file_id, rel, parse in batch:
        try:
            with open(os.path.join(_W_ROOT, rel), "rb") as fh:
                data = fh.read(_W_MAX_FILE_BYTES + 1)
                if b"\0" in data[:8192]:
                    status = "skipped_binary" if parse else "binary"
                    out.append((file_id, 0, (), status, None, parse))
                    continue
                if len(data) > _W_MAX_FILE_BYTES:
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
                parsed = cparse.parse_source(
                    data, _W_KINDS, _W_CALLS,
                    max_file_bytes=_W_MAX_FILE_BYTES)
            except Exception as exc:
                parsed = []
                status = "parse_error"
                error = f"{type(exc).__name__}: {exc}"[:400]
            else:
                status = "parsed"
            syms = tuple(
                (s.name, s.kind, s.start_line, s.end_line, s.signature,
                 int(s.is_static), int(s.is_inline), int(s.is_exported), s.calls,
                 s.indirect_calls,
                 tuple((site.name, site.kind, site.start_line, site.start_byte)
                       for site in s.call_sites),
                 s.summary, s.description,
                 tuple((m.parent_index, m.name, m.kind, m.type_text,
                        m.declaration, m.start_line, m.end_line, m.bit_width,
                        m.array_dimensions, m.description,
                        m.description_source, m.conditions, m.visibility,
                        int(m.is_anonymous), m.generated_by)
                       for m in s.members),
                 s.aliases, int(s.is_anonymous), int(s.parse_complete),
                 s.parse_warnings, s.unmatched_member_docs, s.conditions)
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
    call_occurrences: int = 0
    calls_resolved: int = 0
    calls_ambiguous: int = 0
    calls_macro: int = 0
    calls_indirect: int = 0
    calls_unresolved: int = 0
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

    with Progress("Scanning source tree", unit="files", quiet=quiet) as progress:
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

            progress.update(len(file_rows), detail=f"{len(dir_rows):,} directories")

        conn.executemany(
            "INSERT INTO dirs(id, path, parent_id, name, depth) VALUES (?,?,?,?,?)", dir_rows)
        conn.executemany(
            "INSERT INTO files(id, path, dir_id, name, ext, size, is_symlink,"
            " link_target, index_status, index_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            file_rows)
        conn.commit()
    return len(dir_rows), len(file_rows), pending, n_symlinks, n_symlink_parse


def _parse_all(tree: Path, conn: sqlite3.Connection, pending, kinds, want_calls,
               jobs: int, quiet: bool, max_file_bytes: int
               ) -> tuple[int, int, int, int, int, int]:
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]
    total_files = len(pending)
    n_parsed = n_sym = n_calls = 0
    n_skipped = n_failed = n_oversize = 0
    done = 0
    sym_rows: list[tuple] = []
    alias_rows: list[tuple] = []
    member_rows: list[tuple] = []
    call_rows: list[tuple] = []
    next_sym_id = 1
    next_member_id = 1

    def flush() -> None:
        nonlocal sym_rows, alias_rows, member_rows, call_rows
        if sym_rows:
            conn.executemany(
                "INSERT INTO symbols(id, file_id, name, kind, start_line, end_line,"
                " signature, summary, description, is_static, is_inline,"
                " is_exported, is_anonymous, parse_complete, parse_warnings,"
                " unmatched_member_docs, conditions)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sym_rows)
            sym_rows = []
        if alias_rows:
            conn.executemany(
                "INSERT INTO type_aliases(symbol_id,name) VALUES (?,?)",
                alias_rows)
            alias_rows = []
        if member_rows:
            conn.executemany(
                "INSERT INTO type_members(id,symbol_id,parent_id,ordinal,name,"
                " kind,type_text,declaration,start_line,end_line,bit_width,"
                " array_dimensions,description,description_source,conditions,"
                " visibility,is_anonymous,generated_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", member_rows)
            member_rows = []
        if call_rows:
            conn.executemany(
                "INSERT INTO calls(caller_id,callee,resolution,direct_count,"
                " indirect_count,macro_count) VALUES (?,?,?,?,?,?)",
                call_rows)
            call_rows = []

    with ProcessPoolExecutor(
        max_workers=jobs,
        initializer=_init_worker,
        initargs=(str(tree), list(kinds), want_calls, max_file_bytes),
    ) as pool:
        # map submits eagerly. Start workers before the refresh thread so
        # fork-based runtimes cannot inherit the renderer's thread/lock.
        results = pool.map(_work, batches)
        with Progress("Parsing sources", total=total_files, unit="files",
                      detail=f"{jobs} workers", quiet=quiet) as progress:
            for result in results:
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
                    for (name, kind, start, end, sig, st, inl, exp, calls,
                         indirect_calls, call_sites, summary, description,
                         members, aliases,
                         anonymous, parse_complete, parse_warnings,
                         unmatched_docs, conditions) in syms:
                        sym_rows.append((next_sym_id, file_id, name, kind, start, end,
                                         sig, summary, description, st, inl, exp,
                                         anonymous, parse_complete,
                                         json.dumps(parse_warnings),
                                         json.dumps(dict(unmatched_docs)),
                                         json.dumps(conditions)))
                        for alias in aliases:
                            alias_rows.append((next_sym_id, alias))
                        member_ids = list(range(
                            next_member_id, next_member_id + len(members)))
                        for ordinal, member in enumerate(members):
                            (parent_index, member_name, member_kind, type_text,
                             declaration, member_start, member_end, bit_width,
                             dimensions, member_description, description_source,
                             conditions, visibility, member_anonymous,
                             generated_by) = member
                            parent_id = (member_ids[parent_index]
                                         if parent_index is not None else None)
                            member_rows.append((
                                member_ids[ordinal], next_sym_id, parent_id, ordinal,
                                member_name, member_kind, type_text, declaration,
                                member_start, member_end, bit_width,
                                json.dumps(dimensions), member_description,
                                description_source, json.dumps(conditions),
                                visibility, member_anonymous, generated_by,
                            ))
                        next_member_id += len(members)
                        sites_by_name: dict[str, list[tuple]] = defaultdict(list)
                        for site in call_sites:
                            sites_by_name[site[0]].append(site)
                        indirect = set(indirect_calls)
                        for callee in calls:
                            sites = sites_by_name.get(callee, ())
                            direct_n = sum(site[1] == "direct" for site in sites)
                            indirect_n = sum(
                                site[1] == "indirect" for site in sites)
                            macro_n = sum(site[1] == "macro" for site in sites)
                            if not sites:
                                indirect_n = int(callee in indirect)
                                direct_n = 1 - indirect_n
                            initial = "unresolved" if direct_n else (
                                "macro" if macro_n else "indirect")
                            call_rows.append((
                                next_sym_id, callee, initial,
                                direct_n, indirect_n, macro_n,
                            ))
                            n_calls += 1
                        next_sym_id += 1
                        n_sym += 1
                done += len(result)
                if len(sym_rows) > 50_000 or len(member_rows) > 100_000:
                    flush()
                progress.update(done, detail=(
                    f"{jobs} workers; {n_sym:,} symbols; {n_calls:,} calls; "
                    f"{n_skipped:,} skipped; {n_failed:,} failed"))
            flush()
            conn.commit()
    return n_parsed, n_sym, n_calls, n_skipped, n_failed, n_oversize


def _attach_subsystems(tree: Path, conn: sqlite3.Connection, quiet: bool,
                       max_per_path: int | None = None) -> int:
    with Progress("Mapping ownership", unit="files", quiet=quiet) as progress:
        smap = maintainers.load(tree)
        if not smap.sections:
            progress.update(detail="no MAINTAINERS sections; nothing to map")
            return 0

        conn.executemany(
            "INSERT INTO subsystems(id, name, status, maintainers, reviewers, lists,"
            " trees, websites, patchwork, bugs, chats, profiles, keywords)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(s.id, s.name, s.status, json.dumps(s.maintainers), json.dumps(s.reviewers),
              json.dumps(s.lists), json.dumps(s.trees), json.dumps(s.websites),
              json.dumps(s.patchwork), json.dumps(s.bugs), json.dumps(s.chats),
              json.dumps(s.profiles), json.dumps(s.keywords)) for s in smap.sections],
        )

        rows: list[tuple] = []
        # F:/N: rules describe files.  Matching a bare directory string against
        # them produces false gaps (``kernel/futex``) and false ownership
        # (wildcards which merely happen to match child directory names).
        paths = conn.execute("SELECT id, path FROM files").fetchall()
        progress.update(total=len(paths),
                        detail=f"{len(smap.sections):,} MAINTAINERS sections")
        for done, (rid, path) in enumerate(paths, 1):
            matches = smap.match(path)
            if max_per_path is not None:
                matches = matches[:max_per_path]
            top_score = matches[0][1] if matches else None
            for rank, (sec, score) in enumerate(matches):
                rows.append(("file", rid, sec.id, score, rank,
                             int(score == top_score)))
            progress.update(done)
            if len(rows) > 200_000:
                conn.executemany(
                    "INSERT INTO path_subsys(ref_kind, ref_id, subsystem_id, score,"
                    " rank, is_primary) VALUES (?,?,?,?,?,?)", rows)
                rows = []
        if rows:
            conn.executemany(
                "INSERT INTO path_subsys(ref_kind, ref_id, subsystem_id, score, rank,"
                " is_primary) VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        return len(smap.sections)


def _derive_directory_composition(conn: sqlite3.Connection) -> None:
    """Roll descendant file ownership up into every containing directory.

    A directory can be a boundary shared by many MAINTAINERS sections.  Store
    both all claims and top-score (possibly co-primary) ownership so callers can present that
    composition instead of inventing a single owner from an F: glob.
    """
    dir_ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM dirs")}
    subsystem_names = {
        r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM subsystems")
    }
    totals: Counter[int] = Counter()
    counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])

    def ancestors(file_path: str):
        directory = file_path.rpartition("/")[0]
        while True:
            did = dir_ids.get(directory)
            if did is not None:
                yield did
            if not directory:
                break
            directory = directory.rpartition("/")[0]

    current_id: int | None = None
    current_path = ""
    matches: list[tuple[int, bool]] = []

    def flush() -> None:
        if current_id is None:
            return
        for did in ancestors(current_path):
            totals[did] += 1
            for sid, is_primary in matches:
                bucket = counts[(did, sid)]
                bucket[0] += 1
                if is_primary:
                    bucket[1] += 1

    rows = conn.execute(
        "SELECT f.id, f.path, p.subsystem_id, p.rank, p.is_primary FROM files f"
        " LEFT JOIN path_subsys p ON p.ref_kind='file' AND p.ref_id=f.id"
        " ORDER BY f.id, p.rank"
    )
    for row in rows:
        fid = row["id"]
        if current_id is not None and fid != current_id:
            flush()
            matches = []
        current_id = fid
        current_path = row["path"]
        if row["subsystem_id"] is not None:
            matches.append((row["subsystem_id"], bool(row["is_primary"])))
    flush()

    conn.executemany(
        "UPDATE dirs SET n_files_recursive=? WHERE id=?",
        [(total, did) for did, total in totals.items()],
    )
    root = dir_ids.get("")
    if root is not None:
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.execute("UPDATE dirs SET n_files_recursive=? WHERE id=?",
                     (total_files, root))

    grouped: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for (did, sid), (claimed, primary) in counts.items():
        grouped[did].append((sid, claimed, primary))
    payload: list[tuple] = []
    for did, items in grouped.items():
        items.sort(key=lambda item: (
            subsystem_names.get(item[0]) == "THE REST",
            -item[2], -item[1], subsystem_names.get(item[0], ""), item[0],
        ))
        total = totals[did]
        for rank, (sid, claimed, primary) in enumerate(items):
            payload.append((did, sid, claimed, primary,
                            primary / total if total else 0.0, rank))
    conn.executemany(
        "INSERT INTO dir_subsys(dir_id, subsystem_id, n_claimed, n_primary,"
        " coverage, rank) VALUES (?,?,?,?,?,?)",
        payload,
    )


def _rollup(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        UPDATE dirs SET
          n_files   = (SELECT COUNT(*) FROM files f WHERE f.dir_id = dirs.id),
          n_subdirs = (SELECT COUNT(*) FROM dirs d  WHERE d.parent_id = dirs.id);
        UPDATE subsystems SET n_files = (
          SELECT COUNT(*) FROM path_subsys p
          WHERE p.subsystem_id = subsystems.id AND p.ref_kind = 'file');
        UPDATE subsystems SET n_primary_files = (
          SELECT COUNT(*) FROM path_subsys p
            WHERE p.subsystem_id = subsystems.id AND p.ref_kind = 'file'
            AND p.is_primary = 1);
        """
    )
    _derive_directory_composition(conn)
    conn.commit()


def _resolve_calls(conn: sqlite3.Connection, *, keep_evidence: bool = False) \
        -> dict[str, int]:
    """Resolve calls only when every containing translation unit agrees."""
    return call_resolution.resolve(conn, keep_evidence=keep_evidence)


def _validate_build_kinds(kinds) -> tuple[str, ...]:
    """Normalize the public build API's symbol-kind selection."""
    if isinstance(kinds, (str, bytes)):
        raise ValueError("kinds must be a non-empty iterable of kind names")
    try:
        chosen = tuple(kinds)
    except TypeError as exc:
        raise ValueError(
            "kinds must be a non-empty iterable of kind names") from exc
    if not chosen:
        raise ValueError("kinds must contain at least one kind name")
    if any(not isinstance(kind, str) for kind in chosen):
        raise ValueError("every kind must be a string")
    if len(set(chosen)) != len(chosen):
        raise ValueError("kinds must not contain duplicates")
    unknown = [kind for kind in chosen if kind not in cparse.ALL_KINDS]
    if unknown:
        raise ValueError(
            "unknown symbol kind"
            + ("s" if len(unknown) != 1 else "")
            + ": " + ", ".join(unknown)
            + f" (valid: {', '.join(cparse.ALL_KINDS)})")
    return chosen


def _validate_build_jobs(jobs: int | None) -> int:
    """Return a bounded worker count suitable for ProcessPoolExecutor."""
    if jobs is None:
        return min(os.cpu_count() or 4, 16)
    if not isinstance(jobs, int) or isinstance(jobs, bool):
        raise ValueError("jobs must be an integer between 1 and 256")
    if not 1 <= jobs <= 256:
        raise ValueError("jobs must be between 1 and 256")
    return jobs


def build(tree: Path, out: Path, version: str, kinds=cparse.DEFAULT_KINDS,
          want_calls: bool = False, jobs: int | None = None,
          quiet: bool = False, source: str | None = None, *,
          max_file_bytes: int = cparse.MAX_FILE_BYTES,
          managed_tree_identity: dict[str, str] | None = None,
          pre_publish: Callable[[], None] | None = None) -> BuildStats:
    # Validate pure API options before resolving paths or creating publication
    # scratch files.  Library callers should get stable option errors without
    # partially starting an expensive build.
    kinds = _validate_build_kinds(kinds)
    jobs = _validate_build_jobs(jobs)
    if want_calls:
        chosen = set(kinds)
        if not ({cparse.FUNCTION, cparse.SYSCALL} & chosen):
            raise ValueError(
                "call indexing requires function and/or syscall symbols")
        missing_blockers = {cparse.MACRO, cparse.VARIABLE} - chosen
        if missing_blockers:
            raise ValueError(
                "conservative call resolution also requires indexing: "
                + ", ".join(sorted(missing_blockers)))

    started = time.monotonic()
    version = config.validate_version(version)
    tree = Path(tree).resolve()
    out = Path(out).expanduser()
    if not tree.is_dir():
        raise ValueError(f"kernel source tree does not exist: {tree}")
    if out.is_dir():
        raise ValueError(f"index output is a directory: {out}")
    if source is None:
        # Direct library callers are indexing the tree they supplied.  Only
        # the download lifecycle has enough provenance to pass a kernel.org
        # archive URL and enable authoritative upstream links.
        source = str(tree)
    elif not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    identity_keys = {
        "managed_tree_id", "managed_tree_device", "managed_tree_inode",
        "managed_tree_digest",
    }
    if managed_tree_identity is not None:
        if (set(managed_tree_identity) != identity_keys
                or any(not isinstance(value, str) or not value
                       for value in managed_tree_identity.values())):
            raise ValueError("managed tree identity metadata is incomplete")
    if pre_publish is not None and not callable(pre_publish):
        raise ValueError("pre_publish must be callable")
    try:
        publication = out.parent.resolve() / out.name
        publication.relative_to(tree)
    except ValueError:
        pass
    else:
        raise ValueError(
            f"index output {out} is inside the source tree {tree}")
    max_file_bytes = cparse.validate_max_file_bytes(max_file_bytes)
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
            tree, conn, pending, kinds, want_calls, jobs, quiet,
            max_file_bytes)
        stats.skipped += symlink_parse
        if want_calls:
            with Progress("Analyzing build domains", quiet=quiet):
                _assign_call_domains(tree, conn)
        stats.subsystems = _attach_subsystems(tree, conn, quiet)
        with Progress("Summarizing directories", quiet=quiet):
            _rollup(conn)

        # Resolution needs the symbol/file indexes created by finalization.
        # It deliberately runs after every translation unit has been parsed so
        # uniqueness is decided across the complete index.
        with Progress("Creating database indexes", quiet=quiet):
            db.finalize(conn)
        if want_calls:
            with Progress("Resolving calls", detail=f"{stats.calls:,} call records",
                          quiet=quiet):
                resolution = _resolve_calls(conn, keep_evidence=True)
            stats.call_occurrences = int(conn.execute(
                "SELECT COALESCE(SUM(direct_count+indirect_count+macro_count),0)"
                " FROM calls").fetchone()[0])
            stats.calls_resolved = (
                resolution["same_file"] + resolution["included_source"]
                + resolution["unique_global"])
            stats.calls_ambiguous = resolution["ambiguous"]
            stats.calls_macro = resolution["macro"]
            stats.calls_indirect = resolution["indirect"]
            stats.calls_unresolved = resolution["unresolved"]

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
                ("n_type_aliases", str(conn.execute(
                    "SELECT COUNT(*) FROM type_aliases").fetchone()[0])),
                ("n_type_members", str(conn.execute(
                    "SELECT COUNT(*) FROM type_members").fetchone()[0])),
                ("n_subsystems", str(stats.subsystems)),
                ("n_calls", str(stats.calls)),
                ("n_call_occurrences", str(stats.call_occurrences)),
                ("n_calls_resolved", str(stats.calls_resolved)),
                ("n_calls_ambiguous", str(stats.calls_ambiguous)),
                ("n_calls_macro", str(stats.calls_macro)),
                ("n_calls_indirect", str(stats.calls_indirect)),
                ("n_calls_unresolved", str(stats.calls_unresolved)),
                ("n_parse_skipped", str(stats.skipped)),
                ("n_parse_failed", str(stats.failed)),
                ("n_oversize", str(stats.oversize)),
                ("n_symlinks", str(stats.symlinks)),
            ],
        )
        if managed_tree_identity is not None:
            conn.executemany(
                "INSERT INTO meta(key,value) VALUES (?,?)",
                sorted(managed_tree_identity.items()),
            )
        # Insert a provisional duration so the completed schema can be audited.
        # It is replaced after that publication audit, which can itself be a
        # material part of a multi-million-symbol build.
        with Progress("Analyzing database statistics", quiet=quiet):
            conn.execute("ANALYZE main")
        stats.seconds = time.monotonic() - started
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)",
                     ("build_seconds", f"{stats.seconds:.1f}"))
        conn.commit()
        # Publication is atomic only after a full identity/count audit.  Normal
        # read commands perform the cheap schema check; users can explicitly
        # repeat this scan with ``kernel-atlas check``.
        with Progress("Validating index", detail="full integrity audit", quiet=quiet):
            if want_calls:
                db.validate_schema(
                    conn, deep=True, reuse_call_evidence=True)
            else:
                db.validate_schema(conn, deep=True)
        if pre_publish is not None:
            with Progress("Rechecking source identity", quiet=quiet):
                pre_publish()
        stats.seconds = time.monotonic() - started
        conn.execute("UPDATE meta SET value=? WHERE key='build_seconds'",
                     (f"{stats.seconds:.1f}",))
        conn.commit()
    except BaseException:
        if conn is not None:
            conn.close()
        scratch.unlink(missing_ok=True)
        raise
    assert conn is not None
    conn.close()
    try:
        with Progress("Publishing index", quiet=quiet):
            scratch.replace(out)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return stats
