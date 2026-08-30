"""Resolve targets and answer 'what sits at the same level as this?'.

The model is deliberately uniform: every target lives in a *container*.

    a folder or file   -> its parent directory
    a symbol           -> the file it is defined in

"Siblings" are the other members of that container, and ``--level`` widens the
container outwards (file -> dir -> subtree -> subsystem -> whole tree).
"""

from __future__ import annotations

import json
import re
import sqlite3

from . import maintainers
from .query_models import Entry, Resolution, Scope, Target
from .query_targeting import (
    is_copy_path as _is_copy_path,
    normalize_spec as _norm,
    path_rank as _path_rank,
    rank_candidate as _rank_candidate,
    symbol_target as _target_from_symbol_row,
)
from .structure_query import (
    resolve_structure as _resolve_structure,
    structure_detail as _structure_detail,
    structure_selector as _structure_selector,
)

SYMBOL_KINDS = ("function", "syscall", "struct", "union", "enum", "typedef",
                "macro", "variable", "prototype")
PATH_KINDS = ("dir", "file")
ALL_KINDS = PATH_KINDS + SYMBOL_KINDS

LEVELS = ("auto", "file", "dir", "subtree", "subsystem", "tree")


def parent_path(path: str) -> str:
    """Directory containing a file path; '' for a top-level file or the root."""
    if not path or "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def like_escape(s: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` so a kernel path is a literal LIKE prefix."""
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_under(path: str) -> str:
    """LIKE pattern for every path strictly below `path` (``''`` → the whole tree)."""
    if not path:
        return "%"
    return like_escape(path) + "/%"


def line_selector_suffix(spec: str) -> str | None:
    """Positive numeric suffix of an actual ``path:line`` selector.

    A wholly numeric path is still a path.  Keeping the delimiter check here
    prevents callers from independently recreating the subtle distinction.
    """
    raw = (spec or "").strip()
    if ":" not in raw:
        return None
    tail = raw.rpartition(":")[2]
    if re.fullmatch(r"[+]?\d+", tail) is None:
        return None
    try:
        return tail if int(tail) > 0 else None
    except ValueError:
        return None


def ambiguous_line_paths(conn: sqlite3.Connection, spec: str) -> list[str]:
    """Indexed files competing for a basename-only ``path:line`` selector.

    Generic informational resolution may use the symbol spanning a line to
    explain which same-named file was selected.  Commands acting on one exact
    source identity use this evidence to require a full indexed path instead.
    """
    if line_selector_suffix(spec) is None:
        return []
    head = _norm((spec or "").strip().rpartition(":")[0])
    if not head:
        return []
    if conn.execute("SELECT 1 FROM files WHERE path=?", (head,)).fetchone():
        return []
    if "/" in head or "\\" in head:
        return []
    paths = [
        row["path"]
        for row in conn.execute(
            "SELECT path FROM files WHERE name=?"
            " ORDER BY LENGTH(path),path,id",
            (head,),
        ).fetchall()
    ]
    return paths if len(paths) > 1 else []


def _sym_target(conn: sqlite3.Connection, row: sqlite3.Row) -> Target:
    # Keep the historical private signature for internal callers and tests.
    # The connection was never used to materialize the selected row.
    return _target_from_symbol_row(row)


_SYM_SELECT = """
SELECT s.id, s.file_id, s.name, s.kind, s.start_line, s.end_line, s.signature,
       s.is_static, s.is_inline, s.is_exported, f.path, f.dir_id
FROM symbols s JOIN files f ON f.id = s.file_id
"""

def resolve_symbol(conn: sqlite3.Connection, name: str) -> Resolution:
    """Resolve a bare name strictly in the symbol namespace."""
    raw = (name or "").strip()
    rows = conn.execute(_SYM_SELECT + " WHERE s.name = ?", (raw,)).fetchall()
    if not rows:
        return Resolution(None, note=f"no symbol in the index is named {raw!r}")
    cands = sorted((_sym_target(conn, r) for r in rows), key=_rank_candidate)
    note = ""
    if len(cands) > 1:
        note = (f"{len(cands)} symbols named {raw!r}; "
                "showing the most likely definition")
    return Resolution(cands[0], cands[1:], note)


def resolve_structure(conn: sqlite3.Connection, spec: str) -> Resolution:
    """Compatibility facade for aggregate-specific query resolution."""
    return _resolve_structure(conn, spec)


def structure_detail(conn: sqlite3.Connection, target: Target) -> dict:
    """Compatibility facade for the aggregate-specific detail payload."""
    return _structure_detail(conn, target)


def structure_selector(conn: sqlite3.Connection, target: Target) -> str | None:
    """Compatibility facade for reusable aggregate selectors."""
    return _structure_selector(conn, target)


