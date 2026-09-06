"""Expand source-level struct_group macros into member trees and named tags.

Synthetic member fragments are parsed through an injected callback, keeping
macro expansion independent of aggregate parsing and parser initialization.
"""

from __future__ import annotations

import re

from .documentation import (
    _KernelDoc,
    _adjacent_comment_raw,
    _comment_before_offset,
    _member_path,
    _normalize_doc_key,
    _parse_aggregate_doc,
)
from .models import STRUCT, FragmentParser, Symbol, TypeMember
from .syntax import (
    MAX_MEMBER_DECLARATION,
    matching_delimiter as _matching_delimiter,
    preprocessor_context as _preprocessor_context,
    squash as _squash,
    strip_c_comments as _strip_c_comments,
)


def _macro_arg_byte_ranges(data: bytes, open_paren: int,
                           closing: int) -> list[tuple[int, int]]:
    """Return top-level argument byte ranges inside one macro invocation."""
    ranges: list[tuple[int, int]] = []
    start = open_paren + 1
    stack = [ord(")")]
    quote = 0
    index = start
    while index < closing:
        if quote:
            if data[index] == ord("\\"):
                index += 2
                continue
            if data[index] == quote:
                quote = 0
        elif data.startswith(b"//", index):
            newline = data.find(b"\n", index + 2, closing)
            index = closing if newline < 0 else newline
            continue
        elif data.startswith(b"/*", index):
            end = data.find(b"*/", index + 2, closing)
            index = closing if end < 0 else end + 1
        elif data[index] in (ord('"'), ord("'")):
            quote = data[index]
        elif data[index] in (ord("("), ord("["), ord("{")):
            stack.append({ord("("): ord(")"), ord("["): ord("]"),
                          ord("{"): ord("}")}[data[index]])
        elif data[index] == stack[-1]:
            stack.pop()
        elif data[index] == ord(",") and len(stack) == 1:
            ranges.append((start, index))
            start = index + 1
        index += 1
    ranges.append((start, closing))
    return ranges



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
                         member_ranges: list[tuple[int, int]],
                         parse_fragment: FragmentParser) \
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
        symbol = parse_fragment(wrapper, STRUCT, "__kernel_atlas_group")
        if symbol is None:
            return [], [
                f"could not parse members of {group['macro']} at line "
                f"{group['start_line']}"
            ]
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
        src: bytes, node, outer_doc: _KernelDoc,
        parse_fragment: FragmentParser) -> list[Symbol]:
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
        recovered = parse_fragment(wrapper, STRUCT, tag)
        if recovered is None:
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
