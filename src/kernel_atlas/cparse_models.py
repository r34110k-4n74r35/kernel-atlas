"""Stable in-memory records emitted by the kernel C parser."""

from __future__ import annotations

from dataclasses import dataclass


FUNCTION = "function"
SYSCALL = "syscall"
STRUCT = "struct"
UNION = "union"
ENUM = "enum"
TYPEDEF = "typedef"
MACRO = "macro"
VARIABLE = "variable"
PROTOTYPE = "prototype"

ALL_KINDS = (
    FUNCTION, SYSCALL, STRUCT, UNION, ENUM, TYPEDEF, MACRO, VARIABLE,
    PROTOTYPE,
)
DEFAULT_KINDS = (
    FUNCTION, SYSCALL, STRUCT, UNION, ENUM, TYPEDEF, MACRO, VARIABLE,
)


@dataclass(slots=True)
class TypeMember:
    """One source-level member of a struct or union.

    ``parent_index`` addresses an earlier entry in the enclosing ``Symbol``'s
    preorder ``members`` tuple. Keeping the parser result self-contained makes
    it safe to send through multiprocessing before database ids exist.
    """

    parent_index: int | None
    name: str | None
    kind: str
    type_text: str | None
    declaration: str
    start_line: int
    end_line: int
    bit_width: str | None = None
    array_dimensions: tuple[str, ...] = ()
    description: str | None = None
    description_source: str | None = None
    conditions: tuple[str, ...] = ()
    visibility: str = "unspecified"
    is_anonymous: bool = False
    generated_by: str | None = None


@dataclass(slots=True)
class Symbol:
    """One indexed source-level C identity."""

    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    is_static: bool = False
    is_inline: bool = False
    is_exported: bool = False
    calls: tuple[str, ...] = ()
    # Names invoked through a parameter or block-scope object. They remain in
    # ``calls`` for call-site coverage, but must not resolve as direct calls.
    indirect_calls: tuple[str, ...] = ()
    summary: str | None = None
    description: str | None = None
    members: tuple[TypeMember, ...] = ()
    aliases: tuple[str, ...] = ()
    is_anonymous: bool = False
    parse_complete: bool = True
    parse_warnings: tuple[str, ...] = ()
    unmatched_member_docs: tuple[tuple[str, str], ...] = ()
    conditions: tuple[str, ...] = ()
