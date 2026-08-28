"""Build a kernel index: walk the tree, parse C, attach subsystems."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import call_resolution, config, cparse, db, maintainers

PARSE_EXTS = {".c", ".h"}
SKIP_DIRS = {".git", ".github", ".svn", "__pycache__"}
# Files bigger than this are almost certainly generated blobs.
MAX_READ = 2 * 1024 * 1024
BATCH = 250

_MAKE_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z0-9_./${}()%-]+)\s*(\+=|:=|\?=|=)\s*(.*?)\s*$")
_MAKE_VAR_RE = re.compile(r"\$[({]([A-Za-z0-9_-]+)[)}]")
_MAKE_ADDPREFIX_RE = re.compile(
    r"\$\(addprefix\s+([^,\s()$]+)\s*,\s*([^()$]*)\)")
_SOURCE_INCLUDE_RE = re.compile(
    r'(?m)^[ \t]*#[ \t]*include[ \t]*"([^"\r\n]+\.c)"')

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
                 int(s.is_static), int(s.is_inline), int(s.is_exported), s.calls,
                 s.indirect_calls)
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
            conn.executemany(
                "INSERT INTO calls(caller_id, callee, resolution) VALUES (?,?,?)",
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
                for (name, kind, start, end, sig, st, inl, exp, calls,
                     indirect_calls) in syms:
                    sym_rows.append((next_sym_id, file_id, name, kind, start, end,
                                     sig, st, inl, exp))
                    indirect = set(indirect_calls)
                    for callee in calls:
                        call_rows.append((
                            next_sym_id, callee,
                            "indirect" if callee in indirect else "unresolved",
                        ))
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


def _make_assignments(path: Path) -> tuple[dict[str, list[str]], str]:
    """Return conservative variable assignments from one Kbuild/Makefile.

    This is intentionally not a make interpreter.  Host/user program lists and
    their ``*-objs`` mappings use simple assignments in practice; unknown make
    functions remain unexpanded and are ignored rather than guessed.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        continued = stripped.endswith("\\")
        piece = stripped[:-1] if continued else stripped
        current += (" " if current else "") + piece
        if not continued:
            logical.append(current)
            current = ""
    if current:
        logical.append(current)

    values: dict[str, list[str]] = defaultdict(list)
    for line in logical:
        line = line.split("#", 1)[0]
        match = _MAKE_ASSIGN_RE.match(line)
        if match is None:
            continue
        name, operator, value = match.groups()
        if operator in {"=", ":=", "?="} and name not in values:
            values[name] = [value]
        else:
            values[name].append(value)
    return values, text


def _expand_make_value(value: str, values: dict[str, list[str]]) -> str:
    for _ in range(5):
        expanded = _MAKE_VAR_RE.sub(
            lambda match: (" ".join(values[match.group(1)])
                           if match.group(1) in values else match.group(0)),
            value)
        expanded = _MAKE_ADDPREFIX_RE.sub(
            lambda match: " ".join(
                match.group(1) + token
                for token in match.group(2).split()),
            expanded,
        )
        if expanded == value:
            break
        value = expanded
    return value


def _source_token(directory: str, token: str) -> str | None:
    token = token.strip().replace("${src}/", "").replace("$(src)/", "")
    token = token.replace("${obj}/", "").replace("$(obj)/", "")
    if not token or "$" in token or "%" in token:
        return None
    if token.endswith(".o"):
        token = token[:-2] + ".c"
    elif not token.endswith(".c"):
        token += ".c"
    joined = os.path.normpath(os.path.join(directory, token)).replace(os.sep, "/")
    if joined == ".." or joined.startswith("../"):
        return None
    return joined.removeprefix("./")


def _is_program_list(name: str) -> bool:
    """Recognize Kbuild variables that name independently linked programs."""
    if name in {"hostprogs", "host-progs", "userprogs", "tprogs-y"}:
        return True
    return re.fullmatch(
        r"(?:hostprogs|userprogs)-always-(?:[ym]|\$\([^)]+\))", name
    ) is not None


def _is_kbuild_object_list(name: str) -> bool:
    """Return whether a make variable provides compile/link object evidence."""
    if re.fullmatch(
            r"(?:obj|lib)-(?:[ymn]|\$\([^)]+\)|\$\{[^}]+\})", name):
        return True
    if name.endswith("-objs"):
        return True
    # Composite objects conventionally use <target>-y/-m.  Exclude the flag
    # families whose values are compiler/linker options rather than objects.
    if name.startswith(("ccflags-", "subdir-ccflags-", "asflags-",
                        "ldflags-", "rustflags-")):
        return False
    return re.fullmatch(
        r".+-(?:[ym]|\$\([^)]+\)|\$\{[^}]+\})", name
    ) is not None


