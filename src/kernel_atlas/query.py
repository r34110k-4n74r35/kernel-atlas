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
from dataclasses import dataclass, field

from . import maintainers

SYMBOL_KINDS = ("function", "syscall", "struct", "union", "enum", "typedef",
                "macro", "variable", "prototype")
PATH_KINDS = ("dir", "file")
ALL_KINDS = PATH_KINDS + SYMBOL_KINDS

LEVELS = ("auto", "file", "dir", "subtree", "subsystem", "tree")


@dataclass
class Entry:
    kind: str
    name: str
    path: str
    line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    size: int | None = None
    lines: int | None = None
    n_files: int | None = None
    n_subdirs: int | None = None
    n_symbols: int | None = None
    is_static: bool = False
    is_inline: bool = False
    is_exported: bool = False
    subsystem: str | None = None
    is_target: bool = False

    @property
    def span(self) -> int | None:
        if self.line and self.end_line:
            return self.end_line - self.line + 1
        return None


@dataclass
class Target:
    kind: str                      # 'dir' | 'file' | 'symbol'
    id: int
    path: str                      # path of the dir, or of the defining file
    name: str
    symbol_kind: str | None = None
    line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    dir_id: int | None = None
    file_id: int | None = None
    is_static: bool = False
    is_exported: bool = False

    @property
    def display(self) -> str:
        if self.kind == "symbol":
            return f"{self.path}:{self.name}"
        return self.path or "<kernel root>"


@dataclass
class Resolution:
    target: Target | None
    candidates: list[Target] = field(default_factory=list)
    note: str = ""


@dataclass
class Scope:
    label: str
    dir_sql: str | None
    dir_params: tuple
    file_sql: str | None
    file_params: tuple
    sym_where: str | None
    sym_params: tuple


def _norm(spec: str) -> str:
    spec = (spec or "").strip()
    if spec in (".", "/", "./"):
        return ""
    spec = spec.lstrip("/")
    if spec.startswith("./"):
        spec = spec[2:]
    return spec.rstrip("/")


def _sym_target(conn: sqlite3.Connection, row: sqlite3.Row) -> Target:
    return Target(
        kind="symbol", id=row["id"], path=row["path"], name=row["name"],
        symbol_kind=row["kind"], line=row["start_line"], end_line=row["end_line"],
        signature=row["signature"], file_id=row["file_id"], dir_id=row["dir_id"],
        is_static=bool(row["is_static"]), is_exported=bool(row["is_exported"]),
    )


_SYM_SELECT = """
SELECT s.id, s.file_id, s.name, s.kind, s.start_line, s.end_line, s.signature,
       s.is_static, s.is_inline, s.is_exported, f.path, f.dir_id
FROM symbols s JOIN files f ON f.id = s.file_id
"""


def _rank_candidate(t: Target) -> tuple:
    """Prefer real definitions over prototypes, and exported over file-local."""
    kind_rank = {"function": 0, "syscall": 0, "struct": 1, "typedef": 1, "enum": 1,
                 "union": 1, "macro": 2, "variable": 3, "prototype": 4}
    return (kind_rank.get(t.symbol_kind or "", 5), t.is_static, len(t.path))


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
        frow = conn.execute("SELECT * FROM files WHERE path = ?", (head_n,)).fetchone()
        if frow is None and head_n:
            frow = conn.execute("SELECT * FROM files WHERE name = ?",
                                (head_n.rsplit("/", 1)[-1],)).fetchone()
        if frow is not None:
            if tail.isdigit():
                line = int(tail)
                row = conn.execute(
                    _SYM_SELECT + " WHERE s.file_id = ? AND s.start_line <= ?"
                    " AND s.end_line >= ? ORDER BY (s.end_line - s.start_line) LIMIT 1",
                    (frow["id"], line, line)).fetchone()
                if row:
                    return Resolution(_sym_target(conn, row),
                                      note=f"line {line} falls inside this symbol")
                return Resolution(
                    Target(kind="file", id=frow["id"], path=frow["path"],
                           name=frow["name"], dir_id=frow["dir_id"],
                           file_id=frow["id"]),
                    note=f"no symbol spans line {line}")
            rows = conn.execute(_SYM_SELECT + " WHERE s.file_id = ? AND s.name = ?",
                                (frow["id"], tail)).fetchall()
            if rows:
                cands = sorted((_sym_target(conn, r) for r in rows),
                               key=_rank_candidate)
                return Resolution(cands[0], cands[1:] if len(cands) > 1 else [])

    row = conn.execute("SELECT * FROM dirs WHERE path = ?", (path,)).fetchone()
    if row:
        return Resolution(Target(kind="dir", id=row["id"], path=row["path"],
                                 name=row["name"], dir_id=row["id"]))

    row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
    if row:
        return Resolution(Target(kind="file", id=row["id"], path=row["path"],
                                 name=row["name"], dir_id=row["dir_id"],
                                 file_id=row["id"]))

    # Bare symbol name.
    rows = conn.execute(_SYM_SELECT + " WHERE s.name = ? LIMIT 200", (raw,)).fetchall()
    if rows:
        cands = sorted((_sym_target(conn, r) for r in rows), key=_rank_candidate)
        note = ""
        if len(cands) > 1:
            note = f"{len(cands)} symbols named {raw!r}; showing the most likely definition"
        return Resolution(cands[0], cands[1:], note)

    # Bare file name, e.g. 'inode.c'.
    rows = conn.execute("SELECT * FROM files WHERE name = ? LIMIT 200",
                        (raw,)).fetchall()
    if rows:
        cands = [Target(kind="file", id=r["id"], path=r["path"], name=r["name"],
                        dir_id=r["dir_id"], file_id=r["id"]) for r in rows]
        cands.sort(key=lambda t: len(t.path))
        note = f"{len(cands)} files named {raw!r}" if len(cands) > 1 else ""
        return Resolution(cands[0], cands[1:], note)

    # Bare directory name, e.g. 'ext4'.
    rows = conn.execute("SELECT * FROM dirs WHERE name = ? LIMIT 200",
                        (raw,)).fetchall()
    if rows:
        cands = [Target(kind="dir", id=r["id"], path=r["path"], name=r["name"],
                        dir_id=r["id"]) for r in rows]
        cands.sort(key=lambda t: len(t.path))
        note = f"{len(cands)} directories named {raw!r}" if len(cands) > 1 else ""
        return Resolution(cands[0], cands[1:], note)

    return Resolution(None, note=f"nothing in the index matches {raw!r}")