def resolve(conn: sqlite3.Connection, spec: str) -> Resolution:
    raw = (spec or "").strip()
    path = _norm(raw)

    if path == "":
        row = conn.execute("SELECT * FROM dirs WHERE path = ''").fetchone()
        if row:
            return Resolution(Target(kind="dir", id=row["id"], path="",
                                     name=row["name"], dir_id=row["id"]))

    # path:symbol  or  path:line
    if ":" in raw:
        head, _, tail = raw.rpartition(":")
        head_n = _norm(head)
        line: int | None = None
        if re.fullmatch(r"[+-]?\d+", tail):
            try:
                line = int(tail)
            except ValueError:
                return Resolution(None, note=f"line number {tail!r} is too large")
            if line < 1:
                return Resolution(None, note="line number must be at least 1")
            if line > 2**63 - 1:
                return Resolution(None, note=f"line number {tail!r} is too large")
        frow = conn.execute("SELECT * FROM files WHERE path = ?",
                            (head_n,)).fetchone() if head_n else None
        exact_file = frow is not None
        if exact_file:
            frows = [frow]
        elif head_n and "/" not in head_n and "\\" not in head_n:
            # 'inode.c:ext4_bmap' — consider every file with that basename and
            # let the symbol pick out the right one.  A path-shaped qualifier,
            # on the other hand, is a claim about one exact file: silently
            # discarding misspelled directories can select unrelated code.
            frows = conn.execute(
                "SELECT * FROM files WHERE name = ? ORDER BY LENGTH(path), path",
                (head_n.rsplit("/", 1)[-1],)).fetchall()
        else:
            frows = []

        if frows and line is not None:
            hits = []
            for fr in frows:
                rows = conn.execute(
                    _SYM_SELECT + " WHERE s.file_id = ? AND s.start_line <= ?"
                    " AND s.end_line >= ? ORDER BY (s.end_line - s.start_line)"
                    ", s.start_line, s.kind, s.id",
                    (fr["id"], line, line)).fetchall()
                hits.extend(_sym_target(conn, row) for row in rows)
            if hits:
                # A source coordinate names the most specific enclosing
                # declaration.  Candidate quality remains the deterministic
                # tiebreaker for equal spans and basename matches.
                hits.sort(key=lambda target: (
                    (target.end_line or target.line or 0)
                    - (target.line or 0),
                    *_rank_candidate(target),
                ))
                note = f"line {line} falls inside this symbol"
                if not exact_file and len(frows) > 1:
                    note += (f" (matched {head_n.rsplit('/', 1)[-1]!r} to "
                             f"{hits[0].path})")
                return Resolution(hits[0], hits[1:], note)
            if exact_file:
                frow = frows[0]
                return Resolution(
                    Target(kind="file", id=frow["id"], path=frow["path"],
                           name=frow["name"], dir_id=frow["dir_id"],
                           file_id=frow["id"]),
                    note=f"no symbol spans line {line}")
            return Resolution(
                None, note=f"no symbol spans line {line} in any file named "
                           f"{head_n.rsplit('/', 1)[-1]!r}")

        if frows:
            ids = [r["id"] for r in frows]
            ph = ",".join("?" * len(ids))
            rows = conn.execute(
                _SYM_SELECT + f" WHERE s.file_id IN ({ph}) AND s.name = ?",
                (*ids, tail)).fetchall()
            if rows:
                cands = sorted((_sym_target(conn, r) for r in rows),
                               key=_rank_candidate)
                note = ""
                if not exact_file and len(frows) > 1:
                    note = (f"matched {head_n.rsplit('/', 1)[-1]!r} to "
                            f"{cands[0].path}")
                if len(cands) > 1:
                    ambiguity = (f"{len(cands)} definitions named {tail!r} "
                                 "match; use path:line for one exact identity")
                    note = f"{note}; {ambiguity}" if note else ambiguity
                return Resolution(cands[0], cands[1:], note)
            if exact_file:
                # The file is real, so don't fall through to guessing what the
                # whole string might mean — say precisely what is wrong.
                return Resolution(
                    None, note=f"{head_n} exists but defines no symbol "
                               f"named {tail!r}")

    row = conn.execute("SELECT * FROM dirs WHERE path = ?", (path,)).fetchone()
    if row:
        return Resolution(Target(kind="dir", id=row["id"], path=row["path"],
                                 name=row["name"], dir_id=row["id"]))

    row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
    if row:
        return Resolution(Target(kind="file", id=row["id"], path=row["path"],
                                 name=row["name"], dir_id=row["dir_id"],
                                 file_id=row["id"]))

    # Bare names can collide across namespaces (``sched`` is both a directory
    # and a small static helper).  Keep the oops-friendly symbol preference for
    # real global definitions, but do not let a file-local or tools/ copy hide a
    # kernel area directory.
    symbol_resolution = resolve_symbol(conn, raw)
    sym_cands = ([symbol_resolution.target] + symbol_resolution.candidates
                 if symbol_resolution.target is not None else [])

    file_rows = conn.execute("SELECT * FROM files WHERE name = ?", (raw,)).fetchall()
    file_cands = [
        Target(kind="file", id=r["id"], path=r["path"], name=r["name"],
               dir_id=r["dir_id"], file_id=r["id"])
        for r in file_rows
    ]
    file_cands.sort(key=lambda t: (*_path_rank(t.path), t.id))

    dir_rows = conn.execute("SELECT * FROM dirs WHERE name = ?", (raw,)).fetchall()
    dir_cands = [
        Target(kind="dir", id=r["id"], path=r["path"], name=r["name"],
               dir_id=r["id"])
        for r in dir_rows
    ]
    dir_cands.sort(key=lambda t: (*_path_rank(t.path), t.id))

    if sym_cands:
        best = sym_cands[0]
        if dir_cands and (best.is_static or _is_copy_path(best.path)):
            picked = dir_cands[0]
            why = "file-local" if best.is_static else "a tools/sample copy"
            note = (f"bare name {raw!r} also matches {len(sym_cands)} symbol"
                    f"{'s' if len(sym_cands) != 1 else ''}; using {picked.path}/ "
                    f"instead of {why} {best.display}")
            if len(dir_cands) > 1:
                note += f" ({len(dir_cands)} directories have this name)"
            return Resolution(picked, dir_cands[1:] + sym_cands, note)
        return symbol_resolution

    # Bare file name, e.g. 'inode.c'.
    if file_cands:
        note = (f"{len(file_cands)} files named {raw!r}"
                if len(file_cands) > 1 else "")
        return Resolution(file_cands[0], file_cands[1:], note)

    # Bare directory name, e.g. 'ext4'.
    if dir_cands:
        note = (f"{len(dir_cands)} directories named {raw!r}"
                if len(dir_cands) > 1 else "")
        return Resolution(dir_cands[0], dir_cands[1:], note)

    return Resolution(None, note=f"nothing in the index matches {raw!r}")


