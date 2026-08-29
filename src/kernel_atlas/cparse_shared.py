"""Shared kernel-C syntax helpers used by the parser feature modules."""

from __future__ import annotations


MAX_SIGNATURE = 400

DECLARATION_SPECIFIERS = frozenset({
    "void", "char", "short", "int", "long", "float", "double", "signed",
    "unsigned", "const", "volatile", "restrict", "static", "extern",
    "register", "inline", "_Bool", "_Atomic",
})
NAME_WRAPPING_DECL_MACROS = frozenset({"__bootdata_preserved"})

# Alignment/section attributes written after a declarator. Without the
# preprocessor these look exactly like the variable's name, e.g.
#   struct sem { ... } ____cacheline_aligned_in_smp;
ATTRIBUTE_MACROS = frozenset({
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
    "__section", "__nonstring", "__counted_by", "__counted_by_le",
    "__guarded_by", "__aligned_largest", "__printf", "__scanf",
    "__counted_by_ptr", "__counted_by_be", "__module_memory_align",
    "__kernel_nonstring", "CRYPTO_MINALIGN_ATTR", "ACPI_NONSTRING",
    "BPMP_UNION_ANON", "BPMP_ABI_PACKED", "EPOLL_PACKED",
    "__ATM_API_ALIGN", "__ARCH_COMPAT_FLOCK64_PACK", "PACKED",
    "ARCH_PACK_STATFS64", "ARCH_PACK_COMPAT_STATFS64",
}) | NAME_WRAPPING_DECL_MACROS

CALL_ATTRIBUTE_MACROS = frozenset({
    "__aligned", "__section", "__alias", "__acquires", "__releases",
    "__counted_by", "__counted_by_le", "__counted_by_ptr",
    "__counted_by_be", "__guarded_by", "__printf", "__scanf",
})

# Names that can only come out of a misparse of unexpanded macros, never from
# a real declaration.
C_TYPE_KEYWORDS = frozenset({
    "int", "long", "short", "char", "unsigned", "signed", "void", "float",
    "double", "bool", "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "size_t", "ssize_t", "struct", "union", "enum", "const", "volatile",
    "static", "extern", "register", "inline", "typedef", "if", "else", "for",
    "while", "do", "return", "goto", "switch", "case", "default", "sizeof",
    "NULL",
})

DECLARATOR_FIELDS = {
    "pointer_declarator", "function_declarator", "array_declarator",
    "parenthesized_declarator", "init_declarator", "attributed_declarator",
}
IDENTIFIERS = {"identifier", "type_identifier", "field_identifier"}


def text(src: bytes, node) -> str:
    """Decode the exact source span occupied by a tree-sitter node."""
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def squash(value: str, limit: int = MAX_SIGNATURE) -> str:
    """Collapse source whitespace and cap the resulting display text."""
    value = " ".join(value.split())
    return value[:limit] + "…" if len(value) > limit else value


def declarator_name(node):
    """Walk declarator wrappers down to their identifier node."""
    current = node
    for _ in range(32):
        if current is None:
            return None
        if current.type in IDENTIFIERS:
            return current
        following = current.child_by_field_name("declarator")
        if following is None:
            following = next(
                (child for child in current.named_children
                 if child.type in DECLARATOR_FIELDS
                 or child.type in IDENTIFIERS),
                None,
            )
        current = following
    return None


def lines(node) -> tuple[int, int]:
    """Return inclusive one-based source lines for a tree-sitter node."""
    start = node.start_point[0] + 1
    # Tree-sitter ranges are half-open. A node ending immediately after a
    # newline points at column zero of the following line.
    end = node.end_point[0] + (0 if node.end_point[1] == 0 else 1)
    return start, max(start, end)


def split_macro_args(value: str, open_paren: int) -> list[str] | None:
    """Split a macro invocation while preserving nested delimiters/quotes."""
    args: list[str] = []
    start = open_paren + 1
    stack = [")"]
    quote = ""
    escaped = False
    index = start
    while index < len(value):
        character = value[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            index += 1
            continue
        if character in "\"'":
            quote = character
        elif character in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[character])
        elif character == stack[-1]:
            stack.pop()
            if not stack:
                args.append(value[start:index].strip())
                return args
        elif character == "," and len(stack) == 1:
            args.append(value[start:index].strip())
            start = index + 1
        index += 1
    return None


def source_code_leaf(root, offset: int):
    """Return the source AST leaf unless the offset is inside non-code."""
    node = root.descendant_for_byte_range(offset, offset + 1)
    current = node
    while current is not None and current.type != "translation_unit":
        if current.type in {
                "comment", "string_literal", "char_literal",
                "preproc_def", "preproc_function_def"}:
            return None
        current = current.parent
    return node


def safe_declarators(src: bytes, node) -> list:
    """Return direct declarators before tree-sitter's first recovery node."""
    result = []
    recovered = False
    for index, child in enumerate(node.children):
        if child.type == "ERROR":
            error_text = text(src, child).strip()
            words = error_text.split()
            if words and all(
                    word in ATTRIBUTE_MACROS
                    or word in DECLARATION_SPECIFIERS for word in words):
                continue
            recovered = True
            continue
        if node.field_name_for_child(index) == "declarator" and not recovered:
            result.append(child)
    return result


def matching_delimiter(src: bytes, opening: int,
                       opener: int, closer: int) -> int | None:
    """Find a balanced C delimiter's close, ignoring comments and strings."""
    depth = 1
    quote = 0
    index = opening + 1
    while index < len(src):
        if quote:
            if src[index] == ord("\\"):
                index += 2
                continue
            if src[index] == quote:
                quote = 0
        elif src.startswith(b"//", index):
            newline = src.find(b"\n", index + 2)
            index = len(src) if newline < 0 else newline
            continue
        elif src.startswith(b"/*", index):
            close = src.find(b"*/", index + 2)
            index = len(src) if close < 0 else close + 1
        elif src[index] in (ord('"'), ord("'")):
            quote = src[index]
        elif src[index] == opener:
            depth += 1
        elif src[index] == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None
