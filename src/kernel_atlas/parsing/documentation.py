"""Kernel-doc, ordinary comments, and member documentation matching helpers.

These helpers preserve source markup and distinguish explicit member docs from
fallback prose. They do not parse declarations or own a tree-sitter parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import TypeMember
from .syntax import outer_declaration as _outer_declaration

_BOILERPLATE_COMMENT_RE = re.compile(
    r"(?i)^(?:SPDX-License-Identifier\s*:|copyright\b|"
    r"licen[cs]e(?:d)?\s*(?:under|:)|all rights reserved\b|authors?\s*:)")


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



def _is_boilerplate_comment(text: str) -> bool:
    """Whether an ordinary comment starts a legal/attribution header line.

    Match line roles rather than isolated words. Aggregate fields such as
    ``@author`` and prose which discusses a license are useful documentation,
    not evidence that the whole block is a file header.
    """
    return any(_BOILERPLATE_COMMENT_RE.match(line.strip()) is not None
               for line in _comment_lines(text) if line.strip())



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
    gap = prefix[end + 2:] if end >= 0 else b""
    if end < 0 or gap.strip() or (not kernel_doc and gap.count(b"\n") > 1):
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
    gap = prefix[end + 2:]
    if gap.strip() or gap.count(b"\n") > 1:
        return None
    begin = prefix.rfind(b"/*", 0, end)
    if begin < 0 or prefix[begin:begin + 3] == b"/**":
        return None
    value = _paragraphs(_comment_lines(
        prefix[begin:end + 2].decode("utf-8", "replace")))
    if value and not _is_boilerplate_comment(
            prefix[begin:end + 2].decode("utf-8", "replace")):
        return value
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



def _comment_before_offset(
        src: bytes, offset: int) -> tuple[str | None, str | None, str | None]:
    raw = (_adjacent_comment_raw(src, offset, kernel_doc=True)
           or _adjacent_comment_raw(src, offset))
    if raw is None:
        return None, None, None
    if _is_visibility_marker(raw):
        return None, None, None
    return _comment_description(raw)