def all_subsystems(conn: sqlite3.Connection, ref_kind: str,
                   ref_id: int) -> list[sqlite3.Row]:
    if ref_kind == "dir":
        return conn.execute(
            "SELECT s.*, d.n_claimed, d.n_primary, d.coverage, d.rank"
            " FROM dir_subsys d JOIN subsystems s ON s.id=d.subsystem_id"
            " WHERE d.dir_id=? ORDER BY d.rank", (ref_id,)
        ).fetchall()
    return conn.execute(
        "SELECT s.*, p.score, p.rank, p.is_primary FROM path_subsys p"
        " JOIN subsystems s ON s.id = p.subsystem_id"
        " WHERE p.ref_kind = ? AND p.ref_id = ? ORDER BY p.rank",
        (ref_kind, ref_id)).fetchall()


# 'THE REST' carries `F: *` and `F: */`, so it claims every path in the tree.
# It is a real answer, but never the interesting one when anything else matches.
CATCH_ALL = {"THE REST"}


def visible_subsystems(rows: list) -> list:
    """Drop THE REST when a more specific MAINTAINERS section also matched."""
    specific = [r for r in rows if r["name"] not in CATCH_ALL]
    return specific if specific else list(rows)


def best_subsystem(conn: sqlite3.Connection, ref_kind: str,
                   ref_id: int) -> sqlite3.Row | None:
    if ref_kind == "dir":
        return uniform_directory_subsystem(conn, ref_id)
    if ref_kind == "file":
        return unique_file_subsystem(conn, ref_id)
    rows = all_subsystems(conn, ref_kind, ref_id)
    shown = visible_subsystems(rows)
    return shown[0] if shown else None


def file_primary_subsystems(conn: sqlite3.Connection,
                            file_id: int) -> list[sqlite3.Row]:
    """Every equally strongest MAINTAINERS owner for one file."""
    return conn.execute(
        "SELECT s.*,p.score,p.rank,p.is_primary FROM path_subsys p"
        " JOIN subsystems s ON s.id=p.subsystem_id"
        " WHERE p.ref_kind='file' AND p.ref_id=? AND p.is_primary=1"
        " ORDER BY p.rank", (file_id,)
    ).fetchall()


def unique_file_subsystem(conn: sqlite3.Connection,
                          file_id: int) -> sqlite3.Row | None:
    """Return a file owner only when the strongest evidence is unambiguous."""
    owners = file_primary_subsystems(conn, file_id)
    return owners[0] if len(owners) == 1 else None


def file_subsystem_label(conn: sqlite3.Connection, file_id: int,
                         path: str) -> str | None:
    """Compact file label without hiding equal-strength ownership evidence."""
    owners = file_primary_subsystems(conn, file_id)
    specific = [row for row in owners if row["name"] not in CATCH_ALL]
    if len(specific) == 1 and len(owners) == 1:
        return specific[0]["name"]
    if len(specific) > 1:
        return f"Co-owned ({len(specific)} subsystems)"
    area = describe_area(path)
    return area[0] if area else None


def directory_primary_subsystems(conn: sqlite3.Connection,
                                 dir_id: int) -> list[sqlite3.Row]:
    """Primary owners represented among a directory's descendant files.

    ``THE REST`` is retained here because a directory containing both
    specifically owned and catch-all files is still heterogeneous.
    """
    return conn.execute(
        "SELECT s.*, d.n_claimed, d.n_primary, d.coverage, d.rank"
        " FROM dir_subsys d JOIN subsystems s ON s.id=d.subsystem_id"
        " WHERE d.dir_id=? AND d.n_primary > 0 ORDER BY d.rank",
        (dir_id,),
    ).fetchall()


def uniform_directory_subsystem(conn: sqlite3.Connection,
                                dir_id: int) -> sqlite3.Row | None:
    """Return a subsystem only when every descendant file has that owner."""
    owners = directory_primary_subsystems(conn, dir_id)
    if (len(owners) == 1 and owners[0]["name"] not in CATCH_ALL
            and float(owners[0]["coverage"]) == 1.0):
        return owners[0]
    return None


