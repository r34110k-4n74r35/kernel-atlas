"""Shared value objects used by kernel-atlas query features."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # ``None`` means the property is not applicable (directories/files and
    # unresolved callees).  Symbols always materialize these as real booleans,
    # which keeps JSON from claiming that a directory is "non-static".
    is_static: bool | None = None
    is_inline: bool | None = None
    is_exported: bool | None = None
    subsystem: str | None = None
    resolution: str | None = None
    is_target: bool = False
    # Internal database identity.  It is deliberately not part of the default
    # machine output, but lets callers distinguish declarations which share a
    # path, name and line (a common ``typedef struct foo { ... } foo`` shape).
    ref_id: int | None = None

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
    is_inline: bool = False
    is_exported: bool = False
    summary: str | None = None
    description: str | None = None
    is_anonymous: bool = False
    parse_complete: bool = True
    conditions: tuple[str, ...] = ()

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