def _special_call_domain(path: str) -> str | None:
    """Return a conservative domain for non-vmlinux target-side images."""
    if path.endswith(".bpf.c"):
        return f"isolated:{path}"
    if path.startswith("drivers/firmware/efi/libstub/"):
        return "image:efi-stub"

    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "arch":
        return None
    arch = parts[1]
    if parts[2] == "boot":
        unit = ("boot-compressed" if len(parts) > 3
                and parts[3] == "compressed" else "boot")
        return f"image:arch:{arch}:{unit}"
    if parts[2] in {"purgatory", "realmode"}:
        return f"image:arch:{arch}:{parts[2]}"

    # vDSO implementation objects form a user-visible shared image.  Keep
    # kernel-side mapping/exception-table glue in the ordinary arch domain.
    vdso_at = next((i for i, part in enumerate(parts[2:-1], 2)
                    if part.startswith("vdso")), None)
    kernel_glue = {"extable.c", "vma.c", "vdso.c", "vdso32-setup.c"}
    if (vdso_at is not None and "include" not in parts[2:vdso_at]
            and parts[-1] not in kernel_glue):
        root = "/".join(parts[:vdso_at + 1])
        return f"image:{root}"
    return None


def _record_source_includes(tree: Path, conn: sqlite3.Connection) -> None:
    """Record parseable quoted C members used to assemble translation units."""
    existing = {
        row["path"]: row["id"]
        for row in conn.execute(
            "SELECT id,path FROM files WHERE ext='.c' AND index_status='parsed'")
    }
    rows: list[tuple[int, int, int]] = []
    for row in conn.execute(
            "SELECT id,path,size FROM files WHERE ext='.c' "
            "AND index_status='parsed'"):
        if row["size"] > MAX_READ:
            continue
        try:
            text = (tree / row["path"]).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        directory = row["path"].rpartition("/")[0]
        seen: set[int] = set()
        for match in _SOURCE_INCLUDE_RE.finditer(text):
            token = match.group(1)
            included_path = _source_token(directory, token)
            if included_path not in existing:
                # A few kernel translation units spell quoted C members from
                # the source-tree root (for example lib/vdso/*.c).  Prefer the
                # normal includer-relative interpretation, then accept the
                # root spelling only when it names an indexed, parseable file.
                root_path = _source_token("", token)
                if root_path in existing:
                    included_path = root_path
            included_id = existing.get(included_path or "")
            if (included_id is None or included_id == row["id"]
                    or included_id in seen):
                continue
            seen.add(included_id)
            line = text.count("\n", 0, match.start()) + 1
            rows.append((row["id"], included_id, line))
    conn.executemany(
        "INSERT INTO source_includes(includer_id,included_id,line)"
        " VALUES (?,?,?)", rows)


