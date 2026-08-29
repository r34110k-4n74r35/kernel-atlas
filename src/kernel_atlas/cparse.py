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
MAX_MEMBER_DECLARATION = 32_000

_SYSCALL_MACRO = re.compile(r"^(COMPAT_)?SYSCALL_DEFINE(\d)$")
_EXPORT_MACRO = re.compile(
    r"^EXPORT(_PER_CPU)?_SYMBOL(_GPL|_NS|_NS_GPL|_FOR_MODULES)?$")
_SOURCE_EXPORT_RE = re.compile(
    rb"(?m)(?:^[ \t]*|}[ \t]*)EXPORT(?:_PER_CPU)?_SYMBOL"
    rb"(?:_GPL|_NS|_NS_GPL|_FOR_MODULES)?[ \t]*\([ \t\r\n]*"
    rb"([A-Za-z_]\w*)[ \t\r\n]*(?=[,)])")

# Declaration-like macros whose expansion creates one file-scope object.  Keep
# this list semantic rather than accepting every shouting-case call: annotations
# such as ``__flag(BPF_F_ANY_ALIGNMENT)`` and registration helpers also look like
# declarations to an unpreprocessed grammar, but their arguments are not object
# names.
_NAME_SECOND_DECL_MACRO = re.compile(
    r"^(?:(?:DEFINE|DECLARE)_PER_CPU(?:_[A-Z0-9_]+)?|"
    r"DEFINE_STATIC_KEY_MAYBE)$")
_NAME_FIRST_DECL_MACRO = re.compile(
    r"^(?:"
    r"DECLARE_(?:WORK|DELAYED_WORK|DEFERRABLE_WORK|BITMAP|COMPLETION(?:_ONSTACK)?|"
    r"WAIT_QUEUE_HEAD(?:_ONSTACK)?|TIMER|TRANSPORT_CLASS|RWSEM)|"
    r"DEFINE_(?:MUTEX|SPINLOCK|RAW_SPINLOCK|RWLOCK|SEQLOCK|TIMER|"
    r"STATIC_KEY_(?:TRUE|FALSE)|STATIC_KEY_ARRAY_(?:TRUE|FALSE)|"
    r"STATIC_KEY_(?:FALSE_RO|DEFERRED_FALSE)|SEMAPHORE|SIMPLE_DEV_PM_OPS|"
    r"XARRAY(?:_ALLOC)?|IDR|IDA|HASHTABLE|RATELIMIT_STATE)|"
    r"(?:ATOMIC|BLOCKING|RAW|SRCU)_NOTIFIER_HEAD|SIMPLE_DEV_PM_OPS|"
    r"SOC_ENUM_SINGLE_DECL|(?:LIST|HLIST|LLIST)_HEAD|RADIX_TREE"
    r")$")
_DECL_MACRO_QUERY_RE = (
    r"(?:(?:DEFINE|DECLARE)_PER_CPU(?:_[A-Z0-9_]+)?|"
    r"DECLARE_(?:WORK|DELAYED_WORK|DEFERRABLE_WORK|BITMAP|COMPLETION(?:_ONSTACK)?|"
    r"WAIT_QUEUE_HEAD(?:_ONSTACK)?|TIMER|TRANSPORT_CLASS|RWSEM)|"
    r"DEFINE_(?:MUTEX|SPINLOCK|RAW_SPINLOCK|RWLOCK|SEQLOCK|TIMER|"
    r"STATIC_KEY_(?:TRUE|FALSE)|STATIC_KEY_ARRAY_(?:TRUE|FALSE)|"
    r"STATIC_KEY_(?:MAYBE|FALSE_RO|DEFERRED_FALSE)|SEMAPHORE|"
    r"SIMPLE_DEV_PM_OPS|XARRAY(?:_ALLOC)?|IDR|IDA|HASHTABLE|"
    r"RATELIMIT_STATE)|(?:ATOMIC|BLOCKING|RAW|SRCU)_NOTIFIER_HEAD|"
    r"SIMPLE_DEV_PM_OPS|SOC_ENUM_SINGLE_DECL|"
    r"(?:LIST|HLIST|LLIST)_HEAD|RADIX_TREE)"
)
_ATTRIBUTE_MACRO_QUERY_RE = (
    r"(?:DEVICE|DRIVER|BUS|CLASS|BIN|SENSOR_DEVICE|IIO_DEVICE|IIO_CONST)"
    r"_[A-Z0-9_]+"
)
_INTERESTING_MACRO_QUERY_RE = (
    rf"^(?:EXPORT(?:_PER_CPU)?_SYMBOL(?:_GPL|_NS|_NS_GPL|_FOR_MODULES)?|"
    rf"(?:COMPAT_)?SYSCALL_DEFINE[0-9]|{_DECL_MACRO_QUERY_RE}|"
    rf"{_ATTRIBUTE_MACRO_QUERY_RE})$")
_LOOP_MACRO_HEAD = re.compile(
    r"^(?:(?:[A-Za-z_]\w*_)?for_each\w*|endfor_\w*)\s*\(")
_RECOVERED_DECL_PREFIX = re.compile(
    r"(?:[A-Za-z_]\w*|\*+)(?:[ \t]+(?:[A-Za-z_]\w*|\*+))*[ \t]*$")
