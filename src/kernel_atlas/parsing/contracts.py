"""Function contracts quoted from adjacent kernel-doc comments."""

from __future__ import annotations

from bisect import bisect_right
import re

from .syntax import ATTRIBUTE_MACROS, CALL_ATTRIBUTE_MACROS, matching_delimiter


_BUILTIN_ATTRIBUTES = {"__attribute__", "__attribute"}
_DECORATION_NAME = re.compile(rb"[A-Za-z_][A-Za-z_0-9]*")
_GENERIC_SECTION = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):(?:[ \t]+(.*)|$)")


def _decorations_only(gap: bytes) -> bool:
    """Admit known declaration decorations, never arbitrary intervening code.

    Tree-sitter sometimes starts an annotated prototype after ``__printf``.
    Shared attribute names and balanced argument parsing keep that declaration
    adjacent to its comment without skipping unrelated statements or macros.
    """
    gap = gap.strip()
    position = 0
    while position < len(gap):
        while position < len(gap) and gap[position:position + 1].isspace():
            position += 1
        if position == len(gap):
            return True
        match = _DECORATION_NAME.match(gap, position)
        if match is None:
            return False
        name = match[0].decode("ascii")
        if name not in ATTRIBUTE_MACROS and name not in _BUILTIN_ATTRIBUTES:
            return False
        position = match.end()
        while position < len(gap) and gap[position:position + 1].isspace():
            position += 1
        if name in CALL_ATTRIBUTE_MACROS or name in _BUILTIN_ATTRIBUTES:
            if gap[position:position + 1] != b"(":
                return False
            closing = matching_delimiter(gap, position, ord("("), ord(")"))
            if closing is None or any(token in gap[position:closing] for token in (b";", b"{", b"}", b"#")):
                return False
            position = closing + 1
        elif gap[position:position + 1] == b"(":
            return False
    return True


def function_contracts(source: bytes, symbols) -> dict[int, dict]:
    """Return source documentation by symbol ordinal, without inferred rules."""
    comments = list(re.finditer(rb'/\*\*(.*?)\*/', source, re.S))
    ends = [comment.end() for comment in comments]
    starts = [0]
    starts.extend(match.end() for match in re.finditer(b'\n', source))
    result = {}
    for ordinal, symbol in enumerate(symbols):
        if symbol.kind not in {'function', 'syscall', 'prototype'}:
            continue
        offset = starts[min(symbol.start_line - 1, len(starts) - 1)]
        position = bisect_right(ends, offset) - 1
        if position < 0:
            continue
        comment = comments[position]
        if not _decorations_only(source[comment.end():offset]):
            continue
        lines = [re.sub(r'^\s*\* ?', '', line).rstrip() for line in comment.group(1).decode('utf-8', 'replace').splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        header = re.fullmatch(r'\s*(\w+)(?:\(\))?\s*-\s*(.*)', lines[0])
        if not header or header[1] != symbol.name:
            continue
        sections = {'summary': [header[2]], 'description': [], 'context': [], 'returns': []}
        parameters = {}
        destination = sections['summary']
        for line in lines[1:]:
            parameter = re.match(r'\s*@([\w.]+):\s*(.*)', line)
            section = re.match(r'\s*(Context|Returns?):\s*(.*)', line, re.I)
            if parameter:
                destination = parameters.setdefault(parameter[1], [])
                destination.append(parameter[2])
            elif section:
                key = 'context' if section[1].lower() == 'context' else 'returns'
                destination = sections[key]
                destination.append(section[2])
            elif _GENERIC_SECTION.match(line):
                # A named prose section after Context/Return must not become a
                # calling requirement merely because it follows one. Preserve
                # its title and contents in description instead of discarding it.
                destination = sections['description']
                if destination and destination[-1].strip():
                    destination.append('')
                destination.append(line)
            elif not line.strip() and (destination is sections['summary'] or any(destination is value for value in parameters.values())):
                destination = sections['description']
            else:
                destination.append(line)
        result[ordinal] = {
            'line': bisect_right(starts, comment.start()),
            **{key: '\n'.join(value).strip() for key, value in sections.items()},
            'parameters': {key: '\n'.join(value).strip() for key, value in parameters.items()},
        }
    return result