def directory_unclaimed_files(conn: sqlite3.Connection, path: str) -> int:
    """Descendant files with no primary MAINTAINERS evidence at all."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM files f"
        " WHERE f.path LIKE ? ESCAPE '\\'"
        " AND NOT EXISTS (SELECT 1 FROM path_subsys p"
        " WHERE p.ref_kind='file' AND p.ref_id=f.id AND p.is_primary=1)",
        (like_under(path),),
    ).fetchone()[0])


def directory_subsystem_label(conn: sqlite3.Connection, dir_id: int,
                              path: str) -> str | None:
    """Truthful compact label for listings which have only one text column."""
    uniform = uniform_directory_subsystem(conn, dir_id)
    if uniform is not None:
        return uniform["name"]
    owners = directory_primary_subsystems(conn, dir_id)
    specific = [row for row in owners if row["name"] not in CATCH_ALL]
    unclaimed = directory_unclaimed_files(conn, path)
    unclassified = (bool(unclaimed)
                    or any(row["name"] in CATCH_ALL for row in owners))
    area = describe_area(path)
    if len(owners) > 1:
        if area:
            suffix = "; includes unclassified" if unclassified else ""
            return f"{area[0]} (mixed{suffix})"
        count = f"{len(specific)} subsystem" + ("s" if len(specific) != 1 else "")
        return f"Mixed ({count}" + (" + unclassified)" if unclassified else ")")
    if owners and owners[0]["name"] in CATCH_ALL:
        return area[0] if area else None
    if specific and unclassified:
        if area:
            return f"{area[0]} (mixed; includes unclassified)"
        return "Mixed (1 subsystem + unclassified)"
    return area[0] if area and not owners else None


def subsystem_for_target(conn: sqlite3.Connection, t: Target) -> sqlite3.Row | None:
    if t.kind == "dir":
        # A plurality is not ownership.  Singular operations must not silently
        # turn ``drivers`` into whichever leaf subsystem happens to have the
        # largest file count in this release.
        return uniform_directory_subsystem(conn, t.id)
    return best_subsystem(conn, "file", t.file_id or t.id)


def build_scope(conn: sqlite3.Connection, t: Target, level: str) -> Scope:
    """Turn (target, level) into SQL selecting the dirs/files that form the scope."""
    if level == "auto":
        level = "file" if t.kind == "symbol" else "dir"

    if level == "tree":
        return Scope("the whole kernel tree", "SELECT * FROM dirs WHERE path != ''",
                     (), "SELECT * FROM files", (), "1=1", ())

    if level == "subsystem":
        sub = subsystem_for_target(conn, t)
        if sub is None:
            file_id = t.file_id or (t.id if t.kind == "file" else None)
            coowners = (file_primary_subsystems(conn, file_id)
                        if file_id is not None else [])
            if t.kind == "dir" and directory_primary_subsystems(conn, t.id):
                label = "directory has mixed subsystem ownership"
            elif len(coowners) > 1:
                label = "file has co-primary subsystem ownership"
            else:
                label = "no subsystem found"
            return Scope(label, None, (), None, (), None, ())
        sid = sub["id"]
        return Scope(
            f"subsystem {sub['name']}",
            "SELECT d.* FROM dirs d JOIN dir_subsys p ON p.dir_id=d.id"
            " WHERE p.subsystem_id=?", (sid,),
            "SELECT f.* FROM files f JOIN path_subsys p ON p.ref_kind='file'"
            " AND p.ref_id=f.id WHERE p.subsystem_id=?", (sid,),
            "s.file_id IN (SELECT ref_id FROM path_subsys WHERE ref_kind='file'"
            " AND subsystem_id=?)", (sid,))

    if level == "file":
        fid = t.file_id if t.kind == "symbol" else (
            t.file_id if t.kind == "file" else None)
        if fid is None:
            return Scope("no containing file", None, (), None, (), None, ())
        path = t.path
        return Scope(f"file {path}", None, (),
                     "SELECT * FROM files WHERE id = ?", (fid,),
                     "s.file_id = ?", (fid,))

    if level == "subtree":
        base = t.path if t.kind == "dir" else parent_path(t.path)
        like = like_under(base)
        return Scope(
            f"everything under {base or 'the kernel root'}",
            "SELECT * FROM dirs WHERE path = ? OR path LIKE ? ESCAPE '\\'",
            (base, like),
            "SELECT * FROM files WHERE path LIKE ? ESCAPE '\\'", (like,),
            "s.file_id IN (SELECT id FROM files WHERE path LIKE ? ESCAPE '\\')",
            (like,))

    # level == "dir": the container directory.
    if t.kind == "dir":
        row = conn.execute("SELECT * FROM dirs WHERE id = ?", (t.id,)).fetchone()
        container_id = row["parent_id"] if row and row["parent_id"] else (
            row["id"] if row else None)
        container_path = ""
        if row is not None and row["parent_id"]:
            prow = conn.execute("SELECT path FROM dirs WHERE id = ?",
                                (row["parent_id"],)).fetchone()
            container_path = prow["path"] if prow else ""
    else:
        container_id = t.dir_id
        prow = conn.execute("SELECT path FROM dirs WHERE id = ?",
                            (container_id,)).fetchone()
        container_path = prow["path"] if prow else ""

    if container_id is None:
        return Scope("no container", None, (), None, (), None, ())
    return Scope(
        f"directory {container_path or 'the kernel root'}/",
        "SELECT * FROM dirs WHERE parent_id = ?", (container_id,),
        "SELECT * FROM files WHERE dir_id = ?", (container_id,),
        "s.file_id IN (SELECT id FROM files WHERE dir_id = ?)", (container_id,))


def default_kinds(t: Target) -> tuple[str, ...]:
    """Mirror the target's own type, which is what 'same level' usually means."""
    if t.kind == "dir":
        return ("dir",)
    if t.kind == "file":
        return ("file",)
    if t.symbol_kind in ("function", "syscall"):
        return ("function", "syscall")
    return (t.symbol_kind or "function",)


def entry_for_target(conn: sqlite3.Connection, t: Target) -> Entry | None:
    """Materialize a target as an Entry without relying on a bounded listing."""
    if t.kind == "dir":
        r = conn.execute("SELECT * FROM dirs WHERE id = ?", (t.id,)).fetchone()
        return (Entry(kind="dir", name=r["name"], path=r["path"],
                      n_files=r["n_files"], n_subdirs=r["n_subdirs"], ref_id=r["id"])
                if r else None)
    if t.kind == "file":
        r = conn.execute("SELECT * FROM files WHERE id = ?", (t.id,)).fetchone()
        return (Entry(kind="file", name=r["name"], path=r["path"], size=r["size"],
                      lines=r["lines"], n_symbols=r["n_symbols"], ref_id=r["id"])
                if r else None)
    r = conn.execute(_SYM_SELECT + " WHERE s.id = ?", (t.id,)).fetchone()
    if not r:
        return None
    return Entry(kind=r["kind"], name=r["name"], path=r["path"],
                 line=r["start_line"], end_line=r["end_line"],
                 signature=r["signature"], is_static=bool(r["is_static"]),
                 is_inline=bool(r["is_inline"]),
                 is_exported=bool(r["is_exported"]), ref_id=r["id"])