def _assign_call_domains(tree: Path, conn: sqlite3.Connection) -> None:
    """Separate kernel objects from independently linked host/user programs.

    Cross-file name resolution is meaningful only inside one linked program.
    Kbuild's hostprogs/userprogs and ``*-objs`` declarations provide explicit
    evidence for those units.  Unmodeled auxiliary trees stay file-isolated;
    this sacrifices uncertain edges instead of linking libc calls to unrelated
    kernel implementations with the same name.
    """
    existing_ids = {row["path"]: row["id"]
                    for row in conn.execute("SELECT id,path FROM files")}
    existing = set(existing_ids)
    parsed_c_ids = {row["path"]: row["id"] for row in conn.execute(
        "SELECT id,path FROM files WHERE ext='.c' AND index_status='parsed'")}
    explicit: dict[str, set[str]] = defaultdict(set)
    compiled_sources: set[str] = set()

    makefiles = sorted({*tree.rglob("Makefile"), *tree.rglob("Kbuild"),
                        *tree.rglob("Build")})
    for makefile in makefiles:
        values, text = _make_assignments(makefile)
        if not values:
            continue
        directory = makefile.parent.relative_to(tree).as_posix()
        if directory == ".":
            directory = ""
        # Any literal/expanded object token is direct evidence that its C
        # source can be compiled as a translation-unit root.  This matters for
        # dual-use files such as mm/vma.c and lib/decompress_*.c, which are
        # both objects and quoted members of other sources.
        for name, assigned in values.items():
            if not _is_kbuild_object_list(name):
                continue
            for value in assigned:
                for token in _expand_make_value(value, values).split():
                    if not token.endswith(".o"):
                        continue
                    source = _source_token(directory, token)
                    if source in parsed_c_ids:
                        compiled_sources.add(source)
        program_values = [
            value
            for name, assigned in values.items()
            if _is_program_list(name)
            for value in assigned
        ]
        programs: list[str] = []
        for value in program_values:
            programs.extend(_expand_make_value(value, values).split())

        # A few older in-tree utilities use a standalone BSD-style Makefile.
        # Require both a program/source declaration and explicit userland
        # evidence so ordinary kernel make variables are not reinterpreted.
        custom_sources: list[str] = []
        if "PROG" in values and ("userland app" in text.lower()
                                  or "CSRCS" in values):
            programs.extend(_expand_make_value(
                " ".join(values["PROG"]), values).split())
            for key in ("CSRCS", "SRCS"):
                for value in values.get(key, ()):
                    custom_sources.extend(
                        _expand_make_value(value, values).split())

        for program in programs:
            if "$" in program or program.startswith("-"):
                continue
            base = Path(program).name
            domain = f"program:{directory}:{program}"
            object_values = values.get(f"{base}-objs", ())
            candidates: list[str] = []
            for value in object_values:
                candidates.extend(_expand_make_value(value, values).split())
            if not object_values:
                candidates.append(program)
            if custom_sources:
                candidates.extend(custom_sources)
            for token in candidates:
                source = _source_token(directory, token)
                if source in existing:
                    explicit[source].add(domain)
                    if source in parsed_c_ids:
                        compiled_sources.add(source)

    updates: list[tuple[str, str]] = []
    main_files = {row["path"] for row in conn.execute(
        "SELECT DISTINCT f.path FROM files f JOIN symbols s ON s.file_id=f.id"
        " WHERE s.kind='function' AND s.name='main'")}
    for path in existing:
        domains = explicit.get(path)
        if domains:
            domain = next(iter(domains)) if len(domains) == 1 \
                else f"isolated:{path}"
        else:
            parts = path.split("/")
            special = _special_call_domain(path)
            auxiliary = (parts[0] in {"tools", "scripts", "Documentation"}
                         or "tools" in parts[1:-1]
                         or "Documentation" in parts[1:-1]
                         or path.startswith("arch/um/os-Linux/")
                         or (path.startswith("samples/bpf/")
                             and path.endswith(".c")))
            domain = (special or (f"isolated:{path}"
                                  if auxiliary or path in main_files
                                  else "kernel"))
        if domain != "kernel":
            updates.append((domain, path))
    conn.executemany("UPDATE files SET call_domain=? WHERE path=?", updates)
    conn.executemany(
        "INSERT OR IGNORE INTO translation_unit_roots(file_id) VALUES (?)",
        [(parsed_c_ids[path],) for path in sorted(compiled_sources)],
    )
    _record_source_includes(tree, conn)
    conn.commit()


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
    for rid, path in conn.execute("SELECT id, path FROM files").fetchall():
        matches = smap.match(path)
        if max_per_path is not None:
            matches = matches[:max_per_path]
        top_score = matches[0][1] if matches else None
        for rank, (sec, score) in enumerate(matches):
            rows.append(("file", rid, sec.id, score, rank,
                         int(score == top_score)))
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
    if not quiet:
        print(f"  subsystems: {len(smap.sections):,} sections from MAINTAINERS",
              file=sys.stderr)
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
        if want_calls:
            _assign_call_domains(tree, conn)
        stats.subsystems = _attach_subsystems(tree, conn, quiet)
        _rollup(conn)

        # Resolution needs the symbol/file indexes created by finalization.
        # It deliberately runs after every translation unit has been parsed so
        # uniqueness is decided across the complete index.
        db.finalize(conn)
        if want_calls:
            resolution = _resolve_calls(conn, keep_evidence=True)
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
                ("n_subsystems", str(stats.subsystems)),
                ("n_calls", str(stats.calls)),
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
        # Insert a provisional duration so the completed schema can be audited.
        # It is replaced after that publication audit, which can itself be a
        # material part of a multi-million-symbol build.
        conn.execute("ANALYZE main")
        stats.seconds = time.monotonic() - started
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)",
                     ("build_seconds", f"{stats.seconds:.1f}"))
        conn.commit()
        # Publication is atomic only after a full identity/count audit.  Normal
        # read commands perform the cheap schema check; users can explicitly
        # repeat this scan with ``kernel-atlas check``.
        if want_calls:
            db.validate_schema(
                conn, deep=True, reuse_call_evidence=True)
        else:
            db.validate_schema(conn, deep=True)
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
        scratch.replace(out)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return stats
