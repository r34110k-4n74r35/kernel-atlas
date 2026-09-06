"""Call-site extraction, lexical bindings, and conditional macro state.

The parser facade owns the tree-sitter parser and query lifecycle and passes
its call query explicitly. This module only consumes source bytes and nodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Query, QueryCursor

from .models import CallSite
from .syntax import (
    IDENTIFIERS as _IDENTIFIERS,
    declarator_name as _declarator_name,
    is_function_prototype as _is_function_prototype,
    safe_declarators as _safe_declarators,
    source_sorted_nodes as _source_sorted_nodes,
    squash as _squash,
    text as _text,
)

_ConditionalPath = tuple[tuple[int, int], ...]
_MacroEvent = tuple[int, bool, _ConditionalPath]


@dataclass(frozen=True, slots=True)
class _MacroTransitions:
    events: dict[str, tuple[_MacroEvent, ...]]
    # A conditional group chooses exactly one explicit branch. ``None`` is the
    # implicit fall-through choice of a group which has no final ``#else``.
    choices: dict[int, tuple[int | None, ...]]


_EMPTY_MACRO_TRANSITIONS = _MacroTransitions({}, {})


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



def _macro_transitions(src: bytes, root) \
        -> _MacroTransitions:
    """Return guarded source-order changes for in-file macro state.

    Each guard maps a conditional node to the branch containing the directive.
    This distinguishes mutually exclusive ``#if``/``#else`` bodies while still
    treating state observed after ``#endif`` as configuration-dependent.
    """
    transitions: dict[str, list[_MacroEvent]] = {}
    choices: dict[int, tuple[int | None, ...]] = {}

    stack = [root]
    while stack:
        current = stack.pop()
        if current.type in {"preproc_if", "preproc_ifdef"}:
            branches: list[int | None] = [current.start_byte]
            branch = next((
                child for child in current.named_children
                if child.type in {"preproc_elif", "preproc_else"}
            ), None)
            while branch is not None:
                branches.append(branch.start_byte)
                if branch.type == "preproc_else":
                    break
                branch = next((
                    child for child in branch.named_children
                    if child.type in {"preproc_elif", "preproc_else"}
                ), None)
            else:
                # No final #else: the group can select no explicit body.
                branches.append(None)
            choices[current.start_byte] = tuple(branches)
        if current.type in {"preproc_def", "preproc_function_def"}:
            name_node = current.child_by_field_name("name")
            if name_node is None:
                name_node = next((child for child in current.named_children
                                  if child.type == "identifier"), None)
            if name_node is not None:
                transitions.setdefault(_text(src, name_node), []).append((
                    current.end_byte, True, _conditional_path(current),
                ))
            continue
        if current.type == "preproc_call":
            directive = next((child for child in current.named_children
                              if child.type == "preproc_directive"), None)
            argument = next((child for child in current.named_children
                             if child.type == "preproc_arg"), None)
            if directive is not None and argument is not None \
                    and _text(src, directive).strip() == "#undef":
                match = re.match(r"[A-Za-z_]\w*", _text(src, argument).lstrip())
                if match is not None:
                    transitions.setdefault(match.group(), []).append((
                        current.end_byte, False, _conditional_path(current),
                    ))
            continue
        stack.extend(reversed(current.named_children))
    return _MacroTransitions(
        {name: tuple(sorted(events)) for name, events in transitions.items()},
        choices,
    )



def _conditional_group(branch):
    """Root ``#if`` node for one tree-sitter ``#elif``/``#else`` node."""
    group = branch.parent
    while group is not None and group.type == "preproc_elif":
        group = group.parent
    return group if group is not None and group.type in {
        "preproc_if", "preproc_ifdef",
    } else None



def _conditional_path(node) -> _ConditionalPath:
    """Conditional-group/branch identities enclosing one syntax node."""
    branches: dict[int, int] = {}
    current = node
    parent = node.parent
    while parent is not None:
        if parent.type.startswith(("preproc_else", "preproc_elif")):
            group = _conditional_group(parent)
            if group is not None:
                # An #else after one or more #elif nodes is nested below those
                # nodes in tree-sitter's AST. Preserve the nearest branch rather
                # than overwriting it while walking through the chain.
                branches.setdefault(group.start_byte, parent.start_byte)
        elif parent.type.startswith("preproc_if") \
                and not current.type.startswith((
                    "preproc_else", "preproc_elif")):
            branches.setdefault(parent.start_byte, parent.start_byte)
        current, parent = parent, parent.parent
    return tuple(sorted(branches.items()))



def _macro_is_active_overapprox(
        events: tuple[_MacroEvent, ...], offset: int,
        call_branches: dict[int, int]) -> bool:
    """Sound fallback when exact conditional enumeration would be excessive."""
    states = {False}
    for event_offset, defined, event_path in events:
        if event_offset > offset:
            break
        if any(group in call_branches and call_branches[group] != branch
               for group, branch in event_path):
            continue
        mandatory = all(call_branches.get(group) == branch
                        for group, branch in event_path)
        if mandatory:
            states = {defined}
        else:
            states.add(defined)
    return True in states



