"""Conservative type references from indexed C declaration fragments.

Only AST type positions count: member names, comments, array expressions and
attribute arguments are never treated as references. This is deliberately not
a typedef expander or a C preprocessor.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from tree_sitter import Language, Parser
import tree_sitter_c

from .syntax import ATTRIBUTE_MACROS, CALL_ATTRIBUTE_MACROS, declarator_name, text


@dataclass(frozen=True, slots=True)
class TypeReference:
    name: str
    kind: str | None
    role: str
    pointer_depth: int
    is_array: bool
    parameter: str | None = None


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_c.language()))


def _chain(node) -> list:
    result = []
    while node is not None and len(result) < 64:
        result.append(node)
        following = node.child_by_field_name("declarator")
        if following is None and node.type in {
                "parenthesized_declarator", "abstract_parenthesized_declarator"}:
            following = next(iter(node.named_children), None)
        node = following
    return result


def _reference(src: bytes, type_node, chain: list, role: str,
               parameter: str | None) -> TypeReference | None:
    if type_node is None:
        return None
    kind = None
    if type_node.type in {"struct_specifier", "union_specifier", "enum_specifier"}:
        if type_node.child_by_field_name("body") is not None:
            # An inline definition is not a use of some globally named type.
            return None
        kind = type_node.type.removesuffix("_specifier")
        name_node = type_node.child_by_field_name("name")
        if name_node is None:
            return None
        name = text(src, name_node)
    elif type_node.type == "type_identifier":
        name = text(src, type_node)
    else:
        return None
    pointers = sum(node.type in {"pointer_declarator", "abstract_pointer_declarator"}
                   for node in chain)
    arrays = any(node.type in {"array_declarator", "abstract_array_declarator"}
                 for node in chain)
    if role == "member":
        role = "pointer_member" if pointers else "embedded_member"
    return TypeReference(name, kind, role, pointers, arrays, parameter)


def declaration_references(declaration: str, *, name: str | None,
                           function: bool = False) -> tuple[TypeReference, ...]:
    """Read one member declarator or a function's signature.

    ``name`` selects the relevant declarator in ``struct x *a, b``. Function
    pointer return and parameter types are labelled as callback signatures,
    including a callback passed as a parameter. Truncated or malformed
    fragments yield no speculative relationships.
    """
    if not declaration or "…" in declaration:
        return ()
    # Sparse/address-space qualifiers are known tokens rather than C grammar.
    qualifiers = ATTRIBUTE_MACROS - CALL_ATTRIBUTE_MACROS
    declaration = re.sub(
        r"\b[A-Za-z_]\w*\b",
        lambda match: " " * len(match[0]) if match[0] in qualifiers else match[0],
        declaration,
    )
    fragment = declaration.rstrip().rstrip(";") + ";"
    wrapped = fragment if function else "struct __ka_fragment { " + fragment + " };"
    src = wrapped.encode("utf-8")
    root = _parser().parse(src).root_node
    if root.has_error:
        return ()
    if function:
        field = next((node for node in root.named_children
                      if node.type == "declaration"), None)
    else:
        aggregate = next((node for node in root.named_children
                          if node.type == "struct_specifier"), None)
        body = aggregate.child_by_field_name("body") if aggregate else None
        field = next((node for node in body.named_children
                      if node.type == "field_declaration"), None) if body else None
    if field is None:
        return ()
    declarators = field.children_by_field_name("declarator")
    selected = []
    for declarator in declarators:
        identifier = declarator_name(declarator)
        if identifier is not None and text(src, identifier) == name:
            selected.append(declarator)
    if len(selected) != 1:
        return ()

    result: list[TypeReference] = []

    def visit(type_node, declarator, role, parameter=None, outer_function=False):
        chain = _chain(declarator)
        functions = [node for node in chain if node.type in {
            "function_declarator", "abstract_function_declarator"}]
        if functions:
            first = chain.index(functions[0])
            return_role = ("function_return" if outer_function and len(functions) == 1
                           else "callback_return")
            reference = _reference(src, type_node, chain[:first], return_role, parameter)
        else:
            reference = _reference(src, type_node, chain, role, parameter)
        if reference is not None:
            result.append(reference)
        for func in functions:
            params = func.child_by_field_name("parameters")
            if params is None:
                continue
            param_role = ("function_parameter" if outer_function and func == functions[-1]
                          else "callback_parameter")
            for param in params.named_children:
                if param.type != "parameter_declaration":
                    continue
                decl = param.child_by_field_name("declarator")
                identifier = declarator_name(decl)
                param_name = text(src, identifier) if identifier is not None else None
                visit(param.child_by_field_name("type"), decl, param_role, param_name)

    visit(field.child_by_field_name("type"), selected[0],
          "function_return" if function else "member", outer_function=function)
    return tuple(result)