def _enable_regexp(conn: sqlite3.Connection) -> None:
    """Install a Python REGEXP so --grep can filter inside SQLite (and LIMIT)."""

    def regexp(pattern: str, value: str | None) -> bool:
        if not pattern or value is None:
            return False
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False

    conn.create_function("REGEXP", 2, regexp, deterministic=True)


def _bounded(conn: sqlite3.Connection, sql: str, params: tuple, *,
             order: str, limit: int, grep: str | None) -> tuple[str, tuple]:
    """Wrap a SELECT so grep and LIMIT run in SQLite instead of in Python."""
    extra: list = []
    tail = ""
    if grep:
        _enable_regexp(conn)
        tail += " WHERE name REGEXP ?"
        extra.append(grep)
    if limit and limit > 0:
        tail += f" ORDER BY {order} LIMIT {int(limit)}"
    if not tail:
        return sql, params
    return f"SELECT * FROM ({sql}){tail}", tuple(params) + tuple(extra)


_DIR_ORDER = {
    "name": "LOWER(name), path",
    "path": "path",
    "kind": "LOWER(name), path",
    "line": "path",
    "size": "n_files DESC, LOWER(name), path, id",
    "lines": "n_files DESC, LOWER(name), path, id",
}
_FILE_ORDER = {
    "name": "LOWER(name), path",
    "path": "path",
    "kind": "LOWER(name), path",
    "line": "path",
    "size": "size DESC, LOWER(name), path, id",
    "lines": "lines DESC, LOWER(name), path, id",
}
_SYM_ORDER = {
    "name": "LOWER(name), path, start_line, kind, id",
    "path": "path, start_line, kind, LOWER(name), id",
    "kind": "kind, LOWER(name), path, start_line, id",
    "line": "path, start_line, kind, LOWER(name), id",
    "size": "LOWER(name), path, start_line, kind, id",
    "lines": ("(end_line - start_line) DESC, LOWER(name), path, "
              "start_line, kind, id"),
}
_SEARCH_SYM_ORDER = {
    "name": "LOWER(s.name), f.path, s.start_line, s.kind, s.id",
    "path": "f.path, s.start_line, s.kind, LOWER(s.name), s.id",
    "kind": "s.kind, LOWER(s.name), f.path, s.start_line, s.id",
    "line": "f.path, s.start_line, s.kind, LOWER(s.name), s.id",
    "size": "LOWER(s.name), f.path, s.start_line, s.kind, s.id",
    "lines": ("(s.end_line - s.start_line) DESC, LOWER(s.name), f.path, "
              "s.start_line, s.kind, s.id"),
}


def _entry_sort_key(e: Entry, sort: str):
    keys = {
        "name": lambda x: (x.name.lower(), x.path, x.line or 0, x.kind,
                           x.ref_id or 0),
        "path": lambda x: (x.path, x.line or 0, x.kind, x.name.lower(),
                           x.ref_id or 0),
        "kind": lambda x: (x.kind, x.name.lower(), x.path, x.line or 0,
                           x.ref_id or 0),
        "line": lambda x: (x.path, x.line or 0, x.kind, x.name.lower(),
                           x.ref_id or 0),
        # Directories do not have byte/line counts.  The SQL pre-order has
        # historically treated their immediate file count as their useful
        # notion of size, so preserve that ordering after the rows are merged.
        "size": lambda x: (-(x.n_files if x.kind == "dir" else (x.size or 0)),
                            x.name.lower(), x.path, x.line or 0, x.kind,
                            x.ref_id or 0),
        "lines": lambda x: (-(x.n_files if x.kind == "dir" else
                               (x.lines or x.span or 0)),
                             x.name.lower(), x.path, x.line or 0, x.kind,
                             x.ref_id or 0),
    }
    return keys.get(sort, keys["name"])(e)


def sort_entries(entries: list[Entry], sort: str = "name") -> list[Entry]:
    """Sort entries with the same semantics used by :func:`collect`."""
    entries.sort(key=lambda e: _entry_sort_key(e, sort))
    return entries


