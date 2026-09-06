"""Extract symbols from kernel C sources with tree-sitter.

Kernel C is macro-heavy, so a few idioms need explicit handling on top of the
plain grammar:

  * ``SYSCALL_DEFINE3(open, ...) { ... }`` does not parse as a function.  The
    macro call becomes an ``expression_statement`` and the body a *sibling*
    ``compound_statement``.  We rebuild ``sys_open`` from that shape.
  * ``EXPORT_SYMBOL(foo)`` marks ``foo`` as available to modules, which is one
    of the more useful things to know about a kernel function.

This facade owns tree-sitter initialization, capture dispatch, and symbol
merging. Call evidence, source recovery, and aggregate parsing are implemented
in dedicated feature modules with shared syntax helpers and record types.
"""

from __future__ import annotations

import re

import tree_sitter_c
from tree_sitter import Language, Parser, Query, QueryCursor, QueryError

from . import aggregates as _aggregate_parse
from .calls import (
    _EMPTY_MACRO_TRANSITIONS,
    _call_site,
    _collect_call_details,
    _local_object_bindings,
    _macro_is_active,
    _macro_transitions,
    _summarize_call_sites,
)
from .models import (
    ALL_KINDS as ALL_KINDS,
    DEFAULT_KINDS as DEFAULT_KINDS,
    ENUM,
    FUNCTION,
    MACRO,
    PROTOTYPE,
    STRUCT,
    SYSCALL,
    TYPEDEF,
    UNION,
    VARIABLE,
    CallSite as CallSite,
    Symbol as Symbol,
    TypeMember as TypeMember,
)
from .recovery import (
    _RECOVERED_DECL_PREFIX,
    _head_call_candidates,
    _is_file_scope,
    _recovered_declarations,
    _recovered_function_ends,
    _recovered_function_name,
    _recovery_gaps,
    _source_exported_symbols,
    _source_exports,
    _starts_recovered_toplevel,
)
from .syntax import (
    ATTRIBUTE_MACROS as _ATTRIBUTE_MACROS,
    C_TYPE_KEYWORDS as _C_TYPE_KEYWORDS,
    INLINE_SPECIFIERS as _INLINE_SPECIFIERS,
    MAX_SIGNATURE as MAX_SIGNATURE,
    NAME_WRAPPING_DECL_MACROS as _NAME_WRAPPING_DECL_MACROS,
    declarator_name as _declarator_name,
    function_signature as _function_signature,
    is_function_prototype as _is_function_prototype,
    lines as _lines,
    matching_delimiter as _matching_delimiter,
    safe_declarators as _safe_declarators,
    source_sorted_nodes as _source_sorted_nodes,
    split_macro_args as _split_macro_args,
    squash as _squash,
    text as _text,
)

# Skip pathological/generated files; nothing human-readable is this big.
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_MEMBER_DECLARATION = _aggregate_parse.MAX_MEMBER_DECLARATION


def validate_max_file_bytes(value: int) -> int:
    """Return one supported parser read limit or raise a stable API error."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("max_file_bytes must be a positive integer")
    return value

_SYSCALL_MACRO = re.compile(r"^(COMPAT_)?SYSCALL_DEFINE(\d)$")
_EXPORT_MACRO = re.compile(
    r"^EXPORT(_PER_CPU)?_SYMBOL(_GPL|_NS|_NS_GPL|_FOR_MODULES)?$")
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
_CALL_PATTERN = "(call_expression) @call"
_SYMBOL_KIND_ORDER = {
    kind: position for position, kind in enumerate(ALL_KINDS)
}


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


def _in_preprocessor_continuation(src: bytes, node) -> bool:
    line_start = src.rfind(b"\n", 0, node.start_byte) + 1
    if line_start <= 0:
        return False
    previous_start = src.rfind(b"\n", 0, line_start - 1) + 1
    return src[previous_start:line_start - 1].rstrip().endswith(b"\\")


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


def source_include_directives(src: bytes) -> tuple[tuple[str, str, int], ...]:
    """Real quoted/angle ``#include`` directives whose operand ends in ``.c``.

    A syntax-tree pass is intentional here: a raw line regex also sees examples
    inside block comments and continued string literals, which would invent
    translation-unit membership and confidently misresolve call identities.
    """
    _ensure_parser()
    root = _PARSER.parse(src).root_node
    nodes = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == "preproc_include":
            nodes.append(current)
            continue
        stack.extend(reversed(current.named_children))

    directives: list[tuple[str, str, int]] = []
    for node in _source_sorted_nodes(nodes):
        path = node.child_by_field_name("path")
        if path is None:
            continue
        raw = _text(src, path).strip()
        if path.type == "string_literal" and len(raw) >= 2 \
                and raw.startswith('"') and raw.endswith('"'):
            delimiter, token = '"', raw[1:-1]
        elif path.type == "system_lib_string" and len(raw) >= 2 \
                and raw.startswith("<") and raw.endswith(">"):
            delimiter, token = "<", raw[1:-1]
        else:
            continue
        if token.endswith(".c"):
            directives.append((delimiter, token, node.start_point[0] + 1))
    return tuple(directives)


def _parse_aggregate_fragment(
        src: bytes, kind: str, expected_name: str) -> Symbol | None:
    """Re-enter the facade for one synthetic aggregate source fragment."""
    return next((
        symbol for symbol in parse_source(src, frozenset({kind}))
        if symbol.name == expected_name
    ), None)


def _recover_bpmp_empty_aggregates(
        src: bytes, root) -> tuple[Symbol, ...]:
    """Compatibility wrapper for the aggregate parser's BPMP fallback."""
    return _aggregate_parse.recover_bpmp_empty_aggregates(
        src, root, _is_file_scope)


