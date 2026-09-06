"""Parse C aggregate declarations and retain member syntax and recovery evidence.

Comment interpretation and struct-group macro expansion live in their feature
modules. The caller supplies fragment parsing for recursive recovery.
"""

from __future__ import annotations

import re
from typing import Callable

from .documentation import (
    _KernelDoc,
    _adjacent_comment_raw,
    _adjacent_ordinary_comment,
    _comment_description,
    _comment_lines,
    _declarator_comment,
    _is_boilerplate_comment,
    _kernel_doc,
    _member_path,
    _normalize_doc_key,
    _parse_aggregate_doc,
    _trailing_member_comment,
)
from .groups import _apply_struct_groups, _generated_struct_group_symbols
from .models import ENUM, STRUCT, UNION, FragmentParser, Symbol, TypeMember
from .syntax import (
    ATTRIBUTE_MACROS as _ATTRIBUTE_MACROS,
    CALL_ATTRIBUTE_MACROS as _CALL_ATTRIBUTE_MACROS,
    C_TYPE_KEYWORDS as _C_TYPE_KEYWORDS,
    MAX_MEMBER_DECLARATION as MAX_MEMBER_DECLARATION,
    declarator_name as _declarator_name,
    lines as _lines,
    matching_delimiter as _matching_delimiter,
    outer_declaration as _outer_declaration,
    preprocessor_context as _preprocessor_context,
    safe_declarators as _safe_declarators,
    source_code_leaf as _source_code_leaf,
    split_macro_args as _split_macro_args,
    squash as _squash,
    strip_c_comments as _strip_c_comments,
    text as _text,
)


FileScopePredicate = Callable[[bytes, object], bool]


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
        conditions: tuple[str, ...], visibility: str,
        parse_fragment: FragmentParser) \
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
    recovered = parse_fragment(
        wrapper, STRUCT, "__kernel_atlas_annotated")
    if recovered is None or not recovered.members or not recovered.parse_complete:
        return ()
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


def _aggregate_members(src: bytes, node, doc: _KernelDoc,
                       parse_fragment: FragmentParser,
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
        recovered = parse_fragment(wrapper, UNION, "__kernel_atlas_macro")
        if recovered is None:
            return False
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
                parent_index, conditions, visibility, parse_fragment)
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
                            conditions, visibility, parse_fragment)
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
        src, node, members, member_ranges, parse_fragment)
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


def recover_bpmp_empty_aggregates(
        src: bytes, root, at_file_scope: FileScopePredicate) \
        -> tuple[Symbol, ...]:
    """Recover the exact BPMP empty-payload form lost inside a root ERROR."""
    recovered: list[Symbol] = []
    for match in _BPMP_EMPTY_AGGREGATE_RE.finditer(src):
        leaf = _source_code_leaf(root, match.start())
        if leaf is None or not at_file_scope(src, leaf):
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


def parse_definition(
        src: bytes, node, kind: str, parse_fragment: FragmentParser,
        *, include_generated_structs: bool = False) -> tuple[Symbol, ...]:
    """Parse one captured struct, union, or enum definition."""
    name_node = node.child_by_field_name("name")
    raw_name = (_text(src, name_node).strip()
                if name_node is not None else "")
    aliases = _typedef_aliases(src, node)
    anonymous = not raw_name or raw_name in _ATTRIBUTE_MACROS
    name = aliases[0] if anonymous and aliases else raw_name
    if not name:
        return ()

    location_node = _outer_declaration(node) if anonymous else node
    start, end = _lines(location_node)
    body = node.child_by_field_name("body")
    member_count = _member_count(body, kind)
    summary = description = None
    members: tuple[TypeMember, ...] = ()
    parse_complete = True
    parse_warnings: tuple[str, ...] = ()
    unmatched_docs: tuple[tuple[str, str], ...] = ()
    definition_conditions: tuple[str, ...] = ()
    doc = _KernelDoc(None, None, {})

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
                comment_lines = [
                    line.strip() for line in _comment_lines(ordinary_raw)
                    if line.strip()
                ]
                named = bool(comment_lines) and any(re.match(
                    rf"(?:(?:struct|union|typedef)\s+)?{re.escape(identity)}"
                    r"\s*[-:]",
                    comment_lines[0],
                ) for identity in identities)
                if ((ordinary_doc.members or named)
                        and (named or not _is_boilerplate_comment(ordinary_raw))):
                    doc = ordinary_doc
        summary, description = doc.summary, doc.description
        if summary is None and description is None:
            summary = _adjacent_ordinary_comment(src, node)
        members, parse_complete, parse_warnings, unmatched_docs = \
            _aggregate_members(
                src, node, doc, parse_fragment, definition_conditions)
        member_count = sum(member.parent_index is None for member in members)

    symbol = Symbol(
        name=name,
        kind=kind,
        start_line=start,
        end_line=end,
        signature=_aggregate_signature(
            src, node, kind, name, member_count),
        summary=summary,
        description=description,
        members=members,
        aliases=aliases,
        is_anonymous=anonymous,
        parse_complete=parse_complete,
        parse_warnings=parse_warnings,
        unmatched_member_docs=unmatched_docs,
        conditions=definition_conditions,
    )
    generated = ()
    if kind in {STRUCT, UNION} and include_generated_structs:
        generated = tuple(_generated_struct_group_symbols(
            src, node, doc, parse_fragment))
    return (symbol, *generated)
