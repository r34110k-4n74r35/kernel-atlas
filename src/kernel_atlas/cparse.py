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
_INTERESTING_MACRO_QUERY_RE = (
    rf"^(?:EXPORT(?:_PER_CPU)?_SYMBOL(?:_GPL|_NS|_NS_GPL|_FOR_MODULES)?|"
    rf"(?:COMPAT_)?SYSCALL_DEFINE[0-9]|{_DECL_MACRO_QUERY_RE})$")
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
}) | _NAME_WRAPPING_DECL_MACROS

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
    return node.start_point[0] + 1, node.end_point[0] + 1


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
    while cur is not None:
        if cur.type == "function_definition":
            inside_function = True
            recovered_function = recovered_function or cur.has_error
        elif cur.type in ("compound_statement", "ERROR"):
            recovery_container = True
        elif cur.type in ("preproc_def", "preproc_function_def"):
            return False
        if cur.type == "translation_unit":
            if inside_function:
                return _starts_recovered_toplevel(src, node) \
                    and recovered_function
            return not recovery_container or _starts_recovered_toplevel(src, node)
        cur = cur.parent
    # A severely malformed file can have ERROR as its root rather than a
    # translation_unit.  Do not let that exceptional root turn nested locals
    # back into file-scope symbols.
    if inside_function:
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


def _source_exports(src: bytes, root) -> set[str]:
    """Canonical source-level exports, independent of recovery node shape.

    Some ERROR trees bury a whole run of exports below type descriptors, where
    no useful query capture exists.  The line-anchored spelling is unambiguous;
    checking its smallest AST ancestor excludes comments, strings, and macro
    definitions/continuations.
    """
    exported: set[str] = set()
    ignored = {
        "comment", "string_literal", "char_literal",
        "preproc_def", "preproc_function_def",
    }
    for match in _SOURCE_EXPORT_RE.finditer(src):
        node = root.descendant_for_byte_range(
            match.start(1), match.start(1) + 1)
        cur = node
        while cur is not None and cur.type != "translation_unit":
            if cur.type in ignored:
                break
            cur = cur.parent
        else:
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


def _collect_calls(src: bytes, node, end_byte: int | None = None) -> tuple[str, ...]:
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
        if end_byte is not None and n.start_byte >= end_byte:
            continue
        seen.setdefault(_text(src, n), None)
    return tuple(seen)


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
                symbols.append(Symbol(
                    name=_syscall_name(m, name), kind=SYSCALL,
                    start_line=start, end_line=end, signature=_squash(head),
                    calls=_collect_calls(src, node, effective_end)
                    if want_calls else (),
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
        calls = ()
        if want_calls:
            if source_body_end is not None:
                if range_call_nodes is None:
                    range_call_nodes = QueryCursor(_CALL_QUERY).captures(
                        tree.root_node).get("callee", [])
                calls = tuple(dict.fromkeys(
                    _text(src, call) for call in range_call_nodes
                    if source_body_start < call.start_byte < source_body_end))
            else:
                calls = _collect_calls(src, node, effective_end)
        symbols.append(Symbol(
            name=name,
            kind=FUNCTION,
            start_line=start,
            end_line=end,
            signature=_function_signature(
                head[signature_start:signature_end]),
            is_static="static" in prefix,
            is_inline=any(w in _INLINE_SPECIFIERS for w in prefix),
            calls=calls,
        ))

    for node in caps.get("macrocall", []):
        if not _is_file_scope(src, node):
            continue
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
            body = _following_compound(node)
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
            symbols.append(Symbol(
                name=_syscall_name(m, args[0]), kind=SYSCALL,
                start_line=start, end_line=end, signature=_squash(text),
                calls=_collect_calls(src, body) if want_calls and body is not None else (),
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
            if name_node is None:
                continue
            start, end = _lines(node)
            body = node.child_by_field_name("body")
            nfields = _member_count(body, kind)
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

    return ordered


def parse_file(path, kinds: frozenset[str], want_calls: bool = False) -> list[Symbol]:
    try:
        with open(path, "rb") as fh:
            src = fh.read()
    except OSError:
        return []
    return parse_source(src, kinds, want_calls)