def parse_source(
        src: bytes, kinds: frozenset[str],
        want_calls: bool = False, *,
    max_file_bytes: int = MAX_FILE_BYTES) -> list[Symbol]:
    """Return the symbols defined in one C translation unit."""
    max_file_bytes = validate_max_file_bytes(max_file_bytes)
    _ensure_parser()
    if len(src) > max_file_bytes:
        return []

    tree = _PARSER.parse(src)
    caps = QueryCursor(_QUERY).captures(tree.root_node)
    macro_states = (_macro_transitions(src, tree.root_node) if want_calls
                    else _EMPTY_MACRO_TRANSITIONS)
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
                calls, indirect_calls, call_sites = _collect_call_details(
                    src, node, _CALL_QUERY, effective_end, macro_states
                ) if want_calls else ((), (), ())
                symbols.append(Symbol(
                    name=_syscall_name(m, name), kind=SYSCALL,
                    start_line=start, end_line=end, signature=_squash(head),
                    calls=calls, indirect_calls=indirect_calls,
                    call_sites=call_sites,
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
        call_sites: tuple[CallSite, ...] = ()
        if want_calls:
            if source_body_end is not None:
                if range_call_nodes is None:
                    range_call_nodes = _source_sorted_nodes(
                        QueryCursor(_CALL_QUERY).captures(
                            tree.root_node).get("call", []))
                body = node.child_by_field_name("body")
                bindings = (_local_object_bindings(
                    src, node, body, effective_end) if body is not None else {})
                sites = tuple(
                    site
                    for call in range_call_nodes
                    if source_body_start < call.start_byte < source_body_end
                    if (site := _call_site(
                        src, call, bindings, macro_states)) is not None
                )
                calls, indirect_calls, call_sites = \
                    _summarize_call_sites(sites)
            else:
                calls, indirect_calls, call_sites = _collect_call_details(
                    src, node, _CALL_QUERY, effective_end, macro_states)
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
            call_sites=call_sites,
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
            calls, indirect_calls, call_sites = _collect_call_details(
                src, body, _CALL_QUERY, transitions=macro_states
            ) if want_calls and body is not None else ((), (), ())
            symbols.append(Symbol(
                name=_syscall_name(m, arg),
                kind=SYSCALL,
                start_line=start,
                end_line=end,
                signature=_squash(_text(src, node)),
                calls=calls,
                indirect_calls=indirect_calls,
                call_sites=call_sites,
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
            calls, indirect_calls, call_sites = _collect_call_details(
                src, body, _CALL_QUERY, transitions=macro_states
            ) if want_calls and body is not None else ((), (), ())
            symbols.append(Symbol(
                name=_syscall_name(m, args[0]), kind=SYSCALL,
                start_line=start, end_line=end, signature=_squash(text),
                calls=calls, indirect_calls=indirect_calls,
                call_sites=call_sites,
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
            symbols.extend(_aggregate_parse.parse_definition(
                src, node, kind, _parse_aggregate_fragment,
                include_generated_structs=STRUCT in kinds,
            ))

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
        for sym in parse_source(
                src[gap_start:gap_end], kinds, want_calls,
                max_file_bytes=max_file_bytes):
            sym.start_line += line_offset
            sym.end_line += line_offset
            for member in sym.members:
                member.start_line += line_offset
                member.end_line += line_offset
            for call_site in sym.call_sites:
                call_site.start_line += line_offset
                call_site.start_byte += gap_start
                if call_site.kind != "indirect":
                    call_site.kind = (
                        "macro" if _macro_is_active(
                            macro_states, call_site.name,
                            call_site.start_byte) else "direct")
            symbols.append(sym)

    if exported and (want_fn or want_var):
        existing = {
            sym.name for sym in symbols
            if sym.kind in (FUNCTION, SYSCALL, VARIABLE, PROTOTYPE)
        }
        call_nodes = None
        if want_calls and exported - existing:
            call_nodes = QueryCursor(_CALL_QUERY).captures(
                tree.root_node).get("call", [])
        symbols.extend(_source_exported_symbols(
            src, exported, existing, want_fn, want_var, call_nodes,
            macro_states))

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
        if sym.call_sites:
            prior.call_sites = tuple(sorted(
                dict.fromkeys(
                    (site.name, site.kind, site.start_line, site.start_byte)
                    for site in (*prior.call_sites, *sym.call_sites)
                ),
                key=lambda value: (value[3], value[0], value[1]),
            ))
            prior.call_sites = tuple(CallSite(*site) for site in prior.call_sites)
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

    # QueryCursor capture lists are not guaranteed to retain source order and
    # can vary between otherwise identical parses. Stable source-first output
    # keeps symbol ids repeatable across index builds. Symbols do not retain a
    # source column, so end line, semantic kind order, name, and signature are
    # explicit tie-breakers for definitions beginning on the same line.
    return sorted(ordered, key=lambda symbol: (
        symbol.start_line,
        symbol.end_line,
        _SYMBOL_KIND_ORDER.get(symbol.kind, len(_SYMBOL_KIND_ORDER)),
        symbol.name,
        symbol.signature,
    ))


def parse_file(
        path, kinds: frozenset[str], want_calls: bool = False, *,
        max_file_bytes: int = MAX_FILE_BYTES) -> list[Symbol]:
    max_file_bytes = validate_max_file_bytes(max_file_bytes)
    try:
        with open(path, "rb") as fh:
            src = fh.read(max_file_bytes + 1)
    except OSError:
        return []
    return parse_source(
        src, kinds, want_calls, max_file_bytes=max_file_bytes)