_INLINE_SPECIFIERS = frozenset({
    "inline", "__inline", "__inline__", "__always_inline",
})
_DECLARATION_SPECIFIERS = frozenset({
    "void", "char", "short", "int", "long", "float", "double", "signed",
    "unsigned", "const", "volatile", "restrict", "static", "extern",
    "register", "inline", "_Bool", "_Atomic",
})
_NAME_WRAPPING_DECL_MACROS = frozenset({"__bootdata_preserved"})
_GENERATED_ATTRIBUTE_MACROS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(
        r"^DEVICE_(?:ATTR(?:_(?:RW|RO|WO|ADMIN_RW|ADMIN_RO|RW_NAMED|"
        r"RO_NAMED|WO_NAMED|IGNORE_LOCKDEP))?|(?:ULONG|INT|BOOL)_ATTR|"
        r"STRING_ATTR_RO)$"), "dev_attr_"),
    (re.compile(r"^DRIVER_ATTR_(?:RW|RO|WO|IGNORE_LOCKDEP)$"),
     "driver_attr_"),
    (re.compile(r"^BUS_ATTR_(?:RW|RO|WO)$"), "bus_attr_"),
    (re.compile(r"^CLASS_ATTR_(?:RW|RO|WO|STRING)$"), "class_attr_"),
    (re.compile(
        r"^BIN_ATTR(?:_(?:RO|WO|RW|ADMIN_RO|ADMIN_RW|SIMPLE_RO|"
        r"SIMPLE_ADMIN_RO))?$"), "bin_attr_"),
    (re.compile(r"^SENSOR_DEVICE_ATTR(?:_2)?(?:_(?:RO|WO|RW))?$"),
     "sensor_dev_attr_"),
    (re.compile(r"^IIO_DEVICE_ATTR(?:_(?:RO|WO|RW|NAMED))?$"),
     "iio_dev_attr_"),
    (re.compile(r"^IIO_CONST_ATTR(?:_NAMED)?$"), "iio_const_attr_"),
)
_FIXED_GENERATED_ATTRIBUTE_MACROS = {
    "IIO_CONST_ATTR_SAMP_FREQ_AVAIL":
        "iio_const_attr_sampling_frequency_available",
    "IIO_CONST_ATTR_INT_TIME_AVAIL":
        "iio_const_attr_integration_time_available",
    "IIO_CONST_ATTR_TEMP_OFFSET": "iio_const_attr_in_temp_offset",
    "IIO_CONST_ATTR_TEMP_SCALE": "iio_const_attr_in_temp_scale",
}

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
    "__nocast", "__safe", "__force", "__private", "__alias",
    "__latent_entropy", "__page_aligned_bss", "__acquires", "__releases",
    "__section",
    "__nonstring", "__counted_by", "__counted_by_le", "__guarded_by",
    "__aligned_largest", "__printf", "__scanf",
    "__counted_by_ptr", "__counted_by_be", "__module_memory_align",
    "__kernel_nonstring", "CRYPTO_MINALIGN_ATTR", "ACPI_NONSTRING",
    "BPMP_UNION_ANON", "BPMP_ABI_PACKED", "EPOLL_PACKED",
    "__ATM_API_ALIGN", "__ARCH_COMPAT_FLOCK64_PACK", "PACKED",
    "ARCH_PACK_STATFS64", "ARCH_PACK_COMPAT_STATFS64",
}) | _NAME_WRAPPING_DECL_MACROS
_CALL_ATTRIBUTE_MACROS = frozenset({
    "__aligned", "__section", "__alias", "__acquires", "__releases",
    "__counted_by", "__counted_by_le", "__counted_by_ptr",
    "__counted_by_be", "__guarded_by", "__printf", "__scanf",
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
    "NULL",
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
    rf'''(
      (call_expression function: (identifier) @macro_name) @macrocall
      (#match? @macro_name "{_INTERESTING_MACRO_QUERY_RE}")
    )''',
    rf'''(
      (macro_type_specifier name: (identifier) @decl_macro_name) @decl_macro
      (#match? @decl_macro_name "^{_DECL_MACRO_QUERY_RE}$")
    )''',
    rf'''(
      (function_declarator declarator: (identifier) @decl_call_name) @decl_call
      (#match? @decl_call_name "^{_DECL_MACRO_QUERY_RE}$")
    )''',
    r'''(
      (macro_type_specifier name: (identifier) @syscall_type_name) @syscall_type
      (#match? @syscall_type_name "^(?:COMPAT_)?SYSCALL_DEFINE[0-9]$")
    )''',
    "(translation_unit (declaration) @decl)",
    "(preproc_ifdef (declaration) @decl)",
    "(preproc_if (declaration) @decl)",
    "(preproc_else (declaration) @decl)",
    "(preproc_elif (declaration) @decl)",
    # Error recovery can enclose the rest of an otherwise valid translation
    # unit after one macro-heavy initializer.  Its direct declarations are
    # still file-scope; locals retain a compound/function ancestor and are
    # rejected by _is_file_scope below.
    "(ERROR (declaration) @decl)",
]
_CALL_PATTERN = "(call_expression function: (identifier) @callee)"


@dataclass(slots=True)
class TypeMember:
    """One source-level member of a struct, including nested aggregates.

    ``parent_index`` addresses an earlier entry in the enclosing Symbol's
    preorder ``members`` tuple.  Keeping the parser result self-contained
    makes it safe to send through multiprocessing before database ids exist.
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
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    is_static: bool = False
    is_inline: bool = False
    is_exported: bool = False
    calls: tuple[str, ...] = ()
    # Names invoked through a parameter or block-scope object.  They remain in
    # ``calls`` so call-site coverage is complete, but must never be promoted
    # to a same-named file/global function by the indexer.
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


def _function_signature(s: str) -> str:
    """Normalize a function head without leaking conditional directives."""
    return _squash(re.sub(r"(?m)^[ \t]*#[^\n]*(?:\n|$)", " ", s))


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
    """Distinguish a function declaration from a function-pointer object.

    Walk *outward from the identifier*.  In ``int (*fp)(void)`` a pointer (and
    possibly an array) is encountered before the first function declarator.  In
    ``int (*factory(void))(int)`` the inner function declarator comes first, so
    this is a prototype for a function returning a function pointer.
    """
    name = _declarator_name(node)
    cur = name.parent if name is not None else None
    indirect = False
    for _ in range(64):
        if cur is None:
            return False
        if cur.type == "function_declarator":
            return not indirect
        if cur.type in ("pointer_declarator", "array_declarator"):
            indirect = True
        if cur is node:
            return False
        cur = cur.parent
    return False


def _lines(node) -> tuple[int, int]:
    start = node.start_point[0] + 1
    # Tree-sitter ranges are half-open.  A node ending immediately after a
    # newline points at column zero of the following line; reporting that as
    # part of the symbol creates locations beyond a one-line file.
    end = node.end_point[0] + (0 if node.end_point[1] == 0 else 1)
    return start, max(start, end)


def _starts_recovered_toplevel(src: bytes, node) -> bool:
    """Whether a nested recovery node begins like a top-level definition."""
    if node.start_point[1] == 0:
        return True
    line_start = src.rfind(b"\n", 0, node.start_byte) + 1
    prefix = src[line_start:node.start_byte].decode("utf-8", "replace")
    # Tree-sitter may start the function node after unfamiliar attributes or a
    # tag keyword: ``static __always_inline struct <node starts here>``.  Real
    # top-level prefixes start in column zero and consist solely of declaration
    # words/pointers; statement macros and locals are indented or punctuated.
    return bool(prefix and not prefix[0].isspace()
                and _RECOVERED_DECL_PREFIX.fullmatch(prefix))


def _is_file_scope(src: bytes, node) -> bool:
    """True for real or error-recovered file-scope syntax.

    Tree-sitter occasionally lets one macro-heavy function consume the rest of
    a translation unit.  The later, genuine top-level definitions then have a
    ``compound_statement``/``function_definition`` ancestor even though their
    source lines start in column zero.  Admit that specific recovery shape, but
    keep rejecting ordinary indented locals and nodes in a valid function.
    """
    cur = node.parent
    inside_function = False
    recovered_function = False
    recovery_container = False
    function_ancestor = None
    while cur is not None:
        if cur.type == "function_definition":
            inside_function = True
            recovered_function = recovered_function or cur.has_error
            function_ancestor = cur
        elif cur.type in ("compound_statement", "ERROR"):
            recovery_container = True
        elif cur.type in ("preproc_def", "preproc_function_def"):
            return False
        if cur.type == "translation_unit":
            if inside_function:
                body = (function_ancestor.child_by_field_name("body")
                        if function_ancestor is not None else None)
                closing = (_matching_delimiter(
                    src, body.start_byte, ord("{"), ord("}"))
                    if body is not None else None)
                if closing is not None and node.start_byte < closing:
                    return False
                return _starts_recovered_toplevel(src, node) \
                    and recovered_function
            return not recovery_container or _starts_recovered_toplevel(src, node)
        cur = cur.parent
    # A severely malformed file can have ERROR as its root rather than a
    # translation_unit.  Do not let that exceptional root turn nested locals
    # back into file-scope symbols.
    if inside_function:
        body = (function_ancestor.child_by_field_name("body")
                if function_ancestor is not None else None)
        closing = (_matching_delimiter(
            src, body.start_byte, ord("{"), ord("}"))
            if body is not None else None)
        if closing is not None and node.start_byte < closing:
            return False
        return _starts_recovered_toplevel(src, node) and recovered_function
    return not recovery_container or _starts_recovered_toplevel(src, node)


def _in_preprocessor_continuation(src: bytes, node) -> bool:
    line_start = src.rfind(b"\n", 0, node.start_byte) + 1
    if line_start <= 0:
        return False
    previous_start = src.rfind(b"\n", 0, line_start - 1) + 1
    return src[previous_start:line_start - 1].rstrip().endswith(b"\\")


def _split_macro_args(text: str, open_paren: int) -> list[str] | None:
    """Split a macro invocation without being confused by nested calls.

    This intentionally is not a C lexer; it only needs to preserve commas in
    balanced parentheses/brackets/braces and quoted strings, which covers the
    declaration macros used by the kernel.
    """
    args: list[str] = []
    start = open_paren + 1
    stack = [")"]
    quote = ""
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[ch])
        elif ch == stack[-1]:
            stack.pop()
            if not stack:
                args.append(text[start:i].strip())
                return args
        elif ch == "," and len(stack) == 1:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    return None


def _macro_arg_byte_ranges(data: bytes, open_paren: int,
                           closing: int) -> list[tuple[int, int]]:
    """Top-level argument byte ranges inside one balanced invocation."""
    ranges: list[tuple[int, int]] = []
    start = open_paren + 1
    stack = [ord(")")]
    quote = 0
    i = start
    while i < closing:
        if quote:
            if data[i] == ord("\\"):
                i += 2
                continue
            if data[i] == quote:
                quote = 0
        elif data.startswith(b"//", i):
            newline = data.find(b"\n", i + 2, closing)
            i = closing if newline < 0 else newline
            continue
        elif data.startswith(b"/*", i):
            end = data.find(b"*/", i + 2, closing)
            i = closing if end < 0 else end + 1
        elif data[i] in (ord('"'), ord("'")):
            quote = data[i]
        elif data[i] in (ord("("), ord("["), ord("{")):
            stack.append({ord("("): ord(")"), ord("["): ord("]"),
                          ord("{"): ord("}")}[data[i]])
        elif data[i] == stack[-1]:
            stack.pop()
        elif data[i] == ord(",") and len(stack) == 1:
            ranges.append((start, i))
            start = i + 1
        i += 1
    ranges.append((start, closing))
    return ranges


def _macro_decl(src: bytes, node) -> tuple[str, str] | None:
    """Return ``(macro, object-name)`` for a supported declaration macro."""
    text = _text(src, node)
    macro_match = re.search(
        rf"\b(?P<name>{_DECL_MACRO_QUERY_RE})\s*\(", text)
    if macro_match is None:
        return None

    # A recognized call buried in an ordinary initializer is not the
    # declaration itself.  Before the macro only storage/attribute specifiers
    # are allowed.
    prefix = text[:macro_match.start()].strip()
    if prefix:
        allowed = {"static", "extern", "const", "volatile", "register"} | \
            set(_ATTRIBUTE_MACROS)
        if any(word not in allowed for word in prefix.split()):
            return None

    macro = macro_match.group("name")
    args = _split_macro_args(text, macro_match.end() - 1)
    if not args:
        return None
    if _NAME_SECOND_DECL_MACRO.fullmatch(macro):
        index = 1
    elif _NAME_FIRST_DECL_MACRO.fullmatch(macro):
        index = 0
    else:
        return None
    if index >= len(args):
        return None

    # Array declaration macros sometimes accept ``name[COUNT]``.  A wrapped
    # expression such as kvm_nvhe_sym(name) is configuration-dependent and has
    # no single source-level identifier, so leave it out.
    arg = args[index].strip()
    m = re.fullmatch(r"([A-Za-z_]\w*)\s*(?:\[[^]]*\]\s*)*", arg)
    if m is None:
        return None
    name = m.group(1)
    if name in _C_TYPE_KEYWORDS or name in _ATTRIBUTE_MACROS:
        return None
    return macro, name


def _macro_shaped_declaration(text: str) -> tuple[str, list[str]] | None:
    """A file-scope declaration whose surface syntax is one macro call."""
    match = re.search(r"\b([A-Z][A-Z0-9_]+)\s*\(", text)
    if match is None:
        return None
    allowed = {"static", "extern", "const", "volatile", "register"} | \
        set(_ATTRIBUTE_MACROS)
    if any(word not in allowed for word in text[:match.start()].split()):
        return None
    args = _split_macro_args(text, match.end() - 1)
    return (match.group(1), args) if args is not None else None


def _generated_attribute_decl(text: str) -> str | None:
    """Source identity created by standard sysfs attribute macros."""
    shaped = _macro_shaped_declaration(text)
    if shaped is None:
        return None
    macro, args = shaped
    if not args:
        return None
    if macro in _FIXED_GENERATED_ATTRIBUTE_MACROS:
        return _FIXED_GENERATED_ATTRIBUTE_MACROS[macro]
    for pattern, prefix in _GENERATED_ATTRIBUTE_MACROS:
        if pattern.fullmatch(macro):
            name = args[0].strip()
            return prefix + name if name.isidentifier() else None
    return None


def _source_code_leaf(root, offset: int):
    """Smallest AST node at a source offset, excluding non-code containers."""
    node = root.descendant_for_byte_range(offset, offset + 1)
    cur = node
    while cur is not None and cur.type != "translation_unit":
        if cur.type in {
                "comment", "string_literal", "char_literal",
                "preproc_def", "preproc_function_def"}:
            return None
        cur = cur.parent
    return node


def _source_exports(src: bytes, root) -> set[str]:
    """Canonical source-level exports, independent of recovery node shape.

    Some ERROR trees bury a whole run of exports below type descriptors, where
    no useful query capture exists.  The line-anchored spelling is unambiguous;
    checking its smallest AST ancestor excludes comments, strings, and macro
    definitions/continuations.
    """
    exported: set[str] = set()
    for match in _SOURCE_EXPORT_RE.finditer(src):
        if _source_code_leaf(root, match.start(1)) is not None:
            exported.add(match.group(1).decode("ascii"))
    return exported


def _safe_declarators(src: bytes, node) -> list:
    """Direct declarators before tree-sitter's first recovery node.

    In a macro-heavy aggregate initializer tree-sitter can attach hundreds of
    initializer identifiers as additional ``declarator`` fields.  A direct
    ERROR child marks the point where that interpretation stopped being
    reliable; valid comma-separated declarators occur before it.
    """
    out = []
    recovered = False
    for i, child in enumerate(node.children):
        if child.type == "ERROR":
            error_text = _text(src, child).strip()
            words = error_text.split()
            if words and all(
                    word in _ATTRIBUTE_MACROS
                    or word in _DECLARATION_SPECIFIERS for word in words):
                continue
            recovered = True
            continue
        if node.field_name_for_child(i) == "declarator" and not recovered:
            out.append(child)
    return out


def _has_trailing_attribute_terminator(src: bytes, node) -> bool:
    """A known trailing attribute split into a same-line sibling."""
    sibling = node.next_named_sibling
    if sibling is None or sibling.start_point[0] != node.end_point[0]:
        return False
    text = _text(src, sibling).strip()
    match = re.fullmatch(
        r"([A-Za-z_]\w*)(?:\s*\(.*\))?(?:\s*=\s*.*)?\s*;", text)
    return match is not None and match.group(1) in _ATTRIBUTE_MACROS


def _attribute_declaration_name(text: str) -> str | None:
    """Recover ``object`` from ``type object __known_attribute;``."""
    wrappers = "|".join(
        re.escape(name) for name in _NAME_WRAPPING_DECL_MACROS)
    wrapped = re.search(
        rf"\b(?:{wrappers})\b\s*\(\s*([A-Za-z_]\w*)"
        rf"(?:\s*\[[^]]*\]\s*)*\)\s*;?\s*$", text)
    if wrapped is not None:
        return wrapped.group(1)
    attrs = "|".join(re.escape(name) for name in _ATTRIBUTE_MACROS)
    match = re.search(rf"\b(?:{attrs})\b\s*;?\s*$", text)
    if match is None:
        return None
    prefix = text[:match.start()].rstrip()
    name_match = re.search(
        r"([A-Za-z_]\w*)\s*(?:\[[^]]*\]\s*)*$", prefix)
    if name_match is None:
        return None
    name = name_match.group(1)
    return name if name not in _C_TYPE_KEYWORDS else None


def _initializer_declaration_name(text: str) -> str | None:
    """Recover a declarator hidden before an attributed initializer.

    With ``object __aligned(...) = { ... }`` tree-sitter can put ``object`` in
    an ERROR node and expose the attribute as the declarator.  Work only on the
    declaration head, peel known trailing attributes, and then accept the
    ordinary final identifier/array shape.
    """
    equals = text.find("=")
    if equals < 0:
        return None
    prefix = text[:equals].rstrip()
    attrs = "|".join(re.escape(name) for name in _ATTRIBUTE_MACROS)
    attribute = re.compile(
        rf"\s+\b(?:{attrs})\b(?:\s*\([^()]*\))?\s*$")
    while match := attribute.search(prefix):
        prefix = prefix[:match.start()].rstrip()
    name_match = re.search(
        r"([A-Za-z_]\w*)\s*(?:\[[^]]*\]\s*)*$", prefix)
    if name_match is None:
        return None
    name = name_match.group(1)
    return name if (name not in _C_TYPE_KEYWORDS
                    and name not in _ATTRIBUTE_MACROS) else None


def _member_count(body, kind: str) -> int:
    """Count declared fields/enumerators, including guarded declarations."""
    wanted = "enumerator" if kind == ENUM else "field_declaration"

    def count(node) -> int:
        if node.type == wanted:
            if wanted == "enumerator":
                return 1
            # ``int a, b`` has two members; an anonymous struct/union member has
            # no declarator but still contributes one.
            return max(1, len(node.children_by_field_name("declarator")))
        return sum(count(child) for child in node.named_children)

    return count(body) if body is not None else 0


def _aggregate_signature(src: bytes, node, kind: str, name: str,
                         nfields: int) -> str:
    """Compact aggregate identity including ABI-relevant source attributes."""
    body = node.child_by_field_name("body")
    attributes: list[str] = []
    if body is not None:
        outer = _outer_declaration(node)
        # Error recovery around unexpanded declaration macros can leave a
        # same-line suffix outside ``outer``.  Include only that physical-line
        # tail; the allowlist below prevents a following declarator name from
        # becoming an attribute.
        line_end = src.find(b"\n", body.end_byte)
        if line_end < 0:
            line_end = len(src)
        context = (src[outer.start_byte:body.start_byte]
                   + b" " + src[body.end_byte:max(outer.end_byte, line_end)]).decode(
                       "utf-8", "replace")
        context = _strip_c_comments(context)
        for match in re.finditer(
                r"\b([A-Za-z_]\w*)\b(?:\s*\([^{};]*\))?", context):
            if match.group(1) in _ATTRIBUTE_MACROS \
                    or match.group(1) == "__attribute__":
                attributes.append(_squash(match.group(0), 200))
    qualifier = " " + " ".join(dict.fromkeys(attributes)) if attributes else ""
    return (f"{kind} {name}{qualifier} {{ {nfields} "
            f"member{'s' if nfields != 1 else ''} }}")


@dataclass(slots=True)
class _KernelDoc:
    summary: str | None
    description: str | None
    members: dict[str, str]
    source: str = "kernel-doc"


def _comment_lines(text: str) -> list[str]:
    """Remove C comment furniture without rewriting kernel-doc markup."""
    if text.lstrip().startswith("//"):
        return [re.sub(r"^\s*// ?", "", line).rstrip()
                for line in text.splitlines()]
    text = re.sub(r"^\s*/\*+!?", "", text)
    text = re.sub(r"\*/\s*$", "", text)
    return [re.sub(r"^\s*\* ?", "", line).rstrip()
            for line in text.splitlines()]


def _paragraphs(lines: list[str]) -> str | None:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) or None


def _outer_declaration(node):
    cur = node
    while cur.parent is not None and cur.parent.type in {
            "type_definition", "declaration", "attributed_declarator"}:
        cur = cur.parent
    return cur


def _parse_aggregate_doc(
        text: str, identities: set[str], source: str = "kernel-doc") \
        -> _KernelDoc:
    """Parse a kernel-style aggregate comment already selected by caller."""
    lines = _comment_lines(text)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return _KernelDoc(None, None, {})

    first = lines[0].strip()
    explicit = re.match(
        r"(?:struct|union|typedef)\s+([A-Za-z_]\w*)"
        r"(?:\s*[-:]\s*(.*))?$", first)
    conventional = re.match(
        r"([A-Za-z_]\w*)\s*[-:]\s*(.*)$", first)
    body_start = 0
    summary_parts: list[str] = []
    if explicit is not None:
        if explicit.group(1) not in identities:
            return _KernelDoc(None, None, {}, source)
        if explicit.group(2):
            summary_parts.append(explicit.group(2).strip())
        body_start = 1
    elif conventional is not None:
        if conventional.group(1) not in identities:
            return _KernelDoc(None, None, {}, source)
        if conventional.group(2).strip():
            summary_parts.append(conventional.group(2).strip())
        body_start = 1

    member_lines: dict[str, list[str]] = {}
    member_indent: int | None = None
    prose: list[str] = []
    active: list[str] = []
    in_brief = True
    for line in lines[body_start:]:
        doxygen_brief = re.match(r"^\s*[@\\]brief\s+(.*)$", line)
        if in_brief and doxygen_brief is not None:
            summary_parts.append(doxygen_brief.group(1).strip())
            continue
        # Top-level member markers share the comment's base indentation.  A
        # callback's own @arg documentation is conventionally indented more
        # deeply.  Compare visual indentation instead of requiring column
        # zero: many kernel comments use a tab before every top-level @member.
        field = re.match(r"^([ \t]*)@([^:]+):\s*(.*)$", line)
        indent = (len(field.group(1).expandtabs(8))
                  if field is not None else None)
        if field is not None and (member_indent is None
                                  or indent <= member_indent):
            member_indent = indent if member_indent is None else min(
                member_indent, indent)
            in_brief = False
            keys = [
                key.lstrip("@").strip()
                for key in re.split(r"\s*[,/]\s*", field.group(2))
                if key.lstrip("@").strip()
            ]
            active = keys
            for key in keys:
                member_lines.setdefault(key, []).append(field.group(3).strip())
            continue
        if in_brief and line.strip():
            summary_parts.append(line.strip())
            continue
        if line.strip() in {"Description:", "Context:"}:
            active = []
            continue
        if active and line.strip():
            for key in active:
                member_lines[key].append(line.strip())
            continue
        if not line.strip():
            in_brief = False
            active = []
            prose.append("")
            continue
        prose.append(line)

    members = {
        name: value
        for name, parts in member_lines.items()
        if (value := _paragraphs(parts)) is not None
    }
    summary = " ".join(summary_parts) or None
    return _KernelDoc(summary, _paragraphs(prose), members, source)


def _adjacent_comment_raw(src: bytes, offset: int, *, kernel_doc: bool = False) \
        -> str | None:
    prefix = src[:offset]
    end = prefix.rfind(b"*/")
    if end < 0 or prefix[end + 2:].strip():
        return None
    begin = prefix.rfind(b"/*", 0, end)
    if begin < 0:
        return None
    is_kernel_doc = prefix[begin:begin + 3] == b"/**"
    if kernel_doc != is_kernel_doc:
        return None
    return prefix[begin:end + 2].decode("utf-8", "replace")


def _kernel_doc(src: bytes, node, identities: set[str]) -> _KernelDoc:
    """Parse the adjacent aggregate kernel-doc block, when it names us."""
    raw = _adjacent_comment_raw(
        src, _outer_declaration(node).start_byte, kernel_doc=True)
    return (_parse_aggregate_doc(raw, identities) if raw is not None
            else _KernelDoc(None, None, {}))


def _adjacent_ordinary_comment(src: bytes, node) -> str | None:
    """A tightly adjacent non-kernel-doc comment as a conservative summary."""
    start = _outer_declaration(node).start_byte
    prefix = src[:start]
    end = prefix.rfind(b"*/")
    if end < 0 or prefix[end + 2:].strip():
        return None
    begin = prefix.rfind(b"/*", 0, end)
    if begin < 0 or prefix[begin:begin + 3] == b"/**":
        return None
    value = _paragraphs(_comment_lines(
        prefix[begin:end + 2].decode("utf-8", "replace")))
    if value and "SPDX-License-Identifier" not in value:
        return value
    return None


def _typedef_aliases(src: bytes, node) -> tuple[str, ...]:
    cur = node.parent
    while cur is not None and cur.type not in {
            "type_definition", "translation_unit", "field_declaration"}:
        cur = cur.parent
    if cur is None or cur.type != "type_definition":
        return ()
    aliases: list[str] = []
    for declarator in _safe_declarators(src, cur):
        name_node = _declarator_name(declarator)
        if name_node is not None:
            ancestor = name_node.parent
            indirect = False
            while ancestor is not None:
                if ancestor.type in {
                        "pointer_declarator", "array_declarator",
                        "function_declarator"}:
                    indirect = True
                    break
                if ancestor is declarator:
                    break
                ancestor = ancestor.parent
            if indirect:
                continue
            name = _text(src, name_node).strip()
            if name.isidentifier() and name not in _C_TYPE_KEYWORDS:
                aliases.append(name)
    return tuple(dict.fromkeys(aliases))


def _strip_c_comments(text: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", " ", text)


def _base_member_type(src: bytes, field, type_node) -> str | None:
    if type_node is None:
        return None
    if type_node.type in {"struct_specifier", "union_specifier"}:
        kind = "struct" if type_node.type == "struct_specifier" else "union"
        name_node = type_node.child_by_field_name("name")
        name = _text(src, name_node).strip() if name_node is not None else ""
        if name in _ATTRIBUTE_MACROS:
            name = ""
        qualifiers = src[field.start_byte:type_node.start_byte].decode(
            "utf-8", "replace")
        declarators = _field_declarators(src, field)
        annotations = ""
        if declarators:
            between = src[type_node.end_byte:min(
                declarator.start_byte for declarator in declarators
            )].decode("utf-8", "replace").strip()
            words = re.findall(r"[A-Za-z_]\w*", between)
            if words and all(
                    word in _ATTRIBUTE_MACROS or word.startswith("__")
                    or word in {"const", "volatile", "restrict", "_Atomic"}
                    for word in words):
                annotations = between
        return _squash(
            f"{_strip_c_comments(qualifiers)} {kind} {name} {annotations}",
            MAX_MEMBER_DECLARATION,
        ) or None
    end = type_node.end_byte
    declarators = _field_declarators(src, field)
    if declarators:
        between_end = min(declarator.start_byte for declarator in declarators)
        between = src[type_node.end_byte:between_end].decode(
            "utf-8", "replace").strip()
        words = re.findall(r"[A-Za-z_]\w*", between)
        if words and all(
                word in _ATTRIBUTE_MACROS or word.startswith("__")
                or word in {"const", "volatile", "restrict", "_Atomic"}
                for word in words):
            end = between_end
    prefix = src[field.start_byte:end].decode("utf-8", "replace")
    return _squash(_strip_c_comments(prefix), MAX_MEMBER_DECLARATION) or None


def _declarator_type(src: bytes, base: str | None, declarator) -> str | None:
    name_node = _declarator_name(declarator)
    if name_node is None:
        return base
    before = src[declarator.start_byte:name_node.start_byte]
    after = src[name_node.end_byte:declarator.end_byte]
    shape = (before + after).decode("utf-8", "replace")
    return _squash(f"{base or ''} {shape}", MAX_MEMBER_DECLARATION) or None


def _array_dimensions(src: bytes, declarator) -> tuple[str, ...]:
    dimensions: list[str] = []
    name = _declarator_name(declarator)
    current = name.parent if name is not None else None
    while current is not None:
        if current.type == "array_declarator":
            size = current.child_by_field_name("size")
            dimensions.append(
                _text(src, size).strip() if size is not None else "")
        if current is declarator:
            break
        current = current.parent
    return tuple(dimensions)


def _is_function_pointer(declarator) -> bool:
    name = _declarator_name(declarator)
    cur = name.parent if name is not None else None
    saw_pointer = False
    while cur is not None:
        if cur.type == "pointer_declarator":
            saw_pointer = True
        elif cur.type == "function_declarator":
            return saw_pointer
        if cur is declarator:
            break
        cur = cur.parent
    return False


def _field_declarators(src: bytes, field) -> list:
    """Declarators, recovering a real field hidden by a trailing attribute."""
    declarators = list(field.children_by_field_name("declarator"))
    names = []
    for declarator in declarators:
        name_node = _declarator_name(declarator)
        names.append(_text(src, name_node).strip()
                     if name_node is not None else "")
    attribute_only = bool(declarators) and all(
        name in _ATTRIBUTE_MACROS for name in names)
    if not attribute_only:
        return declarators

    recovered: list = []
    for child in field.named_children:
        if child.type != "ERROR":
            continue
        for candidate in child.named_children:
            name_node = _declarator_name(candidate)
            if name_node is None:
                continue
            name = _text(src, name_node).strip()
            if name and name not in _ATTRIBUTE_MACROS:
                recovered.append(candidate)
                break
    return recovered or declarators


def _bitfield_for(src: bytes, field, declarator, next_declarator) -> str | None:
    for child in field.named_children:
        if child.type != "bitfield_clause":
            continue
        if child.start_byte < declarator.end_byte:
            continue
        if next_declarator is not None and child.start_byte > next_declarator.start_byte:
            continue
        text = _text(src, child).strip()
        return text[1:].strip() if text.startswith(":") else text
    return None


def _comment_description(
        text: str) -> tuple[str | None, str | None, str | None]:
    kernel_doc = text.lstrip().startswith("/**")
    lines = _comment_lines(text)
    value = _paragraphs(lines)
    if value is None:
        return None, None, None
    field = re.match(r"^@([^:]+):\s*", value)
    key = field.group(1).strip() if field is not None else None
    value = value[field.end():].strip() if field is not None else value.strip()
    return (value or None,
            "inline-kernel-doc" if kernel_doc else "source-comment", key)


def _is_visibility_marker(text: str) -> bool:
    return re.fullmatch(
        r"\s*/\*+\s*(?:private|public)\s*:[\s\S]*?\*/\s*",
        text, re.IGNORECASE) is not None


def _trailing_member_comment(
        src: bytes, field, end_byte: int | None = None) \
        -> tuple[str | None, str | None, str | None]:
    end_byte = field.end_byte if end_byte is None else end_byte
    line_end = src.find(b"\n", end_byte)
    if line_end < 0:
        line_end = len(src)
    tail = src[end_byte:line_end].decode("utf-8", "replace")
    match = re.match(r"^\s*(/\*.*?\*/|//.*)", tail)
    if match is None or _is_visibility_marker(match.group(1)):
        return None, None, None
    return _comment_description(match.group(1))


def _declarator_comment(src: bytes, declarator, next_declarator) \
        -> tuple[str | None, str | None, str | None]:
    """Comment between comma declarators, describing the preceding member."""
    end = next_declarator.start_byte if next_declarator is not None else \
        declarator.parent.end_byte
    between = src[declarator.end_byte:end].decode("utf-8", "replace")
    comments = re.findall(r"/\*[\s\S]*?\*/|//[^\n]*", between)
    comments = [comment for comment in comments
                if not _is_visibility_marker(comment)]
    return _comment_description(comments[0]) if comments else (None, None, None)


def _bpmp_empty_member(
        declaration: str, start_line: int, end_line: int,
        parent_index: int | None = None,
        conditions: tuple[str, ...] = (), visibility: str = "unspecified",
        description: str | None = None,
        description_source: str | None = None) -> TypeMember:
    """Model BPMP's optional compatibility member without choosing a build."""
    if description is None:
        description = (
            "Expands to `char empty;` when NO_GCC_EXTENSIONS is defined and "
            "to no member otherwise."
        )
        description_source = "macro-semantics"
    return TypeMember(
        parent_index=parent_index, name="empty", kind="macro",
        type_text="conditional char member (otherwise empty)",
        declaration=_squash(declaration, MAX_MEMBER_DECLARATION),
        start_line=start_line, end_line=end_line,
        description=description, description_source=description_source,
        conditions=(*conditions, "#ifdef NO_GCC_EXTENSIONS"),
        visibility=visibility, generated_by="BPMP_ABI_EMPTY",
    )


def _macro_member(src: bytes, field, parent_index: int | None,
                  conditions: tuple[str, ...], visibility: str) \
        -> TypeMember | None:
    raw = _text(src, field)
    if re.fullmatch(r"\s*BPMP_ABI_EMPTY\s*;?\s*", raw):
        start, end = _lines(field)
        return _bpmp_empty_member(
            raw, start, end, parent_index, conditions, visibility)
    type_node = field.child_by_field_name("type")
    if type_node is None or type_node.type != "macro_type_specifier":
        return None
    cacheline = re.search(
        r"\b(__cacheline_group_(begin|end)(?:_aligned)?)\s*\(", raw)
    if cacheline is not None:
        args = _split_macro_args(raw, cacheline.end() - 1)
        group = args[0].strip() if args else ""
        if group.isidentifier():
            macro, boundary = cacheline.group(1), cacheline.group(2)
            start, end = _lines(field)
            return TypeMember(
                parent_index=parent_index,
                name=f"__cacheline_group_{boundary}__{group}",
                kind="field", type_text="__u8 [0]",
                declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                start_line=start, end_line=end, array_dimensions=("0",),
                description=(f"Zero-length marker for the {boundary} of "
                             f"cacheline group {group}."),
                description_source="macro-semantics",
                conditions=conditions, visibility=visibility,
                generated_by=macro,
            )

    match = re.search(
        r"\b(DECLARE_BITMAP|DECLARE_FLEX_ARRAY|__DECLARE_FLEX_ARRAY)\s*\(",
        raw,
    )
    if match is None:
        return None
    args = _split_macro_args(raw, match.end() - 1)
    if not args:
        return None
    macro = match.group(1)
    if macro == "DECLARE_BITMAP":
        if len(args) < 2 or not args[0].strip().isidentifier():
            return None
        name = args[0].strip()
        dimensions = (f"BITS_TO_LONGS({args[1].strip()})",)
        type_text = f"unsigned long [{dimensions[0]}]"
    else:
        if len(args) < 2 or not args[1].strip().isidentifier():
            return None
        name = args[1].strip()
        dimensions = ("",)
        type_text = f"{args[0].strip()} []"
    start, end = _lines(field)
    return TypeMember(
        parent_index=parent_index, name=name, kind="field",
        type_text=type_text,
        declaration=_squash(raw, MAX_MEMBER_DECLARATION),
        start_line=start, end_line=end, array_dimensions=dimensions,
        conditions=conditions, visibility=visibility, generated_by=macro,
    )


def _recover_annotated_members(
        raw: bytes, source_start_line: int, parent_index: int | None,
        conditions: tuple[str, ...], visibility: str) \
        -> tuple[TypeMember, ...]:
    """Reparse declarations after blanking unexpanded ``__annotations``.

    Keeping byte and newline positions stable lets the ordinary aggregate
    parser recover names, arrays and callbacks even when tree-sitter split one
    declaration into several fields (or glued several declarations together).
    The original source spelling and annotations are restored on each recovered
    root declaration. Nested members retain their independently parsed text.
    """
    sanitized = bytearray(raw)
    annotations: list[tuple[str, int]] = []
    pattern = re.compile(rb"\b([A-Za-z_]\w*)")
    cursor = 0
    while match := pattern.search(raw, cursor):
        name = match.group(1).decode("ascii")
        end = match.end()
        after = end
        while after < len(raw) and raw[after:after + 1] in b" \t\r\n":
            after += 1
        call_end = None
        if raw[after:after + 1] == b"(":
            call_end = _matching_delimiter(raw, after, ord("("), ord(")"))
        recognized = (name in _ATTRIBUTE_MACROS
                      or (name.startswith("__") and call_end is not None))
        if not recognized:
            cursor = end
            continue
        consumes_call = call_end is not None and (
            name not in _ATTRIBUTE_MACROS or name in _CALL_ATTRIBUTE_MACROS)
        annotation_end = call_end + 1 if consumes_call else end
        annotations.append((
            _squash(
                raw[match.start():annotation_end].decode("utf-8", "replace"),
                MAX_MEMBER_DECLARATION,
            ),
            raw.count(b"\n", 0, match.start()),
        ))
        for index in range(match.start(), annotation_end):
            if sanitized[index] not in (ord("\n"), ord("\r")):
                sanitized[index] = ord(" ")
        cursor = annotation_end

    if not annotations:
        return ()
    wrapper = b"struct __kernel_atlas_annotated {\n" + bytes(sanitized) + b"\n};\n"
    parsed = [
        symbol for symbol in parse_source(wrapper, frozenset({STRUCT}))
        if symbol.name == "__kernel_atlas_annotated"
    ]
    if not parsed or not parsed[0].members or not parsed[0].parse_complete:
        return ()
    recovered = parsed[0]
    raw_lines = raw.decode("utf-8", "replace").splitlines()
    root_indexes = [
        index for index, member in enumerate(recovered.members)
        if member.parent_index is None
    ]
    root_ranges = {
        index: (max(0, recovered.members[index].start_line - 2),
                max(0, recovered.members[index].end_line - 2))
        for index in root_indexes
    }
    root_annotations: dict[int, list[tuple[str, int]]] = {
        index: [] for index in root_indexes
    }
    for annotation in annotations:
        line = annotation[1]
        owner = next((
            index for index in root_indexes
            if root_ranges[index][0] <= line <= root_ranges[index][1]
        ), None)
        if owner is None:
            owner = next((index for index in root_indexes
                          if root_ranges[index][0] >= line), root_indexes[-1])
        root_annotations[owner].append(annotation)

    for index, member in enumerate(recovered.members):
        if member.parent_index is None:
            local_start = max(0, member.start_line - 2)
            local_end = max(local_start + 1, member.end_line - 1)
            assigned = root_annotations.get(index, [])
            if assigned:
                local_start = min(local_start, min(
                    line for _, line in assigned))
            original = "\n".join(raw_lines[local_start:local_end]).strip()
            member.declaration = _squash(
                original or raw.decode("utf-8", "replace"),
                MAX_MEMBER_DECLARATION,
            )
            annotation_text = " ".join(dict.fromkeys(
                text for text, _ in assigned))
            if member.type_text and annotation_text:
                member.type_text = _squash(
                    f"{member.type_text} {annotation_text}",
                    MAX_MEMBER_DECLARATION,
                )
        member.start_line += source_start_line - 2
        member.end_line += source_start_line - 2
        member.conditions = tuple(dict.fromkeys(
            (*conditions, *member.conditions)))
        if member.visibility == "unspecified":
            member.visibility = visibility
    return recovered.members


def _member_path(members: list[TypeMember], index: int) -> str:
    names: list[str] = []
    current: int | None = index
    while current is not None:
        member = members[current]
        if member.name:
            names.append(member.name)
        current = member.parent_index
    return ".".join(reversed(names))


def _normalize_doc_key(key: str) -> str:
    key = re.sub(r"\[[^]]*\]", "", key.strip())
    return key.lstrip(".")


def _preprocessor_context(src: bytes, node) -> tuple[str, ...]:
    directives: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in {
                "preproc_if", "preproc_ifdef", "preproc_elif",
                "preproc_else", "preproc_ifndef"}:
            directives.append(_squash(
                _text(src, current).splitlines()[0].strip(), 240))
        current = current.parent
    return tuple(reversed(directives))


def _comment_before_offset(
        src: bytes, offset: int) -> tuple[str | None, str | None, str | None]:
    raw = (_adjacent_comment_raw(src, offset, kernel_doc=True)
           or _adjacent_comment_raw(src, offset))
    if raw is None:
        return None, None, None
    if _is_visibility_marker(raw):
        return None, None, None
    return _comment_description(raw)


def _struct_group_calls(src: bytes, node) -> list[dict]:
    """Balanced source ranges for the kernel's mirrored struct-group macros."""
    body = node.child_by_field_name("body")
    if body is None:
        return []
    pattern = re.compile(
        rb"\b(__struct_group|struct_group(?:_attr|_tagged)?)\s*\(")
    groups: list[dict] = []
    for match in pattern.finditer(src, body.start_byte, body.end_byte):
        if any(group["start_byte"] < match.start() < group["end_byte"]
               for group in groups):
            continue
        leaf = node.descendant_for_byte_range(match.start(), match.start() + 1)
        cur = leaf
        ignored = False
        while cur is not None and cur is not node:
            if cur.type in {
                    "comment", "string_literal", "char_literal",
                    "preproc_def", "preproc_function_def"}:
                ignored = True
                break
            cur = cur.parent
        if ignored:
            continue
        opening = match.end() - 1
        closing = _matching_delimiter(src, opening, ord("("), ord(")"))
        if closing is None or closing >= body.end_byte:
            continue
        invocation_end = closing + 1
        while invocation_end < body.end_byte \
                and src[invocation_end:invocation_end + 1] in b" \t\r\n":
            invocation_end += 1
        if src[invocation_end:invocation_end + 1] == b";":
            invocation_end += 1
        raw_bytes = src[match.start():invocation_end]
        raw = raw_bytes.decode("utf-8", "replace")
        local_open = opening - match.start()
        local_close = closing - match.start()
        arg_ranges = _macro_arg_byte_ranges(raw_bytes, local_open, local_close)
        args = [raw_bytes[start:end].decode("utf-8", "replace").strip()
                for start, end in arg_ranges]
        macro = match.group(1).decode("ascii")
        tag = None
        if macro == "struct_group":
            name_index = 0
            members_index = 1
        elif macro in {"struct_group_attr", "struct_group_tagged"}:
            name_index = 0 if macro == "struct_group_attr" else 1
            members_index = 2
            if macro == "struct_group_tagged" and args:
                candidate = _strip_c_comments(args[0]).strip()
                tag = candidate if candidate.isidentifier() else None
        else:
            name_index = 1
            members_index = 3
            if args:
                candidate = _strip_c_comments(args[0]).strip()
                tag = candidate if candidate.isidentifier() else None
        name = None
        if name_index < len(args):
            candidate = _strip_c_comments(args[name_index]).strip()
            name = candidate if candidate.isidentifier() else None
        members_start = (arg_ranges[members_index][0]
                         if members_index < len(arg_ranges) else local_close)
        groups.append({
            "macro": macro, "name": name, "tag": tag,
            "attributes": (args[2].strip()
                           if macro == "__struct_group" and len(args) > 2
                           else None),
            "start_byte": match.start(), "end_byte": invocation_end,
            "start_line": src.count(b"\n", 0, match.start()) + 1,
            "end_line": src.count(b"\n", 0, invocation_end - 1) + 1,
            "declaration": _squash(raw, MAX_MEMBER_DECLARATION),
            "members_source": raw_bytes[members_start:local_close],
            "members_start_line": (
                src.count(b"\n", 0, match.start() + members_start) + 1),
            "conditions": _preprocessor_context(src, leaf),
            "leading_comment": _comment_before_offset(src, match.start()),
            "leading_comment_raw": (
                _adjacent_comment_raw(src, match.start(), kernel_doc=True)
                or _adjacent_comment_raw(src, match.start())),
        })
    return groups


def _apply_struct_groups(src: bytes, node, members: list[TypeMember],
                         member_ranges: list[tuple[int, int]]) \
        -> tuple[list[TypeMember], list[tuple[int, int]], list[str]]:
    """Reparent recovered group fields below one semantic macro container.

    Tree-sitter exposes most declarations inside ``struct_group()`` as if they
    were direct fields, then recovers at the closing parenthesis.  The macro
    actually creates a mirrored anonymous/named aggregate.  Represent one
    non-duplicated group node and place the recovered declarations beneath it.
    """
    groups = _struct_group_calls(src, node)
    if not groups:
        return members, [], []

    nodes = [
        {"member": member, "children": [], "range": source_range}
        for member, source_range in zip(members, member_ranges)
    ]
    roots: list[dict] = []
    for index, item in enumerate(nodes):
        parent = members[index].parent_index
        (roots if parent is None else nodes[parent]["children"]).append(item)

    def parsed_group_children(group: dict) -> tuple[list[dict], list[str]]:
        wrapper = (b"struct __kernel_atlas_group {\n"
                   + group["members_source"] + b"\n};\n")
        parsed = [
            symbol for symbol in parse_source(wrapper, frozenset({STRUCT}))
            if symbol.name == "__kernel_atlas_group"
        ]
        if not parsed:
            return [], [
                f"could not parse members of {group['macro']} at line "
                f"{group['start_line']}"
            ]
        symbol = parsed[0]
        offset = group["members_start_line"] - 2
        parsed_nodes = [
            {
                "member": member, "children": [],
                "range": (group["start_byte"], group["end_byte"]),
            }
            for member in symbol.members
        ]
        parsed_roots: list[dict] = []
        outer_conditions = tuple(group["conditions"])
        for index, item in enumerate(parsed_nodes):
            member = item["member"]
            member.start_line += offset
            member.end_line += offset
            member.conditions = tuple(dict.fromkeys(
                (*outer_conditions, *member.conditions)))
            parent_index = symbol.members[index].parent_index
            (parsed_roots if parent_index is None
             else parsed_nodes[parent_index]["children"]).append(item)
        parsed_warnings = [
            f"{group['macro']} at line {group['start_line']}: {warning}"
            for warning in symbol.parse_warnings
        ]
        return parsed_roots, parsed_warnings

    warnings: list[str] = []
    for group in groups:
        containers = [
            item for item in nodes
            if item["member"].kind in {"struct", "union", "struct_group"}
            and item["range"][0] <= group["start_byte"]
            and item["range"][1] >= group["end_byte"]
        ]
        parent = min(
            containers,
            key=lambda item: item["range"][1] - item["range"][0],
            default=None,
        )
        siblings = roots if parent is None else parent["children"]
        contained = [
            item for item in siblings
            if item["range"][0] >= group["start_byte"]
            and item["range"][1] <= group["end_byte"]
        ]
        positions = [siblings.index(item) for item in contained]
        insertion = (min(positions) if positions else next(
            (index for index, item in enumerate(siblings)
             if item["range"][0] > group["start_byte"]),
            len(siblings),
        ))
        for item in contained:
            siblings.remove(item)
        recovered, recovered_warnings = parsed_group_children(group)
        warnings.extend(recovered_warnings)
        if recovered:
            contained = recovered
            nodes.extend(recovered)
        type_text = (f"struct {group['tag']}" if group["tag"]
                     else "mirrored anonymous/named struct group")
        group_comment = group["leading_comment"]
        group_visibility = (
            siblings[insertion - 1]["member"].visibility
            if insertion > 0 else "unspecified"
        )
        group_member = TypeMember(
            parent_index=None, name=group["name"], kind="struct_group",
            type_text=type_text, declaration=group["declaration"],
            start_line=group["start_line"], end_line=group["end_line"],
            description=(group_comment[0]
                         or "Mirrored member group; its children are accessible "
                            "directly and through the named group."),
            description_source=(group_comment[1] or "macro-semantics"),
            conditions=(tuple(group["conditions"])
                        or (contained[0]["member"].conditions
                            if contained else ())),
            visibility=group_visibility,
            is_anonymous=group["name"] is None,
            generated_by=group["macro"],
        )
        group_node = {
            "member": group_member, "children": contained,
            "range": (group["start_byte"], group["end_byte"]),
        }
        siblings.insert(insertion, group_node)
        nodes.append(group_node)
        if not contained:
            warnings.append(
                f"{group['macro']} at line {group['start_line']} had no "
                "recoverable child declarations")

    flattened: list[TypeMember] = []

    def flatten(items: list[dict], parent_index: int | None) -> None:
        for item in items:
            index = len(flattened)
            member = item["member"]
            member.parent_index = parent_index
            flattened.append(member)
            flatten(item["children"], index)

    flatten(roots, None)
    ranges = [(group["start_byte"], group["end_byte"]) for group in groups]
    return flattened, ranges, warnings


def _generated_struct_group_symbols(
        src: bytes, node, outer_doc: _KernelDoc) -> list[Symbol]:
    """Materialize reusable tags declared by ``struct_group_tagged``.

    The tag is not merely documentation: kernel code instantiates and takes
    ``sizeof`` of these generated structures.  Tree-sitter cannot see the
    expansion, so build the tag's member list from the macro's balanced source
    fragment while retaining the real invocation span and surrounding docs.
    """
    generated: list[Symbol] = []
    for group in _struct_group_calls(src, node):
        tag = group["tag"]
        if tag is None:
            continue
        wrapper = (f"struct {tag} {{\n".encode()
                   + group["members_source"] + b"\n};\n")
        parsed = [
            symbol for symbol in parse_source(wrapper, frozenset({STRUCT}))
            if symbol.name == tag
        ]
        if not parsed:
            generated.append(Symbol(
                name=tag, kind=STRUCT,
                start_line=group["start_line"], end_line=group["end_line"],
                signature=f"struct {tag} {{ 0 members }}",
                parse_complete=False,
                parse_warnings=(
                    f"could not parse members generated by {group['macro']}",
                ),
                conditions=tuple(group["conditions"]),
            ))
            continue

        recovered = parsed[0]
        line_offset = group["members_start_line"] - 2
        outer_conditions = tuple(group["conditions"])
        for member in recovered.members:
            member.start_line += line_offset
            member.end_line += line_offset
            member.conditions = tuple(dict.fromkeys(
                (*outer_conditions, *member.conditions)))

        raw_comment = group["leading_comment_raw"]
        structured = (_parse_aggregate_doc(raw_comment, {tag})
                      if raw_comment else _KernelDoc(None, None, {}))
        if structured.summary is None and structured.description is None:
            summary = group["leading_comment"][0]
        else:
            summary = structured.summary
        matched: set[str] = set()
        normalized_docs = {
            _normalize_doc_key(key): (key, value)
            for key, value in structured.members.items()
        }
        outer_docs = {
            _normalize_doc_key(key): (key, value)
            for key, value in outer_doc.members.items()
        }
        mutable_members = list(recovered.members)
        for index, member in enumerate(mutable_members):
            path = _normalize_doc_key(_member_path(mutable_members, index))
            candidates = [path]
            if member.name:
                candidates.append(_normalize_doc_key(member.name))
            found = next((normalized_docs[key] for key in candidates
                          if key in normalized_docs), None)
            source = "source-comment"
            if found is None:
                found = next((outer_docs[key] for key in candidates
                              if key in outer_docs), None)
                source = outer_doc.source
            if found is None:
                continue
            original, value = found
            if source == "source-comment":
                matched.add(original)
            if member.description_source != "inline-kernel-doc":
                member.description = value
                member.description_source = source
        unmatched = tuple(
            (key, value) for key, value in structured.members.items()
            if key not in matched
        )
        warnings = list(recovered.parse_warnings)
        if unmatched:
            warnings.append(
                f"{len(unmatched)} documented member(s) could not be matched "
                "to generated fields")
        direct = sum(member.parent_index is None
                     for member in recovered.members)
        attributes = group["attributes"]
        attribute_text = f" {attributes}" if attributes else ""
        generated.append(Symbol(
            name=tag, kind=STRUCT,
            start_line=group["start_line"], end_line=group["end_line"],
            signature=(f"struct {tag}{attribute_text} {{ {direct} "
                       f"member{'s' if direct != 1 else ''} }}"),
            summary=summary, description=structured.description,
            members=recovered.members,
            parse_complete=not warnings, parse_warnings=tuple(warnings),
            unmatched_member_docs=unmatched, conditions=outer_conditions,
        ))
    return generated


def _aggregate_members(src: bytes, node, doc: _KernelDoc,
                       initial_conditions: tuple[str, ...] = ()) \
        -> tuple[tuple[TypeMember, ...], bool, tuple[str, ...],
                 tuple[tuple[str, str], ...]]:
    """Extract a preorder member tree while retaining uncertain syntax."""
    body = node.child_by_field_name("body")
    if body is None:
        return (), False, ("aggregate body was not parsed",), ()

    members: list[TypeMember] = []
    member_ranges: list[tuple[int, int]] = []
    warnings: list[str] = []
    handled_recovery_ranges: list[tuple[int, int]] = []
    consumed_member_macro_ranges: list[tuple[int, int]] = []

    def append_member(member: TypeMember,
                      leading: tuple[str | None, str | None, str | None], field,
                      local: tuple[str | None, str | None, str | None] =
                      (None, None, None), *, allow_trailing: bool = True,
                      effective_end_byte: int | None = None) -> int:
        range_end = (field.end_byte if effective_end_byte is None
                     else effective_end_byte)
        trailing = (_trailing_member_comment(src, field, range_end)
                    if allow_trailing else (None, None, None))
        comment = leading if leading[0] else (local if local[0] else trailing)
        description, source, key = comment
        if key is not None:
            normalized = _normalize_doc_key(key).rsplit(".", 1)[-1]
            if member.name is None or normalized != member.name:
                description = source = None
        if description is not None:
            member.description = description
            member.description_source = source
        members.append(member)
        member_ranges.append((field.start_byte, range_end))
        return len(members) - 1

    def parse_conditional_member_macro(
            field, parent_index: int | None, conditions: tuple[str, ...],
            visibility: str,
            leading: tuple[str | None, str | None, str | None]) -> bool:
        """Model a macro which contributes an anonymous aggregate of fields.

        ``__SYSFS_FUNCTION_ALTERNATIVE`` expands to a struct under CONFIG_CFI
        and a union otherwise.  Both spellings expose every member argument;
        only their layout changes.  A neutral macro container therefore keeps
        the source possibilities honest while its parsed children remain
        searchable and documentable.
        """
        head = _text(src, field)
        match = re.search(r"\b(__SYSFS_FUNCTION_ALTERNATIVE)\s*\(", head)
        if match is None:
            return False
        opening = field.start_byte + match.end() - 1
        closing = _matching_delimiter(src, opening, ord("("), ord(")"))
        body = node.child_by_field_name("body")
        if closing is None or body is None or closing >= body.end_byte:
            return False
        invocation_end = closing + 1
        while invocation_end < body.end_byte \
                and src[invocation_end:invocation_end + 1] in b" \t\r\n":
            invocation_end += 1
        if src[invocation_end:invocation_end + 1] == b";":
            invocation_end += 1

        fragment = src[opening + 1:closing]
        wrapper = b"union __kernel_atlas_macro {\n" + fragment + b"\n};\n"
        parsed = [
            symbol for symbol in parse_source(wrapper, frozenset({UNION}))
            if symbol.name == "__kernel_atlas_macro"
        ]
        if not parsed:
            return False
        recovered = parsed[0]
        raw = src[field.start_byte:invocation_end].decode("utf-8", "replace")
        start = field.start_point[0] + 1
        end = src.count(b"\n", 0, max(field.start_byte, invocation_end - 1)) + 1
        macro = match.group(1)
        container = TypeMember(
            parent_index=parent_index, name=None, kind="macro",
            type_text=("anonymous struct with CONFIG_CFI; anonymous union "
                       "otherwise"),
            declaration=_squash(raw, MAX_MEMBER_DECLARATION),
            start_line=start, end_line=end,
            description=("Conditional anonymous aggregate: its members "
                         "coexist under CONFIG_CFI and overlap otherwise."),
            description_source="macro-semantics", conditions=conditions,
            visibility=visibility, is_anonymous=True, generated_by=macro,
        )
        container_index = append_member(
            container, leading, field, allow_trailing=False,
            effective_end_byte=invocation_end)
        child_base = len(members)
        line_offset = src.count(b"\n", 0, opening + 1) + 1 - 2
        for child in recovered.members:
            old_parent = child.parent_index
            child.parent_index = (container_index if old_parent is None
                                  else child_base + old_parent)
            child.start_line += line_offset
            child.end_line += line_offset
            child.conditions = tuple(dict.fromkeys(
                (*conditions, *child.conditions)))
            child.visibility = (visibility if child.visibility == "unspecified"
                                else child.visibility)
            members.append(child)
            member_ranges.append((field.start_byte, invocation_end))
        handled_recovery_ranges.append((field.start_byte, invocation_end))
        consumed_member_macro_ranges.append((field.start_byte, invocation_end))
        warnings.extend(
            f"{macro} at line {start}: {warning}"
            for warning in recovered.parse_warnings
        )
        return True

    def parse_field(field, parent_index: int | None,
                    conditions: tuple[str, ...], visibility: str,
                    leading: tuple[str | None, str | None, str | None],
                    effective_end_byte: int | None = None,
                    annotated_members: tuple[TypeMember, ...] = ()) -> None:
        raw_end = field.end_byte if effective_end_byte is None else effective_end_byte
        raw = src[field.start_byte:raw_end].decode("utf-8", "replace")
        annotation = src[field.end_byte:raw_end].decode(
            "utf-8", "replace").strip()
        annotation = annotation[:-1].rstrip() if annotation.endswith(";") else annotation

        if parse_conditional_member_macro(
                field, parent_index, conditions, visibility, leading):
            return
        macro_member = _macro_member(
            src, field, parent_index, conditions, visibility)
        if macro_member is not None:
            if raw_end != field.end_byte:
                macro_member.declaration = _squash(raw, MAX_MEMBER_DECLARATION)
            append_member(
                macro_member, leading, field, effective_end_byte=raw_end)
            if macro_member.generated_by == "__cacheline_group_end_aligned":
                group = macro_member.name.removeprefix(
                    "__cacheline_group_end__")
                append_member(TypeMember(
                    parent_index=parent_index,
                    name=f"__cacheline_group_pad__{group}", kind="struct",
                    type_text="empty aligned struct",
                    declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                    start_line=macro_member.start_line,
                    end_line=macro_member.end_line,
                    description=("Padding generated after the aligned "
                                 f"cacheline group {group}."),
                    description_source="macro-semantics",
                    conditions=conditions, visibility=visibility,
                    generated_by=macro_member.generated_by,
                ), (None, None, None), field,
                    effective_end_byte=raw_end)
            handled_recovery_ranges.append((field.start_byte, raw_end))
            return

        direct_names = []
        for declarator in field.children_by_field_name("declarator"):
            direct_name = _declarator_name(declarator)
            if direct_name is not None:
                direct_names.append(_text(src, direct_name).strip())
        suspect_attribute_name = bool(direct_names) and all(
            name in _ATTRIBUTE_MACROS for name in direct_names)
        if not annotated_members and (
                suspect_attribute_name or (field.has_error and not direct_names)):
            annotated_members = _recover_annotated_members(
                src[field.start_byte:raw_end], field.start_point[0] + 1,
                parent_index, conditions, visibility)
        if annotated_members:
            base_index = len(members)
            root_indexes = [
                index for index, member in enumerate(annotated_members)
                if member.parent_index is None
            ]
            for index, member in enumerate(annotated_members):
                old_parent = member.parent_index
                member.parent_index = (parent_index if old_parent is None
                                       else base_index + old_parent)
                if old_parent is None:
                    append_member(
                        member,
                        leading if index == root_indexes[0]
                        else (None, None, None),
                        field,
                        allow_trailing=index == root_indexes[-1],
                        effective_end_byte=raw_end,
                    )
                else:
                    members.append(member)
                    member_ranges.append((field.start_byte, raw_end))
            handled_recovery_ranges.append((field.start_byte, raw_end))
            return

        type_node = field.child_by_field_name("type")
        nested_kind = None
        nested_body = None
        nested_tag = ""
        if type_node is not None and type_node.type in {
                "struct_specifier", "union_specifier"}:
            nested_kind = ("struct" if type_node.type == "struct_specifier"
                           else "union")
            nested_body = type_node.child_by_field_name("body")
            nested_name = type_node.child_by_field_name("name")
            nested_tag = (_text(src, nested_name).strip()
                          if nested_name is not None else "")
            if nested_tag in _ATTRIBUTE_MACROS:
                nested_tag = ""

        direct_declarators = list(field.children_by_field_name("declarator"))
        declarators = _field_declarators(src, field)
        if declarators != direct_declarators:
            handled_recovery_ranges.append((field.start_byte, field.end_byte))
        error_words = [
            re.findall(r"[A-Za-z_]\w*", _text(src, child))
            for child in field.named_children if child.type == "ERROR"
        ]
        if error_words and all(
                words and all(word in _ATTRIBUTE_MACROS or word.startswith("__")
                              for word in words)
                for words in error_words):
            handled_recovery_ranges.append((field.start_byte, field.end_byte))
        base = _base_member_type(src, field, type_node)
        start = field.start_point[0] + 1
        end = src.count(b"\n", 0, max(field.start_byte, raw_end - 1)) + 1

        def with_annotation(type_text: str | None) -> str | None:
            if not annotation:
                return type_text
            return _squash(
                f"{type_text or ''} {annotation}", MAX_MEMBER_DECLARATION,
            ) or None

        if nested_body is not None:
            # ``struct named { ... };`` declares a tag but no storage member.
            if not declarators and nested_tag:
                return
            aggregate_declarators = declarators or [None]
            for pos, declarator in enumerate(aggregate_declarators):
                name_node = (_declarator_name(declarator)
                             if declarator is not None else None)
                name = (_text(src, name_node).strip()
                        if name_node is not None
                        and not getattr(name_node, "is_missing", False)
                        else None)
                if not name or name in _ATTRIBUTE_MACROS:
                    name = None
                declaration = _squash(raw, MAX_MEMBER_DECLARATION)
                type_text = (with_annotation(_declarator_type(src, base, declarator))
                             if declarator is not None else base)
                container = TypeMember(
                    parent_index=parent_index, name=name, kind=nested_kind,
                    type_text=type_text, declaration=declaration,
                    start_line=start, end_line=end,
                    array_dimensions=(_array_dimensions(src, declarator)
                                      if declarator is not None else ()),
                    conditions=conditions, visibility=visibility,
                    is_anonymous=name is None,
                )
                index = append_member(
                    container,
                    leading if pos == 0 else (None, None, None), field,
                    allow_trailing=pos == len(aggregate_declarators) - 1,
                    effective_end_byte=raw_end)
                walk(nested_body, index, conditions, visibility)
            return

        if declarators:
            for pos, declarator in enumerate(declarators):
                name_node = _declarator_name(declarator)
                name = (_text(src, name_node).strip()
                        if name_node is not None
                        and not getattr(name_node, "is_missing", False)
                        else None)
                if not name:
                    name = None
                next_declarator = (declarators[pos + 1]
                                   if pos + 1 < len(declarators) else None)
                bit_width = _bitfield_for(
                    src, field, declarator, next_declarator)
                if name is None and bit_width is not None:
                    kind = "unnamed_bitfield"
                else:
                    kind = "function_pointer" if _is_function_pointer(
                        declarator) else "field"
                member = TypeMember(
                    parent_index=parent_index, name=name, kind=kind,
                    type_text=with_annotation(
                        _declarator_type(src, base, declarator)),
                    declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                    start_line=start, end_line=end, bit_width=bit_width,
                    array_dimensions=_array_dimensions(src, declarator),
                    conditions=conditions, visibility=visibility,
                    is_anonymous=name is None,
                )
                local = _declarator_comment(
                    src, declarator, next_declarator)
                append_member(
                    member,
                    leading if pos == 0 else (None, None, None), field,
                    local,
                    allow_trailing=pos == len(declarators) - 1,
                    effective_end_byte=raw_end)
            return

        clauses = [child for child in field.named_children
                   if child.type == "bitfield_clause"]
        if clauses:
            for clause in clauses:
                width = _text(src, clause).strip()
                if width.startswith(":"):
                    width = width[1:].strip()
                append_member(TypeMember(
                    parent_index=parent_index, name=None,
                    kind="unnamed_bitfield", type_text=with_annotation(base),
                    declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                    start_line=start, end_line=end, bit_width=width,
                    conditions=conditions, visibility=visibility,
                    is_anonymous=True,
                ), leading, field,
                   allow_trailing=clause is clauses[-1],
                   effective_end_byte=raw_end)
            return

        # GCC's anonymous-field extension permits a previously declared tag
        # to be promoted with ``struct tag;`` / ``union tag;``.  It has no C
        # member identifier, but it is real storage rather than a forward
        # declaration.  All-caps tags here are normally unexpanded type macros
        # and remain in conservative recovery instead.
        if nested_kind is not None and nested_tag \
                and not re.fullmatch(r"[A-Z][A-Z0-9_]*", nested_tag):
            append_member(TypeMember(
                parent_index=parent_index, name=None, kind=nested_kind,
                type_text=f"{nested_kind} {nested_tag}",
                declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                start_line=start, end_line=end, conditions=conditions,
                visibility=visibility, is_anonymous=True,
                generated_by="anonymous-tag-member",
            ), leading, field, effective_end_byte=raw_end)
            return

        shaped = re.search(r"\b([A-Za-z_]\w*)\s*\(", raw)
        lone_macro = re.fullmatch(
            r"\s*([A-Za-z_]\w*)\s*;\s*", raw)
        type_is_macro = type_node is not None \
            and type_node.type == "macro_type_specifier"
        if (shaped is not None and type_is_macro) or lone_macro is not None:
            macro = (shaped.group(1) if shaped is not None
                     else lone_macro.group(1))
            if macro in {
                    "struct_group", "struct_group_attr",
                    "struct_group_tagged", "__struct_group"}:
                return
            args = (_split_macro_args(raw, shaped.end() - 1)
                    if shaped is not None else None)
            possible_name = None
            if macro == "__bpf_md_ptr" and args and len(args) > 1:
                possible_name = args[1].strip()
            elif macro == "__ETHTOOL_DECLARE_LINK_MODE_MASK" and args:
                possible_name = args[0].strip()
            if possible_name is not None and not possible_name.isidentifier():
                possible_name = None
            append_member(TypeMember(
                parent_index=parent_index, name=possible_name, kind="macro",
                type_text=None,
                declaration=_squash(raw, MAX_MEMBER_DECLARATION),
                start_line=start, end_line=end, conditions=conditions,
                visibility=visibility, generated_by=macro,
            ), leading, field, effective_end_byte=raw_end)
            warnings.append(
                f"member macro {macro} was preserved without expanding it")
            handled_recovery_ranges.append((field.start_byte, raw_end))
            return

        if raw.strip(" ;\t\r\n"):
            warnings.append(
                f"could not identify a member declared at line {start}")

    def directive(current) -> str:
        first = _text(src, current).splitlines()[0].strip()
        return _squash(first, 240)

    def walk(current, parent_index: int | None,
             conditions: tuple[str, ...], inherited_visibility: str) -> str:
        visibility = inherited_visibility
        pending: tuple[str | None, str | None, str | None] = (None, None, None)
        last_field_end_row: int | None = None
        children = list(current.named_children)
        skipped: set[tuple[int, int]] = set()
        for position, child in enumerate(children):
            child_range = (child.start_byte, child.end_byte)
            if child_range in skipped or any(
                    start < child.start_byte < end
                    for start, end in consumed_member_macro_ranges):
                continue
            if child.type == "comment":
                marker = re.fullmatch(
                    r"\s*/\*+\s*(private|public)\s*:[\s\S]*?\*/\s*",
                    _text(src, child), re.IGNORECASE)
                if marker is not None:
                    visibility = marker.group(1).lower()
                    pending = (None, None, None)
                elif last_field_end_row != child.start_point[0]:
                    pending = _comment_description(_text(src, child))
                continue
            if child.type == "field_declaration":
                effective_end = None
                annotated_members: tuple[TypeMember, ...] = ()
                missing_semicolon = any(
                    part.type == ";" and getattr(part, "is_missing", False)
                    for part in child.children
                )
                if missing_semicolon:
                    previous_end = child.end_byte
                    consumed: list = []
                    for following in children[position + 1:position + 5]:
                        if following.type != "field_declaration" \
                                or src[previous_end:following.start_byte].strip():
                            break
                        consumed.append(following)
                        candidate = _recover_annotated_members(
                            src[child.start_byte:following.end_byte],
                            child.start_point[0] + 1, parent_index,
                            conditions, visibility)
                        if candidate:
                            effective_end = following.end_byte
                            annotated_members = candidate
                            skipped.update(
                                (item.start_byte, item.end_byte)
                                for item in consumed)
                            break
                        has_missing = any(
                            part.type == ";" and getattr(
                                part, "is_missing", False)
                            for part in following.children
                        )
                        if not has_missing:
                            break
                        previous_end = following.end_byte
                parse_field(
                    child, parent_index, conditions, visibility, pending,
                    effective_end, annotated_members)
                pending = (None, None, None)
                last_field_end_row = child.end_point[0]
                continue
            if child.type.startswith("preproc_"):
                visibility = walk(
                    child, parent_index, (*conditions, directive(child)), visibility)
                pending = (None, None, None)
        return visibility

    walk(body, None, initial_conditions, "unspecified")
    members, group_ranges, group_warnings = _apply_struct_groups(
        src, node, members, member_ranges)
    handled_recovery_ranges.extend(group_ranges)
    warnings.extend(group_warnings)
    if group_ranges:
        group_line_ranges = [
            (src.count(b"\n", 0, start) + 1,
             src.count(b"\n", 0, max(start, end - 1)) + 1)
            for start, end in group_ranges
        ]
        warnings = [
            warning for warning in warnings
            if not (
                (match := re.fullmatch(
                    r"could not identify a member declared at line (\d+)",
                    warning,
                ))
                and any(first <= int(match.group(1)) <= last
                        for first, last in group_line_ranges)
            )
        ]

    matched: set[str] = set()
    normalized_docs = {
        _normalize_doc_key(key): (key, value)
        for key, value in doc.members.items()
    }
    for index, member in enumerate(members):
        path = _normalize_doc_key(_member_path(members, index))
        candidates = [path]
        if member.name:
            candidates.append(_normalize_doc_key(member.name))
        elif member.kind in {"struct", "union"} and member.type_text:
            tagged = re.fullmatch(
                r"(?:struct|union)\s+([A-Za-z_]\w*)", member.type_text)
            if tagged is not None:
                candidates.append(_normalize_doc_key(tagged.group(1)))
        found = next((normalized_docs[key] for key in candidates
                      if key in normalized_docs), None)
        if found is None:
            continue
        original, value = found
        matched.add(original)
        # A field-local kernel-doc block is more specific than the aggregate's
        # parameter list.  Ordinary comments remain a fallback.
        if member.description_source != "inline-kernel-doc":
            member.description = value
            member.description_source = doc.source

    unmatched = tuple((key, value) for key, value in doc.members.items()
                      if key not in matched)
    if unmatched:
        warnings.append(
            f"{len(unmatched)} documented member(s) could not be matched to parsed fields")
    warnings = list(dict.fromkeys(warnings))
    stack = [body]
    uncovered_recovery = False
    while stack:
        current = stack.pop()
        if current.type == "ERROR" and not any(
                start <= current.start_byte and current.end_byte <= end
                for start, end in handled_recovery_ranges):
            uncovered_recovery = True
            break
        stack.extend(current.named_children)
    if uncovered_recovery:
        warnings.append("tree-sitter reported recovery inside this declaration")
    complete = not warnings
    return tuple(members), complete, tuple(warnings), unmatched


_BPMP_EMPTY_AGGREGATE_RE = re.compile(
    rb"(?m)^(?P<kind>struct|union)[ \t]+(?P<name>[A-Za-z_]\w*)"
    rb"[ \t\r\n]*\{[ \t\r\n]*(?P<member>BPMP_ABI_EMPTY)"
    rb"[ \t\r\n]*\}[ \t]*(?P<attribute>BPMP_ABI_PACKED)[ \t]*;"
)


def _recover_bpmp_empty_aggregates(src: bytes, root) -> tuple[Symbol, ...]:
    """Recover the exact BPMP empty-payload form lost inside a root ERROR."""
    recovered: list[Symbol] = []
    for match in _BPMP_EMPTY_AGGREGATE_RE.finditer(src):
        leaf = _source_code_leaf(root, match.start())
        if leaf is None or not _is_file_scope(src, leaf):
            continue
        kind = match.group("kind").decode("ascii")
        name = match.group("name").decode("ascii")
        definition_conditions = _preprocessor_context(src, leaf)
        start_line = src.count(b"\n", 0, match.start()) + 1
        end_line = src.count(b"\n", 0, match.end()) + 1
        member_line = src.count(b"\n", 0, match.start("member")) + 1
        doc = _KernelDoc(None, None, {})
        for kernel_doc, source in ((True, "kernel-doc"),
                                   (False, "source-comment")):
            raw_doc = _adjacent_comment_raw(
                src, match.start(), kernel_doc=kernel_doc)
            if raw_doc is None:
                continue
            candidate = _parse_aggregate_doc(raw_doc, {name}, source)
            if candidate.summary or candidate.description or candidate.members:
                doc = candidate
                break
        member_description = doc.members.get("empty")
        member_source = doc.source if member_description is not None else None
        member = _bpmp_empty_member(
            match.group("member").decode("ascii"), member_line, member_line,
            conditions=definition_conditions,
            description=member_description,
            description_source=member_source,
        )
        recovered.append(Symbol(
            name=name, kind=kind, start_line=start_line, end_line=end_line,
            signature=(f"{kind} {name} BPMP_ABI_PACKED {{ 1 member }}"),
            summary=doc.summary, description=doc.description,
            members=(member,), parse_complete=True,
            conditions=definition_conditions,
        ))
    return tuple(recovered)


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


def _following_compound(node):
    """Nearest compound statement following this recovered construct."""
    cur = node
    for _ in range(16):
        if cur is None or cur.type == "translation_unit":
            return None
        nxt = cur.next_named_sibling
        if nxt is not None and nxt.type == "compound_statement":
            return nxt
        cur = cur.parent
    return None


def _head_call_candidates(head: bytes) -> list[tuple[str, int, bytes]]:
    """Top-level ``name(args)`` spellings before the first opening brace."""
    out: list[tuple[str, int, bytes]] = []
    i = 0
    while i < len(head):
        if head.startswith(b"//", i):
            newline = head.find(b"\n", i + 2)
            i = len(head) if newline < 0 else newline + 1
            continue
        if head.startswith(b"/*", i):
            close = head.find(b"*/", i + 2)
            i = len(head) if close < 0 else close + 2
            continue
        if head[i:i + 1] in (b'"', b"'"):
            quote = head[i]
            i += 1
            while i < len(head):
                if head[i] == ord("\\"):
                    i += 2
                elif head[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            continue
        if head[i:i + 1] == b";":
            # Calls before a completed declaration cannot name the function
            # whose body follows later in this recovered head.
            out.clear()
            i += 1
            continue
        if head[i:i + 1] == b"{":
            close = _matching_delimiter(head, i, ord("{"), ord("}"))
            if close is None:
                break
            i = close + 1
            continue
        if not (head[i:i + 1].isalpha() or head[i:i + 1] == b"_"):
            i += 1
            continue

        start = i
        i += 1
        while i < len(head) and (head[i:i + 1].isalnum()
                                 or head[i:i + 1] == b"_"):
            i += 1
        name = head[start:i].decode("ascii")
        opening = i
        while opening < len(head) and head[opening:opening + 1].isspace():
            opening += 1
        if opening >= len(head) or head[opening:opening + 1] != b"(":
            continue

        depth = 1
        j = opening + 1
        quote = 0
        while j < len(head) and depth:
            if quote:
                if head[j] == ord("\\"):
                    j += 2
                    continue
                if head[j] == quote:
                    quote = 0
            elif head.startswith(b"//", j):
                newline = head.find(b"\n", j + 2)
                j = len(head) if newline < 0 else newline
                continue
            elif head.startswith(b"/*", j):
                close = head.find(b"*/", j + 2)
                j = len(head) if close < 0 else close + 1
            elif head[j:j + 1] in (b'"', b"'"):
                quote = head[j]
            elif head[j:j + 1] == b"(":
                depth += 1
            elif head[j:j + 1] == b")":
                depth -= 1
            j += 1
        if depth == 0:
            out.append((name, start, head[opening + 1:j - 1]))
            i = j
    return out


def _recovered_function_name(head: bytes, current: str) \
        -> tuple[str, int] | None:
    """Choose the real declarator after leading annotation/macro calls.

    Error recovery sometimes labels ``__printf(2, 3) real_fn(...)`` as a
    function named ``__printf``.  Real parameter lists and declaration prefixes
    carry type syntax; annotation and registration macro arguments do not.
    """
    best: tuple[int, bool, int, str, int] | None = None
    type_words = re.compile(
        rb"\b(?:void|char|short|int|long|float|double|bool|const|volatile|"
        rb"signed|unsigned|struct|union|enum|[us](?:8|16|32|64)|"
        rb"[A-Za-z_]\w*_t)\b")
    for name, start, args in _head_call_candidates(head):
        parameter_score = 0
        stripped = args.strip()
        if stripped in (b"", b"void"):
            parameter_score += 3
        if b"*" in args or b"..." in args:
            parameter_score += 3
        if type_words.search(args):
            parameter_score += 2
        if re.search(rb"\b[A-Za-z_]\w*\s+[A-Za-z_*]", args):
            parameter_score += 2
        score = parameter_score

        line_start = head.rfind(b"\n", 0, start) + 1
        line_prefix = head[line_start:start].decode("utf-8", "replace")
        if line_prefix and not line_prefix[0].isspace() \
                and _RECOVERED_DECL_PREFIX.fullmatch(line_prefix):
            score += 3
        elif start == line_start and line_start:
            previous_start = head.rfind(b"\n", 0, line_start - 1) + 1
            previous = head[previous_start:line_start - 1] \
                .decode("utf-8", "replace").strip()
            if previous and _RECOVERED_DECL_PREFIX.fullmatch(previous):
                score += 2

        candidate = (score, name == current, parameter_score, name, start)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None or best[0] < 3 or best[2] == 0:
        return None
    return best[3], best[4]


def _matching_delimiter(src: bytes, opening: int,
                        opener: int, closer: int) -> int | None:
    """Closing byte for a balanced C delimiter, ignoring comments/strings."""
    depth = 1
    quote = 0
    i = opening + 1
    while i < len(src):
        if quote:
            if src[i] == ord("\\"):
                i += 2
                continue
            if src[i] == quote:
                quote = 0
        elif src.startswith(b"//", i):
            newline = src.find(b"\n", i + 2)
            i = len(src) if newline < 0 else newline
            continue
        elif src.startswith(b"/*", i):
            close = src.find(b"*/", i + 2)
            i = len(src) if close < 0 else close + 1
        elif src[i] in (ord('"'), ord("'")):
            quote = src[i]
        elif src[i] == opener:
            depth += 1
        elif src[i] == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _canonical_export_follows(src: bytes, offset: int, name: str) -> bool:
    """Whether only whitespace/comments separate an item from its export."""
    trivia = rb"(?:[ \t\r\n]|//[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)*"
    export = (rb"EXPORT(?:_PER_CPU)?_SYMBOL"
              rb"(?:_GPL|_NS|_NS_GPL|_FOR_MODULES)?\s*\(\s*"
              + re.escape(name.encode("ascii")) + rb"\s*(?:,|\))")
    return re.match(trivia + export, src[offset:]) is not None


def _declaration_prefix_words(text: str) -> list[str] | None:
    """Words in a direct declaration prefix, ignoring known annotations."""
    stripped = text.rstrip()
    attrs = "|".join(re.escape(name) for name in _ATTRIBUTE_MACROS)
    stripped = re.sub(
        rf"\b(?:{attrs})\b(?:\s*\([^)]*\))?", " ", stripped)
    if not stripped.strip() or _RECOVERED_DECL_PREFIX.fullmatch(
            stripped.strip()) is None:
        return None
    return re.findall(r"[A-Za-z_]\w*", text)


def _source_exported_symbols(src: bytes, exported: set[str], existing: set[str],
                             want_fn: bool, want_var: bool,
                             call_nodes: list | None = None) -> list[Symbol]:
    """Conservative fallback for literal, exported top-level definitions.

    This is deliberately export-guided: root-level ERROR recovery can erase an
    otherwise ordinary definition, but a canonical export gives us a bounded
    list of names to probe.  A declaration-word prefix plus balanced body is a
    function; a one-line direct declarator with only attributes/initializer is
    a variable.  Macro-generated names have no such source spelling and remain
    omitted.
    """
    out: list[Symbol] = []
    for name in sorted(exported - existing):
        encoded = re.escape(name.encode("ascii"))
        if want_fn:
            pattern = re.compile(
                rb"(?m)^(?P<prefix>[^\n#;{}()]*)\b" + encoded + rb"\s*\(")
            for match in pattern.finditer(src):
                prefix = match.group("prefix").decode("utf-8", "replace")
                definition_start = match.start()
                words = None
                if prefix:
                    if prefix[0].isspace():
                        continue
                    words = _declaration_prefix_words(prefix)
                elif match.start():
                    previous_end = match.start() - 1
                    previous_start = src.rfind(b"\n", 0, previous_end) + 1
                    previous_raw = src[previous_start:previous_end]
                    if not previous_raw or previous_raw[:1].isspace():
                        continue
                    previous = previous_raw.decode("utf-8", "replace")
                    words = _declaration_prefix_words(previous)
                    if words is not None:
                        definition_start = previous_start
                if words is None:
                    continue
                params_end = _matching_delimiter(
                    src, match.end() - 1, ord("("), ord(")"))
                if params_end is None:
                    continue
                opening = params_end + 1
                while opening < len(src) and src[opening:opening + 1].isspace():
                    opening += 1
                conditional_head = False
                if src.startswith(b"#else", opening):
                    endif = re.search(
                        rb"(?m)^#endif[^\n]*(?:\n|$)", src[opening:])
                    if endif is None:
                        continue
                    opening += endif.end()
                    while opening < len(src) \
                            and src[opening:opening + 1].isspace():
                        opening += 1
                    conditional_head = True
                elif src.startswith(b"#endif", opening):
                    endif = re.match(rb"#endif[^\n]*(?:\n|$)", src[opening:])
                    if endif is None:
                        continue
                    opening += endif.end()
                    while opening < len(src) \
                            and src[opening:opening + 1].isspace():
                        opening += 1
                    conditional_head = True
                if opening >= len(src) or src[opening] != ord("{"):
                    continue
                closing = _matching_delimiter(
                    src, opening, ord("{"), ord("}"))
                if closing is None:
                    # Preprocessor alternatives can make raw brace balancing
                    # impossible (two conditional openings, one shared close).
                    # Kernel top-level closing braces are column zero; this
                    # fallback remains constrained to the exact exported name.
                    close_match = re.search(rb"(?m)^}", src[opening + 1:])
                    if close_match is None:
                        continue
                    closing = opening + 1 + close_match.start()
                    if not _canonical_export_follows(src, closing + 1, name):
                        continue
                if not conditional_head and not _canonical_export_follows(
                        src, closing + 1, name):
                    continue
                start = src.count(b"\n", 0, definition_start) + 1
                end = src.count(b"\n", 0, closing) + 1
                calls: tuple[str, ...] = ()
                if call_nodes is not None:
                    calls = tuple(dict.fromkeys(
                        _text(src, node) for node in call_nodes
                        if opening < node.start_byte < closing))
                out.append(Symbol(
                    name=name, kind=FUNCTION, start_line=start, end_line=end,
                    signature=_function_signature(
                        src[definition_start:params_end + 1].decode(
                            "utf-8", "replace")),
                    is_static="static" in words,
                    is_inline=any(word in _INLINE_SPECIFIERS for word in words),
                    is_exported=True, calls=calls,
                ))

        if want_var and not any(symbol.name == name for symbol in out):
            initializer = re.compile(
                rb"(?m)^(?P<prefix>[^\n#;{}(),=]*)\b" + encoded
                + rb"\b(?P<tail>[^\n;=]*)=\s*\{")
            for match in initializer.finditer(src):
                prefix = match.group("prefix").decode("utf-8", "replace")
                tail = match.group("tail").decode("utf-8", "replace")
                words = prefix.split()
                if not prefix or prefix[0].isspace() or "extern" in words \
                        or _RECOVERED_DECL_PREFIX.fullmatch(prefix) is None:
                    continue
                if re.fullmatch(
                        r"(?:\s*\[[^]]*\])*"
                        r"(?:\s+_+[A-Za-z_]\w*(?:\s*\([^;]*\))?)*\s*",
                        tail) is None:
                    continue
                opening = match.end() - 1
                closing = _matching_delimiter(
                    src, opening, ord("{"), ord("}"))
                if closing is None:
                    close_match = re.search(rb"(?m)^}", src[opening + 1:])
                    if close_match is None:
                        continue
                    closing = opening + 1 + close_match.start()
                semicolon = closing + 1
                while semicolon < len(src) \
                        and src[semicolon:semicolon + 1].isspace():
                    semicolon += 1
                if semicolon >= len(src) or src[semicolon] != ord(";") \
                        or not _canonical_export_follows(
                            src, semicolon + 1, name):
                    continue
                start = src.count(b"\n", 0, match.start()) + 1
                end = src.count(b"\n", 0, closing) + 1
                out.append(Symbol(
                    name=name, kind=VARIABLE, start_line=start, end_line=end,
                    signature=_squash(
                        src[match.start():opening].decode("utf-8", "replace")),
                    is_static="static" in words, is_exported=True,
                ))
                break

        if want_var and not any(symbol.name == name for symbol in out):
            pattern = re.compile(
                rb"(?m)^(?P<prefix>[^\n#;{}(),=]*)\b" + encoded
                + rb"\b(?P<tail>[^;{}]*);")
            for match in pattern.finditer(src):
                prefix = match.group("prefix").decode("utf-8", "replace")
                tail = match.group("tail").decode("utf-8", "replace")
                words = _declaration_prefix_words(prefix)
                if not prefix or prefix[0].isspace() or words is None \
                        or "extern" in words:
                    continue
                if re.fullmatch(
                        r"(?:\s*\[[^]]*\])*"
                        r"(?:\s+(?:_+[A-Za-z_]\w*|[A-Z][A-Z0-9_]*)"
                        r"(?:\s*\([^;{}]*\))?)*"
                        r"(?:\s*=\s*[^;]*)?\s*", tail) is None:
                    continue
                if not _canonical_export_follows(src, match.end(), name):
                    continue
                start = src.count(b"\n", 0, match.start()) + 1
                end = src.count(b"\n", 0, match.end() - 1) + 1
                out.append(Symbol(
                    name=name, kind=VARIABLE, start_line=start, end_line=end,
                    signature=_squash(
                        src[match.start():match.end()].decode("utf-8", "replace")),
                    is_static="static" in words, is_exported=True,
                ))
                break
    return out


def _recovered_function_ends(src: bytes, functions: list) -> dict[tuple[int, int], int]:
    """Effective closing braces for overextended recovered function bodies.

    Kernel style keeps the function's closing brace in column zero while inner
    block braces are indented.  If tree-sitter continues a body past that brace
    (sometimes swallowing exports, declarations, and later functions), clamp
    its symbol/call extent to the source-level boundary.
    """
    ends: dict[tuple[int, int], int] = {}
    for function in functions:
        if not function.has_error:
            continue
        body = function.child_by_field_name("body")
        if body is None:
            continue
        region = src[body.start_byte + 1:body.end_byte]
        close = re.search(rb"(?m)^}", region)
        if close is None:
            continue
        end_byte = body.start_byte + 1 + close.start() + 1
        if end_byte >= body.end_byte:
            continue
        # A parse error alone is insufficient: valid conditional branches can
        # put a column-zero brace inside an otherwise correctly bounded body.
        # Require proof that file-scope syntax was swallowed after the brace.
        swallowed_function = any(
            other is not function and end_byte <= other.start_byte < body.end_byte
            and _starts_recovered_toplevel(src, other)
            for other in functions)
        swallowed_export = _SOURCE_EXPORT_RE.search(
            src[end_byte:body.end_byte]) is not None
        if swallowed_function or swallowed_export:
            ends[(function.start_byte, function.end_byte)] = end_byte
    return ends


def _recovered_declarations(functions: list,
                            ends: dict[tuple[int, int], int]) -> list:
    """Column-zero declarations hidden after a recovered function boundary."""
    out = []
    seen: set[tuple[int, int]] = set()

    def visit(node, boundary: int) -> None:
        for child in node.named_children:
            if child.end_byte <= boundary:
                continue
            if child.type == "function_definition":
                # A later recovered top-level function is handled by the
                # function capture; none of its locals belongs here.
                continue
            if child.type == "declaration" and child.start_byte >= boundary \
                    and child.start_point[1] == 0:
                key = (child.start_byte, child.end_byte)
                if key not in seen:
                    seen.add(key)
                    out.append(child)
                continue
            visit(child, boundary)

    for function in functions:
        boundary = ends.get((function.start_byte, function.end_byte))
        body = function.child_by_field_name("body")
        if boundary is not None and body is not None:
            visit(body, boundary)
    return out


def _recovery_gaps(src: bytes, functions: list,
                   ends: dict[tuple[int, int], int]) -> list[tuple[int, int]]:
    """Source ranges hidden inside an overextended function recovery node."""
    candidates = sorted(
        function.start_byte for function in functions
        if _starts_recovered_toplevel(src, function))
    gaps: list[tuple[int, int]] = []
    for function in functions:
        start = ends.get((function.start_byte, function.end_byte))
        if start is None or not function.has_error:
            continue
        end = next((byte for byte in candidates if byte > start),
                   function.end_byte)
        if end > start and src[start:end].strip():
            gaps.append((start, end))

    # Nested recovery nodes can describe the same source range.  Parsing the
    # earliest enclosing gap once is enough and avoids quadratic rescans.
    unique: list[tuple[int, int]] = []
    for start, end in sorted(set(gaps)):
        if unique and start < unique[-1][1]:
            continue
        unique.append((start, end))
    return unique


def _parameter_names(src: bytes, function) -> set[str]:
    """Named parameters belonging to a function definition's outer list."""
    if function.type != "function_definition":
        return set()
    declarator = function.child_by_field_name("declarator")
    name = _declarator_name(declarator)
    cur = name
    function_declarator = None
    for _ in range(64):
        if cur is None:
            break
        if cur.type == "function_declarator":
            function_declarator = cur
            break
        if cur is declarator:
            break
        cur = cur.parent
    if function_declarator is None:
        return set()
    parameters = function_declarator.child_by_field_name("parameters")
    if parameters is None:
        return set()

    names: set[str] = set()
    for parameter in parameters.named_children:
        if parameter.type in _IDENTIFIERS:
            names.add(_text(src, parameter))
            continue
        if parameter.type != "parameter_declaration":
            continue
        declarators = parameter.children_by_field_name("declarator")
        if not declarators:
            declarator = parameter.child_by_field_name("declarator")
            declarators = [declarator] if declarator is not None else []
        for declarator in declarators:
            name_node = _declarator_name(declarator)
            if name_node is not None:
                names.add(_text(src, name_node))
    return names


def _local_object_bindings(src: bytes, function, body,
                           end_byte: int | None) -> dict[str, list[tuple[int, int]]]:
    """Byte ranges where parameters or block-scope objects shadow functions.

    A block-scope function prototype is deliberately excluded: it still names
    a function.  Ordinary objects and function-pointer objects are blockers.
    The range model also respects nested compounds and ``for`` initializer
    scope, avoiding the common mistake of treating a later/sibling declaration
    as if it shadowed the whole function.
    """
    limit = min(body.end_byte, end_byte) if end_byte is not None else body.end_byte
    bindings: dict[str, list[tuple[int, int]]] = {}
    for name in _parameter_names(src, function):
        bindings.setdefault(name, []).append((body.start_byte, limit))

    stack = list(reversed(body.named_children))
    while stack:
        current = stack.pop()
        if current.start_byte >= limit:
            continue
        # Malformed input can place a recovered top-level function inside the
        # preceding body.  Its declarations are not locals of this function.
        if current.type == "function_definition":
            continue
        if current.type == "declaration":
            for declarator in _safe_declarators(src, current):
                if _is_function_prototype(declarator):
                    continue
                name_node = _declarator_name(declarator)
                if name_node is None:
                    continue
                scope = current.parent
                while scope is not None and scope is not body:
                    if scope.type in ("for_statement", "compound_statement"):
                        break
                    scope = scope.parent
                if scope is None:
                    scope = body
                scope_end = min(scope.end_byte, limit)
                if name_node.end_byte < scope_end:
                    bindings.setdefault(_text(src, name_node), []).append(
                        (name_node.end_byte, scope_end))
        stack.extend(reversed(current.named_children))
    return bindings


def _collect_call_details(src: bytes, node,
                          end_byte: int | None = None
                          ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Callee names and the subset bound to local objects in a function body.

    Accepts either a function_definition or a bare compound_statement — the
    latter is what SYSCALL_DEFINEn leaves us with, where the body is a sibling
    of the macro call rather than a child of anything function-shaped.
    """
    body = node if node.type == "compound_statement" else \
        node.child_by_field_name("body")
    if body is None:
        return (), ()
    cursor = QueryCursor(_CALL_QUERY)
    caps = cursor.captures(body)
    seen: dict[str, None] = {}
    indirect: dict[str, None] = {}
    direct: dict[str, None] = {}
    bindings = _local_object_bindings(src, node, body, end_byte)
    for n in caps.get("callee", []):
        if end_byte is not None and n.start_byte >= end_byte:
            continue
        name = _text(src, n)
        seen.setdefault(name, None)
        if any(start <= n.start_byte < end
               for start, end in bindings.get(name, ())):
            indirect.setdefault(name, None)
        else:
            direct.setdefault(name, None)
    # The calls table has one edge per caller/name.  If at least one spelling
    # is a direct function call, preserve that useful edge; mark a name
    # indirect only when every occurrence is bound to a local object.
    return tuple(seen), tuple(name for name in indirect if name not in direct)


def _collect_calls(src: bytes, node, end_byte: int | None = None) -> tuple[str, ...]:
    """Compatibility wrapper returning every syntactically visible callee."""
    return _collect_call_details(src, node, end_byte)[0]


def parse_source(src: bytes, kinds: frozenset[str], want_calls: bool = False) -> list[Symbol]:
    """Return the symbols defined in one C translation unit."""
    _ensure_parser()
    if len(src) > MAX_FILE_BYTES:
        return []

    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    function_nodes = caps.get("function", [])
    recovered_ends = _recovered_function_ends(src, function_nodes)
    declaration_nodes = list(caps.get("decl", []))
    known_declarations = {(n.start_byte, n.end_byte) for n in declaration_nodes}
    for node in _recovered_declarations(function_nodes, recovered_ends):
        if (node.start_byte, node.end_byte) not in known_declarations:
            declaration_nodes.append(node)

    symbols: list[Symbol] = []
    exported = _source_exports(src, tree.root_node)

    want_fn = FUNCTION in kinds
    want_sys = SYSCALL in kinds
    want_var = VARIABLE in kinds
    want_proto = PROTOTYPE in kinds
    range_call_nodes = None

    for node in function_nodes:
        if not (want_fn or want_sys):
            break
        # Loop macros followed by a block (for_each_possible_cpu, etc.) are
        # commonly recovered as nested function definitions.  Kernel C does
        # not use GCC nested functions, so only file-scope definitions belong
        # in the symbol index.
        if not _is_file_scope(src, node):
            continue
        if _in_preprocessor_continuation(src, node):
            continue
        name_node = _declarator_name(node.child_by_field_name("declarator"))
        if name_node is None:
            continue
        body = node.child_by_field_name("body")
        head_start = node.start_byte
        if node.start_point[1] and _starts_recovered_toplevel(src, node):
            head_start = src.rfind(b"\n", 0, node.start_byte) + 1
        head_bytes = src[head_start:node.end_byte] if body is None else \
            src[head_start:body.start_byte]
        head = head_bytes.decode("utf-8", "replace")
        if _LOOP_MACRO_HEAD.match(head.lstrip()):
            continue
        start, end = _lines(node)
        effective_end = recovered_ends.get((node.start_byte, node.end_byte))
        if effective_end is not None:
            end = src.count(b"\n", 0, effective_end) + 1
        name = _text(src, name_node)

        # SYSCALL_DEFINE0(fork) has a single argument, so unlike its siblings it
        # parses as a real function whose "return type" is the macro itself.
        type_node = node.child_by_field_name("type")
        m = _SYSCALL_MACRO.match(_text(src, type_node)) if type_node is not None else None
        if m:
            if want_sys and name.isidentifier():
                calls, indirect_calls = _collect_call_details(
                    src, node, effective_end) if want_calls else ((), ())
                symbols.append(Symbol(
                    name=_syscall_name(m, name), kind=SYSCALL,
                    start_line=start, end_line=end, signature=_squash(head),
                    calls=calls, indirect_calls=indirect_calls,
                ))
            continue

        name_offset = name_node.start_byte - head_start
        signature_start = 0
        signature_end = len(head)
        source_body_start = None
        source_body_end = None
        head_calls = _head_call_candidates(head_bytes) if node.has_error \
            or re.fullmatch(r"[A-Z][A-Z0-9_]+", name) else []
        single_current = len(head_calls) == 1 and head_calls[0][0] == name \
            and re.fullmatch(r"[A-Z][A-Z0-9_]+", name) is None
        single_named_arg = len(head_calls) == 1 \
            and re.match(rb"\s*" + re.escape(name.encode("ascii"))
                         + rb"\s*(?:,|$)", head_calls[0][2]) is not None \
            and re.fullmatch(r"[A-Z][A-Z0-9_]+", name) is None
        if (node.has_error and not (single_current or single_named_arg)) \
                or (not node.has_error and head_calls):
            recovered_name = _recovered_function_name(head_bytes, name)
            if recovered_name is None:
                continue
            name, name_offset = recovered_name
            start = src.count(b"\n", 0, head_start + name_offset) + 1
            signature_start = head.rfind("\n", 0, name_offset) + 1
            line_prefix = head[signature_start:name_offset]
            if not line_prefix.strip() and signature_start:
                previous_start = head.rfind("\n", 0, signature_start - 1) + 1
                previous = head[previous_start:signature_start - 1].strip()
                if previous and (_RECOVERED_DECL_PREFIX.fullmatch(previous)
                                 or re.search(
                                     r"\b(?:void|char|short|int|long|bool|"
                                     r"struct|union|enum|[us](?:8|16|32|64))\b",
                                     previous)):
                    signature_start = previous_start
            opening_brace = head.find("{", name_offset)
            if opening_brace >= 0:
                signature_end = opening_brace
                source_body_start = head_start + opening_brace
                closing_brace = _matching_delimiter(
                    src, source_body_start, ord("{"), ord("}"))
                if closing_brace is not None \
                        and closing_brace + 1 > node.end_byte:
                    source_body_end = closing_brace + 1
                    end = src.count(b"\n", 0, closing_brace) + 1
        if not name.isidentifier() or name in _C_TYPE_KEYWORDS \
                or name in _ATTRIBUTE_MACROS:
            continue
        prefix = re.findall(r"[A-Za-z_]\w*", head[:name_offset])

        if not want_fn:
            continue
        # In a real definition the extracted name introduces the parameter
        # list.  Recovery nodes for scoped_guard(x), EXPECT_FALSE(x), and other
        # block macros instead name an argument, which is followed by ``)``.
        if not single_named_arg \
                and re.search(rf"\b{re.escape(name)}\s*\(", head) is None:
            continue
        calls: tuple[str, ...] = ()
        indirect_calls: tuple[str, ...] = ()
        if want_calls:
            if source_body_end is not None:
                if range_call_nodes is None:
                    range_call_nodes = QueryCursor(_CALL_QUERY).captures(
                        tree.root_node).get("callee", [])
                calls = tuple(dict.fromkeys(
                    _text(src, call) for call in range_call_nodes
                    if source_body_start < call.start_byte < source_body_end))
                # The recovered extension may not belong to the function's
                # AST body, but bindings in the reliable prefix still block
                # false direct-function promotion.
                _, indirect_calls = _collect_call_details(
                    src, node, effective_end)
            else:
                calls, indirect_calls = _collect_call_details(
                    src, node, effective_end)
        symbols.append(Symbol(
            name=name,
            kind=FUNCTION,
            start_line=start,
            end_line=end,
            signature=_function_signature(
                head[signature_start:signature_end]),
            is_static="static" in prefix,
            is_inline=any(w in _INLINE_SPECIFIERS for w in prefix),
            calls=calls, indirect_calls=indirect_calls,
        ))

    for node in caps.get("macrocall", []):
        if not _is_file_scope(src, node):
            continue
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            continue
        macro = _text(src, callee)
        text = _text(src, node)

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
            body = _following_compound(node)
            start = node.start_point[0] + 1
            end = body.end_point[0] + 1 if body is not None and \
                body.type == "compound_statement" else node.end_point[0] + 1
            calls, indirect_calls = _collect_call_details(
                src, body) if want_calls and body is not None else ((), ())
            symbols.append(Symbol(
                name=_syscall_name(m, arg),
                kind=SYSCALL,
                start_line=start,
                end_line=end,
                signature=_squash(_text(src, node)),
                calls=calls,
                indirect_calls=indirect_calls,
            ))
            continue

        generated_attribute = _generated_attribute_decl(text)
        if generated_attribute is not None and want_var:
            start, end = _lines(node)
            line_start = src.rfind(b"\n", 0, node.start_byte) + 1
            line_prefix = src[line_start:node.start_byte].decode(
                "utf-8", "replace").split()
            symbols.append(Symbol(
                name=generated_attribute, kind=VARIABLE,
                start_line=start, end_line=end, signature=_squash(text),
                is_static="static" in line_prefix,
            ))
            continue

        macro_decl = _macro_decl(src, node)
        if macro_decl is not None and want_var:
            _, name = macro_decl
            start, end = _lines(node)
            symbols.append(Symbol(
                name=name, kind=VARIABLE, start_line=start, end_line=end,
                signature=_squash(_text(src, node)),
            ))

    if want_sys:
        for node in caps.get("syscall_type", []):
            if not _is_file_scope(src, node):
                continue
            name_node = node.child_by_field_name("name")
            macro = _text(src, name_node) if name_node is not None else ""
            m = _SYSCALL_MACRO.fullmatch(macro)
            text = _text(src, node)
            open_paren = text.find("(")
            args = _split_macro_args(text, open_paren) if open_paren >= 0 else None
            if m is None or not args or not args[0].isidentifier():
                continue
            body = _following_compound(node)
            start = node.start_point[0] + 1
            end = body.end_point[0] + 1 if body is not None else node.end_point[0] + 1
            calls, indirect_calls = _collect_call_details(
                src, body) if want_calls and body is not None else ((), ())
            symbols.append(Symbol(
                name=_syscall_name(m, args[0]), kind=SYSCALL,
                start_line=start, end_line=end, signature=_squash(text),
                calls=calls, indirect_calls=indirect_calls,
            ))

    if want_var:
        for capture in ("decl_macro", "decl_call"):
            for node in caps.get(capture, []):
                if not _is_file_scope(src, node):
                    continue
                macro_decl = _macro_decl(src, node)
                if macro_decl is None:
                    continue
                _, name = macro_decl
                start, end = _lines(node)
                line_start = src.rfind(b"\n", 0, node.start_byte) + 1
                line_prefix = src[line_start:node.start_byte].decode(
                    "utf-8", "replace").split()
                symbols.append(Symbol(
                    name=name, kind=VARIABLE, start_line=start, end_line=end,
                    signature=_squash(_text(src, node)),
                    is_static="static" in line_prefix,
                ))

    for capture, kind in (("struct", STRUCT), ("union", UNION), ("enum", ENUM)):
        if kind not in kinds:
            continue
        for node in caps.get(capture, []):
            if not _is_file_scope(src, node):
                continue
            name_node = node.child_by_field_name("name")
            raw_name = (_text(src, name_node).strip()
                        if name_node is not None else "")
            aliases = _typedef_aliases(src, node)
            anonymous = not raw_name or raw_name in _ATTRIBUTE_MACROS
            name = aliases[0] if anonymous and aliases else raw_name
            if not name:
                continue
            location_node = _outer_declaration(node) if anonymous else node
            start, end = _lines(location_node)
            body = node.child_by_field_name("body")
            nfields = _member_count(body, kind)
            summary = description = None
            members: tuple[TypeMember, ...] = ()
            parse_complete = True
            parse_warnings: tuple[str, ...] = ()
            unmatched_docs: tuple[tuple[str, str], ...] = ()
            if kind in {STRUCT, UNION}:
                definition_conditions = _preprocessor_context(src, node)
                identities = {name, *aliases}
                if raw_name and raw_name not in _ATTRIBUTE_MACROS:
                    identities.add(raw_name)
                doc = _kernel_doc(src, node, identities)
                if not (doc.summary or doc.description or doc.members):
                    ordinary_raw = _adjacent_comment_raw(
                        src, _outer_declaration(node).start_byte)
                    if ordinary_raw is not None:
                        ordinary_doc = _parse_aggregate_doc(
                            ordinary_raw, identities, "source-comment")
                        if (ordinary_doc.summary or ordinary_doc.description
                                or ordinary_doc.members):
                            doc = ordinary_doc
                summary, description = doc.summary, doc.description
                if summary is None and description is None:
                    summary = _adjacent_ordinary_comment(src, node)
                members, parse_complete, parse_warnings, unmatched_docs = \
                    _aggregate_members(src, node, doc, definition_conditions)
                nfields = sum(member.parent_index is None for member in members)
            else:
                definition_conditions = ()
            symbols.append(Symbol(
                name=name,
                kind=kind,
                start_line=start,
                end_line=end,
                signature=_aggregate_signature(src, node, kind, name, nfields),
                summary=summary, description=description, members=members,
                aliases=aliases, is_anonymous=anonymous,
                parse_complete=parse_complete,
                parse_warnings=parse_warnings,
                unmatched_member_docs=unmatched_docs,
                conditions=definition_conditions,
            ))
            if kind in {STRUCT, UNION} and STRUCT in kinds:
                symbols.extend(_generated_struct_group_symbols(src, node, doc))

    if STRUCT in kinds or UNION in kinds:
        known_aggregates = {
            (symbol.name, symbol.kind, symbol.start_line)
            for symbol in symbols if symbol.kind in {STRUCT, UNION}
        }
        for recovered in _recover_bpmp_empty_aggregates(src, tree.root_node):
            identity = (recovered.name, recovered.kind, recovered.start_line)
            if recovered.kind in kinds and identity not in known_aggregates:
                symbols.append(recovered)
                known_aggregates.add(identity)

    if TYPEDEF in kinds:
        for node in caps.get("typedef", []):
            if not _is_file_scope(src, node):
                continue
            start, end = _lines(node)
            for decl in _safe_declarators(src, node):
                name_node = _declarator_name(decl)
                if name_node is None:
                    continue
                name = _text(src, name_node).strip()
                if not name or name in _C_TYPE_KEYWORDS:
                    continue
                symbols.append(Symbol(
                    name=name, kind=TYPEDEF,
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

    if want_var or want_proto or want_fn:
        for node in declaration_nodes:
            if not _is_file_scope(src, node):
                continue
            head = _text(src, node)
            prefix = head.split("(", 1)[0].split()
            if "typedef" in prefix:
                continue
            type_node = node.child_by_field_name("type")
            if type_node is not None and _EXPORT_MACRO.fullmatch(
                    _text(src, type_node).strip()):
                # In ERROR recovery EXPORT_SYMBOL(foo); can look like a C
                # declaration of a variable named foo.  Its dedicated capture
                # above records the export; it is not itself a declaration.
                continue
            is_static = "static" in prefix
            start, end = _lines(node)

            # Tree-sitter can glue an export statement onto the preceding
            # macro declaration.  Inspect all declarator fields for this one
            # exact shape even though ordinary symbols after a recovery node
            # are deliberately ignored by _safe_declarators.
            for maybe_export in node.children_by_field_name("declarator"):
                export_name_node = _declarator_name(maybe_export)
                export_macro = _text(src, export_name_node).strip() \
                    if export_name_node is not None else ""
                if not _EXPORT_MACRO.match(export_macro):
                    continue
                plist = maybe_export.child_by_field_name("parameters")
                if plist is not None:
                    for p in plist.named_children:
                        arg = _text(src, p).strip()
                        if arg.isidentifier():
                            exported.add(arg)

            declarators = _safe_declarators(src, node)
            macro_decl = _macro_decl(src, node)
            if macro_decl and VARIABLE in kinds:
                _, macro_name = macro_decl
                symbols.append(Symbol(
                    name=macro_name, kind=VARIABLE, start_line=start,
                    end_line=end, signature=_squash(head),
                    is_static=is_static))

            generated_attribute = _generated_attribute_decl(head)
            if generated_attribute and VARIABLE in kinds:
                symbols.append(Symbol(
                    name=generated_attribute, kind=VARIABLE,
                    start_line=start, end_line=end,
                    signature=_squash(head), is_static=is_static))

            # An unexpanded file-scope macro invocation is not a C variable
            # declaration.  Tree-sitter recovery often presents its first
            # argument as a declarator (DEVICE_ATTR_WO(undock) used to create
            # a fictitious variable named ``undock``).  Standard attribute
            # macros above retain the object name that the macro really
            # generates; unknown shapes stay out of the index conservatively.
            if macro_decl is None and _macro_shaped_declaration(head) is not None:
                continue

            attribute_name = _attribute_declaration_name(head)
            if attribute_name and VARIABLE in kinds:
                symbols.append(Symbol(
                    name=attribute_name, kind=VARIABLE, start_line=start,
                    end_line=end, signature=_squash(head),
                    is_static=is_static))

            initializer_name = _initializer_declaration_name(head)
            if initializer_name and VARIABLE in kinds:
                symbols.append(Symbol(
                    name=initializer_name, kind=VARIABLE, start_line=start,
                    end_line=end, signature=_squash(head),
                    is_static=is_static))

            # A real C declaration ends in a semicolon.  Recovery frequently
            # turns the annotation lines immediately before a function (for
            # example __flag(...) __naked) into a declaration fragment; do not
            # expose its annotation token as a variable.
            if macro_decl is None and not head.rstrip().endswith(";") \
                    and not _has_trailing_attribute_terminator(src, node):
                continue

            for decl in declarators:
                name_node = _declarator_name(decl)
                name = _text(src, name_node).strip() if name_node is not None else ""
                if macro_decl or name == attribute_name:
                    continue
                if not name or name in _ATTRIBUTE_MACROS \
                        or name in _C_TYPE_KEYWORDS:
                    continue
                if _is_function_prototype(decl):
                    # GCC's __alias target emits a definition even though its
                    # surface syntax has no body.  Index it as a function so it
                    # remains visible with DEFAULT_KINDS.
                    kind = FUNCTION if re.search(r"\b__alias\s*\(", head) \
                        else PROTOTYPE
                else:
                    kind = VARIABLE
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

    # If recovery swallowed syntax so thoroughly that it produced no nodes at
    # all (ks0108's exports and following definitions are a real example),
    # reparse just the short gap before the next known top-level function.
    for gap_start, gap_end in _recovery_gaps(src, function_nodes, recovered_ends):
        line_offset = src.count(b"\n", 0, gap_start)
        for sym in parse_source(src[gap_start:gap_end], kinds, want_calls):
            sym.start_line += line_offset
            sym.end_line += line_offset
            for member in sym.members:
                member.start_line += line_offset
                member.end_line += line_offset
            symbols.append(sym)

    if exported and (want_fn or want_var):
        existing = {
            sym.name for sym in symbols
            if sym.kind in (FUNCTION, SYSCALL, VARIABLE, PROTOTYPE)
        }
        call_nodes = None
        if want_calls and exported - existing:
            call_nodes = QueryCursor(_CALL_QUERY).captures(
                tree.root_node).get("callee", [])
        symbols.extend(_source_exported_symbols(
            src, exported, existing, want_fn, want_var, call_nodes))

    if exported:
        for sym in symbols:
            if sym.kind in (FUNCTION, SYSCALL, VARIABLE, PROTOTYPE) \
                    and sym.name in exported:
                sym.is_exported = True

    # A simple ``static DEFINE_*`` is visible both as a declaration and as its
    # macro type specifier.  Merge those two views while retaining the richer
    # declaration signature and flags.  Source line is part of the key so
    # legitimate conditional redefinitions remain distinct.
    unique: dict[tuple[str, str, int], Symbol] = {}
    ordered: list[Symbol] = []
    for sym in symbols:
        key = (sym.name, sym.kind, sym.start_line)
        prior = unique.get(key)
        if prior is None:
            unique[key] = sym
            ordered.append(sym)
            continue
        prior.is_static = prior.is_static or sym.is_static
        prior.is_inline = prior.is_inline or sym.is_inline
        prior.is_exported = prior.is_exported or sym.is_exported
        if len(sym.signature) > len(prior.signature):
            prior.signature = sym.signature
            prior.end_line = max(prior.end_line, sym.end_line)
        if sym.calls:
            prior.calls = tuple(dict.fromkeys((*prior.calls, *sym.calls)))
        if sym.indirect_calls:
            prior.indirect_calls = tuple(dict.fromkeys(
                (*prior.indirect_calls, *sym.indirect_calls)))
        if not prior.summary and sym.summary:
            prior.summary = sym.summary
        if not prior.description and sym.description:
            prior.description = sym.description
        if not prior.members and sym.members:
            prior.members = sym.members
        if sym.aliases:
            prior.aliases = tuple(dict.fromkeys((*prior.aliases, *sym.aliases)))
        prior.is_anonymous = prior.is_anonymous or sym.is_anonymous
        prior.parse_complete = prior.parse_complete and sym.parse_complete
        if sym.parse_warnings:
            prior.parse_warnings = tuple(dict.fromkeys(
                (*prior.parse_warnings, *sym.parse_warnings)))
        if sym.unmatched_member_docs:
            prior.unmatched_member_docs = tuple(dict.fromkeys(
                (*prior.unmatched_member_docs, *sym.unmatched_member_docs)))
        if sym.conditions:
            prior.conditions = tuple(dict.fromkeys(
                (*prior.conditions, *sym.conditions)))

    return ordered


def parse_file(path, kinds: frozenset[str], want_calls: bool = False) -> list[Symbol]:
    try:
        with open(path, "rb") as fh:
            src = fh.read()
    except OSError:
        return []
    return parse_source(src, kinds, want_calls)
