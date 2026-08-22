"""Extract symbols from kernel C sources with tree-sitter.

Kernel C is macro-heavy, so a few idioms need explicit handling on top of the
plain grammar:

  * ``SYSCALL_DEFINE3(open, ...) { ... }`` does not parse as a function.  The
    macro call becomes an ``expression_statement`` and the body a *sibling*
    ``compound_statement``.  We rebuild ``sys_open`` from that shape.
  * ``EXPORT_SYMBOL(foo)`` marks ``foo`` as available to modules, which is one
    of the more useful things to know about a kernel function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter_c
from tree_sitter import Language, Parser, Query, QueryCursor, QueryError

# Symbol kinds this module can emit.
FUNCTION = "function"
SYSCALL = "syscall"
STRUCT = "struct"
UNION = "union"
ENUM = "enum"
TYPEDEF = "typedef"
MACRO = "macro"
VARIABLE = "variable"
PROTOTYPE = "prototype"

ALL_KINDS = (FUNCTION, SYSCALL, STRUCT, UNION, ENUM, TYPEDEF, MACRO, VARIABLE, PROTOTYPE)
DEFAULT_KINDS = (FUNCTION, SYSCALL, STRUCT, UNION, ENUM, TYPEDEF, MACRO, VARIABLE)

# Skip pathological/generated files; nothing human-readable is this big.
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SIGNATURE = 400

_SYSCALL_MACRO = re.compile(r"^(COMPAT_)?SYSCALL_DEFINE(\d)$")
_EXPORT_MACRO = re.compile(
    r"^EXPORT(_PER_CPU)?_SYMBOL(_GPL|_NS|_NS_GPL|_FOR_MODULES)?$")

# Alignment/section attributes written after a declarator. Without the
# preprocessor these look exactly like the variable's name, e.g.
#   struct sem { ... } ____cacheline_aligned_in_smp;
_ATTRIBUTE_MACROS = frozenset({
    "____cacheline_aligned", "____cacheline_aligned_in_smp",
    "____cacheline_internodealigned_in_smp", "__cacheline_aligned",
    "__cacheline_aligned_in_smp", "__read_mostly", "__ro_after_init",
    "__init", "__exit", "__initdata", "__exitdata", "__initconst",
    "__devinitdata", "__meminitdata", "__refdata", "__packed", "__aligned",
    "__maybe_unused", "__used", "__unused", "__weak", "__deprecated",
    "__must_check", "__percpu", "__rcu", "__iomem", "__user", "__kernel",
    "__randomize_layout", "__no_randomize_layout", "__attribute_const__",
    "__nocast", "__safe", "__force", "__private",
})

# Names that can only come out of a misparse of unexpanded macros, never from
# a real declaration, e.g. `STATIC int INIT get_next_block(...)` yielding a
# "variable" called `int`.
_C_TYPE_KEYWORDS = frozenset({
    "int", "long", "short", "char", "unsigned", "signed", "void", "float",
    "double", "bool", "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "size_t", "ssize_t", "struct", "union", "enum", "const", "volatile",
    "static", "extern", "register", "inline", "typedef", "if", "else", "for",
    "while", "do", "return", "goto", "switch", "case", "default", "sizeof",
})

_DECLARATOR_FIELDS = {
    "pointer_declarator", "function_declarator", "array_declarator",
    "parenthesized_declarator", "init_declarator", "attributed_declarator",
}
_IDENTIFIERS = {"identifier", "type_identifier", "field_identifier"}

# Patterns are compiled individually so a grammar that lacks one node type
# degrades gracefully instead of breaking the whole index.
_PATTERNS: list[str] = [
    "(function_definition) @function",
    "(struct_specifier body: (field_declaration_list)) @struct",
    "(union_specifier body: (field_declaration_list)) @union",
    "(enum_specifier body: (enumerator_list)) @enum",
    "(type_definition) @typedef",
    "(preproc_def) @macro",
    "(preproc_function_def) @macro",
    "(translation_unit (expression_statement (call_expression) @macrocall))",
    "(preproc_ifdef (expression_statement (call_expression) @macrocall))",
    "(preproc_if (expression_statement (call_expression) @macrocall))",
    "(preproc_else (expression_statement (call_expression) @macrocall))",
    "(preproc_elif (expression_statement (call_expression) @macrocall))",
    "(translation_unit (declaration) @decl)",
    "(preproc_ifdef (declaration) @decl)",
    "(preproc_if (declaration) @decl)",
    "(preproc_else (declaration) @decl)",
    "(preproc_elif (declaration) @decl)",
]
_CALL_PATTERN = "(call_expression function: (identifier) @callee)"


@dataclass(slots=True)
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    is_static: bool = False
    is_inline: bool = False
    is_exported: bool = False
    calls: tuple[str, ...] = ()


# Built lazily so each multiprocessing worker gets its own parser.
_LANG: Language | None = None
_PARSER: Parser | None = None
_QUERY: Query | None = None
_CALL_QUERY: Query | None = None


def _ensure_parser() -> None:
    global _LANG, _PARSER, _QUERY, _CALL_QUERY
    if _PARSER is not None:
        return
    _LANG = Language(tree_sitter_c.language())
    _PARSER = Parser(_LANG)

    usable = []
    for pat in _PATTERNS:
        try:
            Query(_LANG, pat)
        except QueryError:
            continue
        usable.append(pat)
    _QUERY = Query(_LANG, "\n".join(usable))
    _CALL_QUERY = Query(_LANG, _CALL_PATTERN)


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _squash(s: str, limit: int = MAX_SIGNATURE) -> str:
    s = " ".join(s.split())
    return s[:limit] + "…" if len(s) > limit else s


def _declarator_name(node):
    """Walk down pointer/array/function declarator wrappers to the identifier."""
    cur = node
    for _ in range(32):
        if cur is None:
            return None
        if cur.type in _IDENTIFIERS:
            return cur
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            nxt = next(
                (c for c in cur.named_children
                 if c.type in _DECLARATOR_FIELDS or c.type in _IDENTIFIERS),
                None,
            )
        cur = nxt
    return None


def _is_function_prototype(node) -> bool:
    """True for ``int foo(void);`` but not for the function-pointer variable
    ``int (*fp)(void);`` — in the pointer case the function_declarator wraps a
    parenthesized declarator around the name instead of the name itself."""
    cur = node
    for _ in range(32):
        if cur is None:
            return False
        if cur.type == "function_declarator":
            inner = cur.child_by_field_name("declarator")
            return inner is None or inner.type != "parenthesized_declarator"
        cur = cur.child_by_field_name("declarator") or next(
            (c for c in cur.named_children if c.type in _DECLARATOR_FIELDS), None
        )
    return False


def _lines(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _is_file_scope(node) -> bool:
    """True only for declarations outside any function body.

    The `#ifdef` capture patterns are needed to reach guarded top-level code,
    but `#ifdef` blocks also appear *inside* functions, where a declaration is
    an ordinary local variable rather than a file-scope one.
    """
    cur = node.parent
    while cur is not None:
        if cur.type in ("compound_statement", "function_definition"):
            return False
        if cur.type == "translation_unit":
            return True
        cur = cur.parent
    return True


def _macro_decl_name(src: bytes, node) -> str | None:
    """Recover the declared name from a multi-argument declaration macro.

    Conventions differ — ``DECLARE_WORK(name, fn)`` puts the name first while
    ``DEFINE_PER_CPU(type, name)`` puts it second — and tree-sitter scatters
    the arguments between clean ``type_descriptor`` nodes and ERROR recovery
    nodes depending on whether the first token is a C keyword. Gather every
    argument-ish identifier in order and return the first one that could not be
    a type.
    """
    for child in node.named_children:
        if child.type != "macro_type_specifier":
            continue
        candidates: list[str] = []

        def collect(n) -> None:
            if n.type in ("type_descriptor", "identifier", "type_identifier"):
                text = _text(src, n).strip()
                if text:
                    candidates.append(text)
            elif n.type == "ERROR":
                for c in n.children:
                    collect(c)

        # Skip the leading identifier, which is the macro name itself.
        for arg in list(child.named_children)[1:]:
            collect(arg)

        for cand in candidates:
            if cand.isidentifier() and cand not in _C_TYPE_KEYWORDS:
                return cand
        return None
    return None


def _syscall_name(match: re.Match, arg: str) -> str:
    """COMPAT_SYSCALL_DEFINE4(openat, ...) defines compat_sys_openat, which is a
    different symbol from the sys_openat defined by SYSCALL_DEFINE4."""
    return f"compat_sys_{arg}" if match.group(1) else f"sys_{arg}"


def _first_argument(src: bytes, call_node) -> str | None:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        return _text(src, child).strip()
    return None


def _collect_calls(src: bytes, node) -> tuple[str, ...]:
    """Callee names inside a function body.

    Accepts either a function_definition or a bare compound_statement — the
    latter is what SYSCALL_DEFINEn leaves us with, where the body is a sibling
    of the macro call rather than a child of anything function-shaped.
    """
    body = node if node.type == "compound_statement" else \
        node.child_by_field_name("body")
    if body is None:
        return ()
    cursor = QueryCursor(_CALL_QUERY)
    caps = cursor.captures(body)
    seen: dict[str, None] = {}
    for n in caps.get("callee", []):
        seen.setdefault(_text(src, n), None)
    return tuple(seen)


def parse_source(src: bytes, kinds: frozenset[str], want_calls: bool = False) -> list[Symbol]:
    """Return the symbols defined in one C translation unit."""
    _ensure_parser()
    if len(src) > MAX_FILE_BYTES:
        return []

    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)

    symbols: list[Symbol] = []
    exported: set[str] = set()

    want_fn = FUNCTION in kinds
    want_sys = SYSCALL in kinds
    want_var = VARIABLE in kinds
    want_proto = PROTOTYPE in kinds

    for node in caps.get("function", []):
        if not (want_fn or want_sys):
            break
        name_node = _declarator_name(node.child_by_field_name("declarator"))
        if name_node is None:
            continue
        body = node.child_by_field_name("body")
        head = _text(src, node) if body is None else \
            src[node.start_byte:body.start_byte].decode("utf-8", "replace")
        prefix = head.split("(", 1)[0].split()
        start, end = _lines(node)
        name = _text(src, name_node)

        # SYSCALL_DEFINE0(fork) has a single argument, so unlike its siblings it
        # parses as a real function whose "return type" is the macro itself.
        type_node = node.child_by_field_name("type")
        m = _SYSCALL_MACRO.match(_text(src, type_node)) if type_node is not None else None
        if m:
            if want_sys:
                symbols.append(Symbol(
                    name=_syscall_name(m, name), kind=SYSCALL,
                    start_line=start, end_line=end, signature=_squash(head),
                    calls=_collect_calls(src, node) if want_calls else (),
                ))
            continue

        if not want_fn:
            continue
        symbols.append(Symbol(
            name=name,
            kind=FUNCTION,
            start_line=start,
            end_line=end,
            signature=_squash(head),
            is_static="static" in prefix,
            is_inline=any(w.endswith("inline") for w in prefix),
            calls=_collect_calls(src, node) if want_calls else (),
        ))

    for node in caps.get("macrocall", []):
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            continue
        macro = _text(src, callee)

        if _EXPORT_MACRO.match(macro):
            arg = _first_argument(src, node)
            if arg and arg.isidentifier():
                exported.add(arg)
            continue

        m = _SYSCALL_MACRO.match(macro)
        if m and want_sys:
            arg = _first_argument(src, node)
            if not arg or not arg.isidentifier():
                continue
            # The body is a sibling compound_statement, not a child.
            stmt = node.parent
            body = stmt.next_named_sibling if stmt is not None else None
            start = node.start_point[0] + 1
            end = body.end_point[0] + 1 if body is not None and \
                body.type == "compound_statement" else node.end_point[0] + 1
            symbols.append(Symbol(
                name=_syscall_name(m, arg),
                kind=SYSCALL,
                start_line=start,
                end_line=end,
                signature=_squash(_text(src, node)),
                calls=_collect_calls(src, body) if want_calls and body is not None else (),
            ))

    for capture, kind in (("struct", STRUCT), ("union", UNION), ("enum", ENUM)):
        if kind not in kinds:
            continue
        for node in caps.get(capture, []):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            start, end = _lines(node)
            body = node.child_by_field_name("body")
            nfields = len(body.named_children) if body is not None else 0
            symbols.append(Symbol(
                name=_text(src, name_node),
                kind=kind,
                start_line=start,
                end_line=end,
                signature=f"{kind} {_text(src, name_node)} "
                          f"{{ {nfields} member{'s' if nfields != 1 else ''} }}",
            ))

    if TYPEDEF in kinds:
        for node in caps.get("typedef", []):
            name_node = _declarator_name(node.child_by_field_name("declarator"))
            if name_node is None:
                continue
            start, end = _lines(node)
            symbols.append(Symbol(
                name=_text(src, name_node), kind=TYPEDEF,
                start_line=start, end_line=end,
                signature=_squash(_text(src, node)),
            ))

    if MACRO in kinds:
        for node in caps.get("macro", []):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                name_node = next((c for c in node.named_children
                                  if c.type == "identifier"), None)
            if name_node is None:
                continue
            start, end = _lines(node)
            symbols.append(Symbol(
                name=_text(src, name_node), kind=MACRO,
                start_line=start, end_line=end,
                signature=_squash(_text(src, node), 200),
            ))

    if want_var or want_proto:
        for node in caps.get("decl", []):
            if not _is_file_scope(node):
                continue
            head = _text(src, node)
            prefix = head.split("(", 1)[0].split()
            if "typedef" in prefix:
                continue
            is_static = "static" in prefix
            start, end = _lines(node)

            declarators = node.children_by_field_name("declarator")
            macro_name = _macro_decl_name(src, node)
            if macro_name and VARIABLE in kinds:
                symbols.append(Symbol(
                    name=macro_name, kind=VARIABLE, start_line=start,
                    end_line=end, signature=_squash(head),
                    is_static=is_static))

            for decl in declarators:
                name_node = _declarator_name(decl)
                name = _text(src, name_node).strip() if name_node is not None else ""
                # Tree-sitter sometimes glues the next statement onto this
                # declaration; EXPORT_PER_CPU_SYMBOL(foo) then looks like a
                # function declarator rather than a call.
                if name and _EXPORT_MACRO.match(name):
                    plist = decl.child_by_field_name("parameters")
                    if plist is not None:
                        for p in plist.named_children:
                            arg = _text(src, p).strip()
                            if arg.isidentifier():
                                exported.add(arg)
                    continue
                if macro_name:
                    continue
                if not name or name in _ATTRIBUTE_MACROS \
                        or name in _C_TYPE_KEYWORDS:
                    continue
                kind = PROTOTYPE if _is_function_prototype(decl) else VARIABLE
                # `DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);` parses
                # as a prototype named after the macro; real prototypes are
                # never SHOUTING_CASE.
                if kind == PROTOTYPE and re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
                    continue
                if kind not in kinds:
                    continue
                symbols.append(Symbol(
                    name=name, kind=kind,
                    start_line=start, end_line=end,
                    signature=_squash(head),
                    is_static=is_static,
                ))

    if exported:
        for sym in symbols:
            if sym.kind in (FUNCTION, SYSCALL, VARIABLE) and sym.name in exported:
                sym.is_exported = True

    return symbols


def parse_file(path, kinds: frozenset[str], want_calls: bool = False) -> list[Symbol]:
    try:
        with open(path, "rb") as fh:
            src = fh.read()
    except OSError:
        return []
    return parse_source(src, kinds, want_calls)