def _macro_is_active(
        transitions: _MacroTransitions,
        name: str, offset: int,
        call_path: tuple[tuple[int, int], ...] = ()) -> bool:
    """Whether ``name`` can be defined in a configuration reaching a call."""
    events = transitions.events.get(name, ())
    call_branches = dict(call_path)
    relevant_events = tuple(
        event for event in events if event[0] <= offset and not any(
            group in call_branches and call_branches[group] != branch
            for group, branch in event[2]
        )
    )
    if not relevant_events:
        return False

    groups = sorted({
        group for _, _, event_path in relevant_events
        for group, _ in event_path if group not in call_branches
    })
    assignments: list[dict[int, int | None]] = [dict(call_branches)]
    for group in groups:
        group_choices = transitions.choices.get(group)
        if group_choices is None:
            # Malformed/recovered preprocessor syntax: keep an implicit branch
            # as well as every observed branch so this remains conservative.
            group_choices = tuple(dict.fromkeys((
                *(branch for _, _, path in relevant_events
                  for candidate_group, branch in path
                  if candidate_group == group),
                None,
            )))
        if len(assignments) * len(group_choices) > 4096:
            return _macro_is_active_overapprox(
                relevant_events, offset, call_branches)
        assignments = [
            {**assignment, group: branch}
            for assignment in assignments for branch in group_choices
        ]

    for assignment in assignments:
        defined = False
        for _, event_defined, event_path in relevant_events:
            if all(assignment.get(group) == branch
                   for group, branch in event_path):
                defined = event_defined
        if defined:
            return True
    return False



def _call_target(src: bytes, node) -> tuple[str, bool] | None:
    """Return a stable display name and whether syntax is inherently indirect."""
    current = node.child_by_field_name("function") \
        if node.type == "call_expression" else node
    indirect = False
    for _ in range(32):
        if current is None:
            return None
        if current.type == "identifier":
            return _text(src, current), indirect
        if current.type == "field_expression":
            raw = _squash(_text(src, current), 200)
            raw = re.sub(r"\s*(->|\.)\s*", r"\1", raw)
            return raw, True
        if current.type == "pointer_expression":
            argument = current.child_by_field_name("argument")
            operand = argument or next(iter(current.named_children), None)
            if operand is None:
                return None
            raw = re.sub(r"\s+", "", _text(src, current))
            return raw[:200], True
        if current.type in {
                "subscript_expression", "conditional_expression",
                "cast_expression"}:
            raw = _squash(_text(src, current), 200)
            raw = re.sub(r"\s*([\[\]])\s*", r"\1", raw)
            return raw, True
        if current.type in {"parenthesized_expression", "attributed_expression"}:
            current = next(iter(current.named_children), None)
            continue
        return None
    return None



def _call_site(
        src: bytes, node, bindings: dict[str, list[tuple[int, int]]],
        transitions: _MacroTransitions) -> CallSite | None:
    target = _call_target(src, node)
    if target is None:
        return None
    name, syntactic_indirect = target
    if not name or "\0" in name:
        return None
    function_node = node.child_by_field_name("function")
    bare_identifier = function_node is not None \
        and function_node.type == "identifier"
    macro_active = bare_identifier and _macro_is_active(
        transitions, name, node.start_byte, _conditional_path(node))
    if macro_active:
        kind = "macro"
    elif syntactic_indirect or any(
            start <= node.start_byte < end
            for start, end in bindings.get(name, ())):
        kind = "indirect"
    else:
        kind = "direct"
    return CallSite(
        name=name, kind=kind, start_line=node.start_point[0] + 1,
        start_byte=node.start_byte,
    )



def _summarize_call_sites(
        sites: tuple[CallSite, ...]
        ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[CallSite, ...]]:
    calls = tuple(dict.fromkeys(site.name for site in sites))
    indirect = tuple(dict.fromkeys(
        site.name for site in sites if site.kind == "indirect"))
    return calls, indirect, sites



def _collect_call_details(
        src: bytes, node, query: Query, end_byte: int | None = None,
        transitions: _MacroTransitions | None = None,
        ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[CallSite, ...]]:
    """Callee names, indirect names, and source-level occurrence evidence.

    Accepts either a function_definition or a bare compound_statement — the
    latter is what SYSCALL_DEFINEn leaves us with, where the body is a sibling
    of the macro call rather than a child of anything function-shaped.
    """
    body = node if node.type == "compound_statement" else \
        node.child_by_field_name("body")
    if body is None:
        return (), (), ()
    cursor = QueryCursor(query)
    caps = cursor.captures(body)
    bindings = _local_object_bindings(src, node, body, end_byte)
    transitions = transitions or _EMPTY_MACRO_TRANSITIONS
    sites: list[CallSite] = []
    for call in _source_sorted_nodes(caps.get("call", [])):
        if end_byte is not None and call.start_byte >= end_byte:
            continue
        site = _call_site(src, call, bindings, transitions)
        if site is not None:
            sites.append(site)
    return _summarize_call_sites(tuple(sites))