def primary_subsystem(conn: sqlite3.Connection, ref_kind: str,
                      ref_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.* FROM path_subsys p JOIN subsystems s ON s.id = p.subsystem_id"
        " WHERE p.ref_kind = ? AND p.ref_id = ? ORDER BY p.rank LIMIT 1",
        (ref_kind, ref_id)).fetchone()


def all_subsystems(conn: sqlite3.Connection, ref_kind: str,
                   ref_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.*, p.score, p.rank FROM path_subsys p"
        " JOIN subsystems s ON s.id = p.subsystem_id"
        " WHERE p.ref_kind = ? AND p.ref_id = ? ORDER BY p.rank",
        (ref_kind, ref_id)).fetchall()


# 'THE REST' carries `F: *` and `F: */`, so it claims every path in the tree.
# It is a real answer, but never the interesting one when anything else matches.
CATCH_ALL = {"THE REST"}


def best_subsystem(conn: sqlite3.Connection, ref_kind: str,
                   ref_id: int) -> sqlite3.Row | None:
    rows = all_subsystems(conn, ref_kind, ref_id)
    for r in rows:
        if r["name"] not in CATCH_ALL:
            return r
    return rows[0] if rows else None


def subsystem_for_target(conn: sqlite3.Connection, t: Target) -> sqlite3.Row | None:
    if t.kind == "dir":
        return best_subsystem(conn, "dir", t.id)
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
            return Scope("no subsystem found", None, (), None, (), None, ())
        sid = sub["id"]
        return Scope(
            f"subsystem {sub['name']}",
            "SELECT d.* FROM dirs d JOIN path_subsys p ON p.ref_kind='dir'"
            " AND p.ref_id=d.id WHERE p.subsystem_id=?", (sid,),
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
        base = t.path if t.kind == "dir" else t.path.rsplit("/", 1)[0]
        like = f"{base}/%" if base else "%"
        return Scope(
            f"everything under {base or 'the kernel root'}",
            "SELECT * FROM dirs WHERE path = ? OR path LIKE ?", (base, like),
            "SELECT * FROM files WHERE path LIKE ?", (like,),
            "s.file_id IN (SELECT id FROM files WHERE path LIKE ?)", (like,))

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


def collect(conn: sqlite3.Connection, scope: Scope, kinds, limit: int = 0,
            grep: str | None = None, exported_only: bool = False,
            static: str = "any", with_subsystem: bool = False,
            sort: str = "name") -> list[Entry]:
    kinds = tuple(kinds)
    entries: list[Entry] = []
    rx = re.compile(grep, re.IGNORECASE) if grep else None

    if "dir" in kinds and scope.dir_sql:
        for r in conn.execute(scope.dir_sql, scope.dir_params):
            if not r["path"]:
                continue
            if rx and not rx.search(r["name"]):
                continue
            entries.append(Entry(kind="dir", name=r["name"], path=r["path"],
                                 n_files=r["n_files"], n_subdirs=r["n_subdirs"]))

    if "file" in kinds and scope.file_sql:
        for r in conn.execute(scope.file_sql, scope.file_params):
            if rx and not rx.search(r["name"]):
                continue
            entries.append(Entry(kind="file", name=r["name"], path=r["path"],
                                 size=r["size"], lines=r["lines"],
                                 n_symbols=r["n_symbols"]))

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
        for r in conn.execute(sql, params):
            if rx and not rx.search(r["name"]):
                continue
            entries.append(Entry(
                kind=r["kind"], name=r["name"], path=r["path"],
                line=r["start_line"], end_line=r["end_line"],
                signature=r["signature"], is_static=bool(r["is_static"]),
                is_inline=bool(r["is_inline"]), is_exported=bool(r["is_exported"])))

    keys = {
        "name": lambda e: (e.name.lower(), e.path),
        "path": lambda e: (e.path, e.line or 0),
        "kind": lambda e: (e.kind, e.name.lower()),
        "line": lambda e: (e.path, e.line or 0),
        "size": lambda e: (-(e.size or 0), e.name.lower()),
        "lines": lambda e: (-(e.lines or e.span or 0), e.name.lower()),
    }
    entries.sort(key=keys.get(sort, keys["name"]))
    if limit:
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
            sub = best_subsystem(conn, ref_kind, row["id"]) if row else None
            cache[key] = sub["name"] if sub else None
        e.subsystem = cache[key]


def search(conn: sqlite3.Connection, pattern: str, kinds=(), mode: str = "substring",
           limit: int = 50, exported_only: bool = False,
           with_subsystem: bool = True) -> list[Entry]:
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
    sql += " ORDER BY LENGTH(s.name), s.name LIMIT ?"
    params.append(max(limit, 1))

    entries = [
        Entry(kind=r["kind"], name=r["name"], path=r["path"], line=r["start_line"],
              end_line=r["end_line"], signature=r["signature"],
              is_static=bool(r["is_static"]), is_inline=bool(r["is_inline"]),
              is_exported=bool(r["is_exported"]))
        for r in conn.execute(sql, params)
    ]
    if with_subsystem:
        annotate_subsystems(conn, entries)
    return entries


def ancestry(conn: sqlite3.Connection, path: str) -> list[tuple[str, str | None]]:
    """Each parent directory of `path` with the subsystem that claims it.

    Top-level directories are usually claimed only by the catch-all section, so
    fall back to the friendlier area name there.
    """
    out: list[tuple[str, str | None]] = []
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        row = conn.execute("SELECT id FROM dirs WHERE path = ?", (p,)).fetchone()
        if row is None:
            continue
        sub = best_subsystem(conn, "dir", row["id"])
        label = sub["name"] if sub and sub["name"] not in CATCH_ALL else None
        if label is None:
            area = maintainers.top_level_area(p)
            label = area[0] if area else (sub["name"] if sub else None)
        out.append((p, label))
    return out


def subsystem_by_name(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    exact = conn.execute("SELECT * FROM subsystems WHERE name = ?", (name,)).fetchall()
    if exact:
        return exact
    return conn.execute(
        "SELECT * FROM subsystems WHERE name LIKE ? ORDER BY n_files DESC LIMIT 50",
        (f"%{name}%",)).fetchall()


def subsystem_json_fields(row: sqlite3.Row) -> dict:
    out = {}
    for k in ("maintainers", "reviewers", "lists", "trees"):
        try:
            out[k] = json.loads(row[k] or "[]")
        except (json.JSONDecodeError, TypeError):
            out[k] = []
    return out


def callees(conn: sqlite3.Connection, symbol_id: int, limit: int = 200) -> list[str]:
    return [r["callee"] for r in conn.execute(
        "SELECT DISTINCT callee FROM calls WHERE caller_id = ? ORDER BY callee LIMIT ?",
        (symbol_id, limit))]


def callers(conn: sqlite3.Connection, name: str, limit: int = 200) -> list[Entry]:
    rows = conn.execute(
        _SYM_SELECT.replace("FROM symbols s", "FROM calls c JOIN symbols s ON s.id = c.caller_id")
        + " WHERE c.callee = ? GROUP BY s.id ORDER BY s.name LIMIT ?",
        (name, limit)).fetchall()
    return [Entry(kind=r["kind"], name=r["name"], path=r["path"], line=r["start_line"],
                  end_line=r["end_line"], signature=r["signature"]) for r in rows]


def describe_area(path: str) -> tuple[str, str] | None:
    return maintainers.top_level_area(path)
