"""Conservative recovery of file-scope definitions from malformed kernel C.

Unexpanded macros can hide otherwise valid declarations in tree-sitter ERROR
nodes. Recovery uses bounded source spans, file-scope evidence, and explicit
exports; normal query dispatch and parser ownership stay in cparse.py.
"""

from __future__ import annotations

import re

from .calls import (
    _EMPTY_MACRO_TRANSITIONS,
    _MacroTransitions,
    _call_site,
    _summarize_call_sites,
)
from .models import FUNCTION, VARIABLE, CallSite, Symbol
from .syntax import (
    ATTRIBUTE_MACROS as _ATTRIBUTE_MACROS,
    INLINE_SPECIFIERS as _INLINE_SPECIFIERS,
    function_signature as _function_signature,
    matching_delimiter as _matching_delimiter,
    source_code_leaf as _source_code_leaf,
    source_sorted_nodes as _source_sorted_nodes,
    squash as _squash,
)


_SOURCE_EXPORT_RE = re.compile(
    rb"(?m)(?:^[ \t]*|}[ \t]*)EXPORT(?:_PER_CPU)?_SYMBOL"
    rb"(?:_GPL|_NS|_NS_GPL|_FOR_MODULES)?[ \t]*\([ \t\r\n]*"
    rb"([A-Za-z_]\w*)[ \t\r\n]*(?=[,)])")



_RECOVERED_DECL_PREFIX = re.compile(
    r"(?:[A-Za-z_]\w*|\*+)(?:[ \t]+(?:[A-Za-z_]\w*|\*+))*[ \t]*$")



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
                             call_nodes: list | None = None,
                             transitions: _MacroTransitions | None = None,
                             ) -> list[Symbol]:
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
                indirect_calls: tuple[str, ...] = ()
                call_sites: tuple[CallSite, ...] = ()
                if call_nodes is not None:
                    sites = tuple(
                        site
                        for node in _source_sorted_nodes(call_nodes)
                        if opening < node.start_byte < closing
                        if (site := _call_site(
                            src, node, {},
                            transitions or _EMPTY_MACRO_TRANSITIONS)) is not None
                    )
                    calls, indirect_calls, call_sites = \
                        _summarize_call_sites(sites)
                out.append(Symbol(
                    name=name, kind=FUNCTION, start_line=start, end_line=end,
                    signature=_function_signature(
                        src[definition_start:params_end + 1].decode(
                            "utf-8", "replace")),
                    is_static="static" in words,
                    is_inline=any(word in _INLINE_SPECIFIERS for word in words),
                    is_exported=True, calls=calls,
                    indirect_calls=indirect_calls, call_sites=call_sites,
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
