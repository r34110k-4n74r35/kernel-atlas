"""Type-position evidence distinguishes types from names and expressions."""

import pytest

from kernel_atlas.parsing.type_references import declaration_references


@pytest.mark.parametrize(("declaration", "name", "role", "pointers"), [
    ("struct packet value;", "value", "embedded_member", 0),
    ("struct packet **value;", "value", "pointer_member", 2),
    ("struct packet *pointer, value;", "value", "embedded_member", 0),
    ("struct packet *pointer, value;", "pointer", "pointer_member", 1),
    ("const struct packet __rcu *value;", "value", "pointer_member", 1),
])
def test_member_type_references_follow_the_selected_declarator(
        declaration, name, role, pointers):
    result = declaration_references(declaration, name=name)
    assert len(result) == 1
    assert (result[0].name, result[0].kind) == ("packet", "struct")
    assert (result[0].role, result[0].pointer_depth) == (role, pointers)


def test_arrays_and_direct_typedef_spellings_remain_distinct():
    reference, = declaration_references("packet_t values[3];", name="values")
    assert reference.kind is None and reference.name == "packet_t"
    assert reference.is_array and reference.role == "embedded_member"


def test_callbacks_retain_return_and_parameter_types():
    result = declaration_references(
        "struct packet *(*submit)(union address *, packet_t packet);", name="submit")
    assert [(ref.name, ref.kind, ref.role, ref.pointer_depth) for ref in result] == [
        ("packet", "struct", "callback_return", 1),
        ("address", "union", "callback_parameter", 1),
        ("packet_t", None, "callback_parameter", 0),
    ]
    assert result[-1].parameter == "packet"


def test_function_parameters_containing_callbacks_keep_callback_roles():
    result = declaration_references(
        "struct packet *build(union address *address, "
        "struct result (*callback)(struct task *));", name="build", function=True)
    assert [(ref.name, ref.role) for ref in result] == [
        ("packet", "function_return"), ("address", "function_parameter"),
        ("result", "callback_return"), ("task", "callback_parameter"),
    ]


def test_function_returning_callback_does_not_claim_callback_result_as_direct_return():
    result = declaration_references(
        "struct packet (*factory(struct context *))(struct argument *);",
        name="factory", function=True)
    assert [(ref.name, ref.role) for ref in result] == [
        ("packet", "callback_return"), ("argument", "callback_parameter"),
        ("context", "function_parameter"),
    ]


@pytest.mark.parametrize(("declaration", "name"), [
    ("int packet;", "packet"),
    ("int values[sizeof(struct packet)];", "values"),
    ("int value; /* struct packet */", "value"),
    ("struct packet { int n; } value;", "value"),
    ("DECLARE_OBJECT(struct packet, value);", "value"),
    ("struct packet value…", "value"),
    ("struct packet *real;", "wrong"),
])
def test_non_type_positions_inline_definitions_and_incomplete_fragments_are_omitted(
        declaration, name):
    assert declaration_references(declaration, name=name) == ()
