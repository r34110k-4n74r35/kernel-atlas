"""Only adjacent, correctly named kernel-doc supplies function requirements."""

import pytest

from kernel_atlas.parsing.contracts import _decorations_only, function_contracts
from .helpers import parse


def contracts(source):
    symbols = parse(source, kinds={"function", "prototype", "struct"})
    return {symbols[ordinal].name: contract for ordinal, contract
            in function_contracts(source.encode(), symbols).items()}


def test_contract_preserves_multiline_parameters_context_and_returns():
    source = """/* unrelated introductory comment */
/**
 * take_ref() - Acquire a reference
 * @item: The caller's item,
 *   which must remain alive.
 * @flags: Allocation flags.
 *
 * The caller owns the returned reference.
 *
 * Context: Process context.
 *   May sleep when allocating memory.
 * Return: A reference, or NULL.
 */
int take_ref(int item, int flags) { return item; }
"""
    result = contracts(source)["take_ref"]
    assert result["line"] == 2
    assert result["summary"] == "Acquire a reference"
    assert result["parameters"] == {
        "item": "The caller's item,\n  which must remain alive.",
        "flags": "Allocation flags.",
    }
    assert result["description"] == "The caller owns the returned reference."
    assert result["context"] == "Process context.\n  May sleep when allocating memory."
    assert result["returns"] == "A reference, or NULL."


def test_prototype_contract_is_retained_and_missing_fields_stay_empty():
    result = contracts("/**\n * ask - Query a value\n */\nint ask(int input);\n")["ask"]
    assert result["summary"] == "Query a value"
    assert result["parameters"] == {}
    assert result["context"] == result["returns"] == result["description"] == ""


def test_wrong_name_intervening_declaration_and_non_function_docs_are_omitted():
    assert contracts("""
/**
 * other - belongs to another function
 */
int named(void) { return 0; }
/**
 * later - separated by a declaration
 */
int unrelated;
int later(void) { return 0; }
/**
 * holder - a structure, not a function contract
 */
struct holder { int field; };
int undocumented(void) { return 0; }
""") == {}


def test_nearest_matching_comment_is_not_reused_for_later_definitions():
    result = contracts("""/**
 * item - documented prototype
 */
int item(void);
int item(void) { return 0; }
""")
    assert result["item"]["summary"] == "documented prototype"
    source = "/**\n * a - first\n */\nint a(void) {}\nint b(void) {}\n"
    assert list(contracts(source)) == ["a"]


@pytest.mark.parametrize("decoration", [
    "__printf(1, 2)", "__must_check", "__must_check\n__printf(1, 2)",
    "__attribute__((format(printf, 1, 2)))", "__acquires(&object->lock)",
])
def test_declaration_decorations_do_not_disconnect_adjacent_documentation(decoration):
    source = ("/**\n * report - Report a message\n * @fmt: Format text.\n */\n"
              + decoration + "\nint report(const char *fmt, ...);\n")
    result = contracts(source)["report"]
    assert result["summary"] == "Report a message"
    assert result["line"] == 1
    assert result["parameters"] == {"fmt": "Format text."}


@pytest.mark.parametrize("gap", [
    b"unrelated();", b"int unrelated;", b"NOT_AN_ATTRIBUTE(1)",
    b"__printf(1, 2);", b"__must_check unrelated", b"__printf", b"__printf(1, 2",
    b"__must_check()", b"__aligned(({ unrelated(); 1; }))", b"#if SOMETHING",
])
def test_only_known_complete_decorations_can_bridge_documentation(gap):
    assert not _decorations_only(gap)


def test_annotation_does_not_skip_an_intervening_declaration():
    source = """/**
 * target - Not adjacent to target.
 */
__must_check int unrelated(void);
int target(void);
"""
    assert "target" not in contracts(source)


def test_named_prose_sections_do_not_become_context_or_return_requirements():
    result = contracts("""/**
 * inspect - Inspect an object.
 *
 * Initial explanation.
 *
 * Context: Process context.
 *   The caller holds a lock.
 *
 * Notes:
 *   Additional background, not an execution-context requirement.
 *
 * Return: Zero when successful.
 *
 * Example: A typical use follows.
 *   inspect();
 *
 * Locking:
 *   Descriptive locking notes remain available.
 */
int inspect(void) { return 0; }
""")["inspect"]
    assert result["context"] == "Process context.\n  The caller holds a lock."
    assert result["returns"] == "Zero when successful."
    assert result["description"] == (
        "Initial explanation.\n\nNotes:\n"
        "  Additional background, not an execution-context requirement.\n\n"
        "Example: A typical use follows.\n  inspect();\n\nLocking:\n"
        "  Descriptive locking notes remain available.")