def collect(conn: sqlite3.Connection, scope: Scope, kinds, limit: int = 0,
            grep: str | None = None, exported_only: bool = False,
            static: str = "any", with_subsystem: bool = False,
            sort: str = "name") -> list[Entry]:
    kinds = tuple(kinds)
    entries: list[Entry] = []

    if "dir" in kinds and scope.dir_sql:
        sql, params = _bounded(
            conn, scope.dir_sql, scope.dir_params,
            order=_DIR_ORDER.get(sort, _DIR_ORDER["name"]),
            limit=limit, grep=grep)
        for r in conn.execute(sql, params):
            if not r["path"]:
                continue
            entries.append(Entry(kind="dir", name=r["name"], path=r["path"],
                                 n_files=r["n_files"], n_subdirs=r["n_subdirs"],
                                 ref_id=r["id"]))

    if "file" in kinds and scope.file_sql:
        sql, params = _bounded(
            conn, scope.file_sql, scope.file_params,
            order=_FILE_ORDER.get(sort, _FILE_ORDER["name"]),
            limit=limit, grep=grep)
        for r in conn.execute(sql, params):
            entries.append(Entry(kind="file", name=r["name"], path=r["path"],
                                 size=r["size"], lines=r["lines"],
                                 n_symbols=r["n_symbols"], ref_id=r["id"]))

    sym_kinds = [k for k in kinds if k in SYMBOL_KINDS]
    if sym_kinds and scope.sym_where:
        placeholders = ",".join("?" * len(sym_kinds))
        sql = (_SYM_SELECT + f" WHERE {scope.sym_where} AND s.kind IN ({placeholders})")
        params = tuple(scope.sym_params) + tuple(sym_kinds)
        if exported_only:
            sql += " AND s.is_exported = 1"
        if static == "only":
            sql += " AND s.is_static = 1"
        elif static == "exclude":
            sql += " AND s.is_static = 0"
        sql, params = _bounded(
            conn, sql, params,
            order=_SYM_ORDER.get(sort, _SYM_ORDER["name"]),
            limit=limit, grep=grep)
        for r in conn.execute(sql, params):
            entries.append(Entry(
                kind=r["kind"], name=r["name"], path=r["path"],
                line=r["start_line"], end_line=r["end_line"],
                signature=r["signature"], is_static=bool(r["is_static"]),
                is_inline=bool(r["is_inline"]), is_exported=bool(r["is_exported"]),
                ref_id=r["id"]))

    sort_entries(entries, sort)
    if limit and limit > 0:
        entries = entries[:limit]

    if with_subsystem:
        annotate_subsystems(conn, entries)
    return entries


def annotate_subsystems(conn: sqlite3.Connection, entries: list[Entry]) -> None:
    cache: dict[str, str | None] = {}
    for e in entries:
        key = e.path
        if key not in cache:
            ref_kind = "dir" if e.kind == "dir" else "file"
            table = "dirs" if ref_kind == "dir" else "files"
            row = conn.execute(f"SELECT id FROM {table} WHERE path = ?",
                               (key,)).fetchone()
            if row and ref_kind == "dir":
                name = directory_subsystem_label(conn, row["id"], key)
            else:
                name = (file_subsystem_label(conn, row["id"], key)
                        if row else None)
            cache[key] = name
        e.subsystem = cache[key]


def search(conn: sqlite3.Connection, pattern: str, kinds=(), mode: str = "substring",
           limit: int = 50, exported_only: bool = False,
           with_subsystem: bool = True, grep: str | None = None,
           static: str = "any", sort: str | None = None) -> list[Entry]:
    kinds = tuple(k for k in kinds if k in SYMBOL_KINDS) or SYMBOL_KINDS
    placeholders = ",".join("?" * len(kinds))
    sql = _SYM_SELECT + f" WHERE s.kind IN ({placeholders})"
    params: list = list(kinds)

    if mode == "exact":
        sql += " AND s.name = ?"
        params.append(pattern)
    elif mode == "glob":
        sql += " AND s.name GLOB ?"
        params.append(pattern)
    elif mode == "prefix":
        sql += " AND s.name LIKE ? ESCAPE '\\'"
        params.append(pattern.replace("%", "\\%").replace("_", "\\_") + "%")
    else:
        sql += " AND s.name LIKE ? ESCAPE '\\'"
        params.append("%" + pattern.replace("%", "\\%").replace("_", "\\_") + "%")
    if exported_only:
        sql += " AND s.is_exported = 1"
    if static == "only":
        sql += " AND s.is_static = 1"
    elif static == "exclude":
        sql += " AND s.is_static = 0"
    if grep:
        _enable_regexp(conn)
        sql += " AND s.name REGEXP ?"
        params.append(grep)
    sql += (f" ORDER BY {_SEARCH_SYM_ORDER.get(sort, _SEARCH_SYM_ORDER['name'])}"
            if sort else " ORDER BY LENGTH(s.name), s.name, f.path, s.start_line")
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))

    entries = [
        Entry(kind=r["kind"], name=r["name"], path=r["path"], line=r["start_line"],
              end_line=r["end_line"], signature=r["signature"],
              is_static=bool(r["is_static"]), is_inline=bool(r["is_inline"]),
              is_exported=bool(r["is_exported"]), ref_id=r["id"])
        for r in conn.execute(sql, params)
    ]
    if with_subsystem:
        annotate_subsystems(conn, entries)
    return entries


def ancestry(conn: sqlite3.Connection, path: str) -> list[tuple[str, str | None]]:
    """Each parent directory with a uniform owner or an honest mixed label."""
    out: list[tuple[str, str | None]] = []
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        row = conn.execute("SELECT id FROM dirs WHERE path = ?", (p,)).fetchone()
        if row is None:
            continue
        label = directory_subsystem_label(conn, row["id"], p)
        out.append((p, label))
    return out


