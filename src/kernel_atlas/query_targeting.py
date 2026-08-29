"""Target normalization and ranking shared by query features."""

from __future__ import annotations

import sqlite3

from .query_models import Target


_COPY_PREFIXES = ("tools/", "samples/", "Documentation/", "usr/")

_KIND_RANK = {
    "function": 0,
    "syscall": 0,
    "struct": 1,
    "typedef": 1,
    "enum": 1,
    "union": 1,
    "macro": 2,
    "variable": 3,
    "prototype": 4,
}


def normalize_spec(spec: str) -> str:
    spec = (spec or "").strip()
    if spec in (".", "/", "./"):
        return ""
    spec = spec.lstrip("/")
    if spec.startswith("./"):
        spec = spec[2:]
    return spec.rstrip("/")


def symbol_target(row: sqlite3.Row) -> Target:
    """Materialize the common symbol fields selected by query modules."""
    return Target(
        kind="symbol",
        id=row["id"],
        path=row["path"],
        name=row["name"],
        symbol_kind=row["kind"],
        line=row["start_line"],
        end_line=row["end_line"],
        signature=row["signature"],
        file_id=row["file_id"],
        dir_id=row["dir_id"],
        is_static=bool(row["is_static"]),
        is_inline=bool(row["is_inline"]),
        is_exported=bool(row["is_exported"]),
    )


def is_copy_path(path: str) -> bool:
    return (path or "").startswith(_COPY_PREFIXES)


def path_rank(path: str) -> tuple:
    """Prefer the kernel proper, then shallow and short paths."""
    path = path or ""
    return (int(is_copy_path(path)), path.count("/"), len(path), path)


def definition_rank(
    path: str,
    symbol_kind: str | None,
    is_static: bool,
) -> tuple:
    """Quality of a definition: real code first, then shallower paths.

    Shorter *string* length used to win, so ``include/linux/raid/pq.h``
    (a ``#define GFP_KERNEL 0`` stub) beat ``include/linux/gfp_types.h``.
    """
    path = path or ""
    copy = int(is_copy_path(path))
    return (
        _KIND_RANK.get(symbol_kind or "", 5),
        copy,
        int(is_static),
        path.count("/"),
        len(path),
        path,
    )


def rank_candidate(target: Target) -> tuple:
    """Prefer real definitions over prototypes, copies, and nested stubs."""
    return (
        *definition_rank(target.path, target.symbol_kind, target.is_static),
        target.line or 0,
        target.id,
    )