def subsystem_by_name(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    exact = conn.execute("SELECT * FROM subsystems WHERE name = ?", (name,)).fetchall()
    if exact:
        return exact
    exact = conn.execute(
        "SELECT * FROM subsystems WHERE name = ? COLLATE NOCASE ORDER BY name",
        (name,)).fetchall()
    if exact:
        return exact
    return conn.execute(
        "SELECT * FROM subsystems WHERE name LIKE ? ESCAPE '\\'"
        " ORDER BY n_files DESC LIMIT 50",
        (f"%{like_escape(name)}%",)).fetchall()


def subsystem_json_fields(row: sqlite3.Row) -> dict:
    out = {}
    for k in ("maintainers", "reviewers", "lists", "trees", "websites",
              "patchwork", "bugs", "chats", "profiles", "keywords"):
        try:
            out[k] = json.loads(row[k] or "[]")
        except (IndexError, json.JSONDecodeError, TypeError):
            out[k] = []
    return out


def callee_entries(conn: sqlite3.Connection, symbol_id: int,
                   limit: int = 200) -> list[Entry]:
    """Direct calls made by one callable, with conservative identity status."""
    sql = """
        SELECT c.callee, c.resolution, c.direct_count, c.indirect_count,
               c.macro_count, s.id, s.kind, s.name, s.start_line,
               s.end_line, s.signature, s.is_static, s.is_inline,
               s.is_exported, f.path
        FROM calls c
        LEFT JOIN symbols s ON s.id = c.callee_id
        LEFT JOIN files f ON f.id = s.file_id
        WHERE c.caller_id = ?
        ORDER BY LOWER(c.callee), c.callee
    """
    params: list = [symbol_id]
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    out: list[Entry] = []
    for row in conn.execute(sql, params):
        if row["id"] is None:
            out.append(Entry(kind="?", name=row["callee"], path="-",
                             resolution=row["resolution"],
                             direct_count=row["direct_count"],
                             indirect_count=row["indirect_count"],
                             macro_count=row["macro_count"]))
        else:
            out.append(Entry(
                kind=row["kind"], name=row["name"], path=row["path"],
                line=row["start_line"], end_line=row["end_line"],
                signature=row["signature"], is_static=bool(row["is_static"]),
                is_inline=bool(row["is_inline"]),
                is_exported=bool(row["is_exported"]), ref_id=row["id"],
                resolution=row["resolution"],
                direct_count=row["direct_count"],
                indirect_count=row["indirect_count"],
                macro_count=row["macro_count"],
            ))
    return out


def callees(conn: sqlite3.Connection, symbol_id: int, limit: int = 200) -> list[str]:
    """Compatibility view of :func:`callee_entries` containing raw names."""
    return [entry.name for entry in callee_entries(conn, symbol_id, limit)]


def callers(conn: sqlite3.Connection, symbol_id: int | str,
            limit: int = 200) -> list[Entry]:
    """Callers of one concrete symbol identity.

    Passing a name is retained for the small library API used by older code;
    it first resolves that name using the normal target ranking.  Interactive
    commands always pass an explicit symbol id, so colliding static functions
    cannot be mixed together.
    """
    if isinstance(symbol_id, str):
        resolved = resolve_symbol(conn, symbol_id)
        if resolved.target is None:
            return []
        callable_alternatives = [
            candidate for candidate in resolved.candidates
            if candidate.symbol_kind in ("function", "syscall")
        ]
        if callable_alternatives:
            raise ValueError(
                f"{len(callable_alternatives) + 1} callable definitions are "
                f"named {symbol_id!r}; pass a concrete symbol id")
        symbol_id = resolved.target.id
    sql = """
        SELECT s.id, s.file_id, s.name, s.kind, s.start_line, s.end_line,
               s.signature, s.is_static, s.is_inline, s.is_exported,
               f.path, f.dir_id, c.resolution,
               SUM(c.direct_count) AS direct_count,
               SUM(c.indirect_count) AS indirect_count,
               SUM(c.macro_count) AS macro_count
        FROM calls c
        JOIN symbols s ON s.id = c.caller_id
        JOIN files f ON f.id = s.file_id
        WHERE c.callee_id = ?
        GROUP BY s.id, c.resolution
        ORDER BY LOWER(s.name), f.path, s.start_line
    """
    params: list = [symbol_id]
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [Entry(kind=r["kind"], name=r["name"], path=r["path"], line=r["start_line"],
                  end_line=r["end_line"], signature=r["signature"],
                  is_static=bool(r["is_static"]), is_inline=bool(r["is_inline"]),
                  is_exported=bool(r["is_exported"]), ref_id=r["id"],
                  resolution=r["resolution"],
                  direct_count=r["direct_count"],
                  indirect_count=r["indirect_count"],
                  macro_count=r["macro_count"]) for r in rows]


def documentation_for(conn: sqlite3.Connection, t: Target, limit: int = 30) -> list[Entry]:
    """Rank Documentation files by direct, ownership, and lexical evidence.

    Limits are applied only after all evidence has been combined.  This keeps
    a broad area or incidental secondary MAINTAINERS match from filling the
    result before a specific file's documentation is considered.
    """
    cap = limit if limit and limit > 0 else 10**9
    docs = conn.execute(
        "SELECT id,path,name,size,lines FROM files"
        " WHERE path LIKE 'Documentation/%' ORDER BY path"
    ).fetchall()
    if not docs:
        return []

    stop = {"api", "core", "doc", "docs", "driver", "drivers", "file",
            "files", "kernel", "linux", "main", "subsystem", "system"}

    def tokens(value: str) -> set[str]:
        stem = value.rsplit(".", 1)[0]
        return {word for word in re.findall(r"[a-z0-9]+", stem.lower())
                if len(word) >= 3 and word not in stop}

    def lexical(source: set[str], candidate: set[str]) -> int:
        score = 0
        for left in source:
            best = 0
            for right in candidate:
                if left == right:
                    best = max(best, 30 + min(len(left), 12))
                    continue
                common = 0
                for a, b in zip(left, right):
                    if a != b:
                        break
                    common += 1
                if common >= 4:
                    best = max(best, 5 + min(common, 12))
            score += best
        return score

    path = t.path or ""
    parts = [part for part in path.split("/") if part]
    file_id = t.file_id or (t.id if t.kind == "file" else None)
    identity_terms = tokens(parts[-1]) if parts else set()
    path_terms = tokens("/".join(parts[-3:]))
    semantic_terms = set(path_terms)
    if t.kind == "symbol":
        semantic_terms.update(tokens(t.name))
    elif file_id is not None:
        # Macro-heavy generated headers can contain tens of thousands of names.
        # A bounded set of declaration identities retains useful semantic hints
        # without turning one interactive docs query into hundreds of millions
        # of token comparisons.  The target path and owner evidence remain
        # unbounded and carry the strongest tiers below.
        for row in conn.execute(
                "SELECT name FROM symbols WHERE file_id=?"
                " AND kind IN ('function','syscall','struct','union','enum',"
                "              'typedef','variable')"
                " GROUP BY name"
                " ORDER BY MIN(CASE kind WHEN 'function' THEN 0"
                "  WHEN 'syscall' THEN 0 WHEN 'struct' THEN 1"
                "  WHEN 'union' THEN 1 WHEN 'enum' THEN 1"
                "  WHEN 'typedef' THEN 2 ELSE 3 END),"
                " MAX(is_exported) DESC,LENGTH(name) DESC,name LIMIT 192",
                (file_id,)):
            semantic_terms.update(tokens(row["name"]))

    if t.kind == "dir":
        owners = directory_primary_subsystems(conn, t.id)
        specific_owners = [row for row in owners
                           if row["name"] not in CATCH_ALL]
        # Hundreds of leaf owners are composition, not evidence for one
        # aggregate directory.  Let its coherent Documentation area win.
        if len(specific_owners) > 12:
            specific_owners = []
    else:
        specific_owners = [row for row in file_primary_subsystems(conn, file_id)
                           if row["name"] not in CATCH_ALL]
    for owner in specific_owners:
        semantic_terms.update(tokens(owner["name"]))

    owner_rank = {row["id"]: rank for rank, row in enumerate(specific_owners)}
    owner_paths: dict[str, int] = {}
    if owner_rank:
        placeholders = ",".join("?" for _ in owner_rank)
        for row in conn.execute(
            "SELECT f.path,p.subsystem_id FROM files f"
            " JOIN path_subsys p ON p.ref_kind='file' AND p.ref_id=f.id"
            f" WHERE f.path LIKE 'Documentation/%'"
            f" AND p.subsystem_id IN ({placeholders})",
            tuple(owner_rank),
        ):
            rank = owner_rank[row["subsystem_id"]]
            owner_paths[row["path"]] = min(
                owner_paths.get(row["path"], rank), rank)

    direct_exact = path if path.startswith("Documentation/") \
        and t.kind != "dir" else None
    direct_prefix: str | None = None
    if t.kind == "dir" and (not path or path == "Documentation"):
        direct_prefix = "Documentation/"
    elif t.kind == "dir" and path.startswith("Documentation/"):
        direct_prefix = path.rstrip("/") + "/"
    elif path.startswith("Documentation/"):
        direct_prefix = path.rpartition("/")[0] + "/"

    aliases = {
        "arch": "arch", "block": "block", "drivers": "driver-api",
        "fs": "filesystems", "include": "core-api", "kernel": "core-api",
        "net": "networking", "security": "security", "sound": "sound",
        "tools": "tools", "virt": "virt",
    }
    area_roots: list[str] = []
    if t.kind == "dir" and parts and parts[0] != "Documentation":
        last = aliases.get(parts[-1], parts[-1])
        top = aliases.get(parts[0], parts[0])
        candidates = [last]
        if len(parts) > 1:
            candidates.append(f"{top}/{parts[-1]}")
        candidates.append(top)
        for area in candidates:
            area = area.strip("/")
            prefix = f"Documentation/{area}/"
            standalone = re.compile(
                rf"^Documentation/{re.escape(area)}\.[^/]+$")
            if area not in area_roots and any(
                    row["path"].startswith(prefix)
                    or standalone.fullmatch(row["path"])
                    for row in docs):
                area_roots.append(area)

    ranked: list[tuple[tuple, sqlite3.Row]] = []
    for row in docs:
        doc_path = row["path"]
        doc_terms = tokens(doc_path.removeprefix("Documentation/"))
        path_score = lexical(identity_terms, doc_terms)
        semantic_score = lexical(semantic_terms, doc_terms)
        owner = owner_paths.get(doc_path)
        area = next((rank for rank, root in enumerate(area_roots)
                     if doc_path.startswith(f"Documentation/{root}/")
                     or re.fullmatch(
                         rf"Documentation/{re.escape(root)}\.[^/]+",
                         doc_path)), None)
        exact = direct_exact == doc_path
        contained = direct_prefix is not None and doc_path.startswith(direct_prefix)

        if exact:
            tier = 0
        elif contained:
            tier = 1
        elif owner is not None and semantic_score:
            tier = 2
        elif path_score:
            tier = 3
        elif owner is not None:
            tier = 4
        elif area is not None:
            tier = 5
        else:
            continue
        stem = row["name"].rsplit(".", 1)[0].lower()
        overview = 0 if stem in {"index", "readme", "overview"} else 1
        depth = doc_path.count("/")
        ranked.append(((tier, -semantic_score, owner if owner is not None else 10**6,
                        area if area is not None else 10**6,
                        overview, depth, doc_path), row))

    ranked.sort(key=lambda item: item[0])
    return [Entry(kind="file", name=row["name"], path=row["path"],
                  size=row["size"], lines=row["lines"])
            for _, row in ranked[:cap]]


def describe_area(path: str) -> tuple[str, str] | None:
    return maintainers.top_level_area(path)
