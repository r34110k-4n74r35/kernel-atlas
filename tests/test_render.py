from kernel_atlas import render, structure_render
from kernel_atlas.query import Entry
from kernel_atlas.render import (entry_dict, human_size, render_plain, render_table,
                                 render_tree)


def _syms():
    return [
        Entry(kind="function", name="a_fn", path="fs/x.c", line=10, end_line=12),
        Entry(kind="function", name="b_fn", path="fs/x.c", line=20, end_line=25),
    ]


def test_empty_no_color_environment_variable_disables_auto_color(
        monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setattr(render.sys.stdout, "isatty", lambda: True)
    assert not render.use_color("auto")


def test_tree_format_gives_each_symbol_its_own_leaf():
    """Regression: two symbols in one file used to collapse into one node."""
    out = render_tree(_syms(), color=False)
    assert "a_fn:10" in out
    assert "b_fn:20" in out


def test_tree_format_nests_files_under_directories():
    entries = [Entry(kind="file", name="x.c", path="fs/ext4/x.c"),
               Entry(kind="dir", name="ext4", path="fs/ext4")]
    out = render_tree(entries, color=False)
    assert "ext4/" in out and "x.c" in out


def test_plain_format_is_grep_shaped_for_symbols():
    assert render_plain(_syms()).splitlines() == ["fs/x.c:10:a_fn", "fs/x.c:20:b_fn"]


def test_plain_format_omits_a_missing_line_number():
    e = Entry(kind="function", name="x", path="a.c")
    assert render_plain([e]).strip() == "a.c:x"


def test_human_size_zero_is_zero_bytes_not_a_dash():
    assert human_size(None) == "-"
    assert human_size(0) == "0B"
    assert human_size(512) == "512B"
    assert human_size(1024) == "1.0K"


def test_table_alignment_header_matches_rows():
    out = render_table(_syms(), ["kind", "name", "line"], color=False)
    header, first, _ = out.splitlines()
    assert header.index("NAME") == first.index("a_fn")


def test_tree_keeps_same_name_same_line_symbols_distinct():
    entries = [
        Entry(kind="union", name="word", path="include/x.h", line=8),
        Entry(kind="typedef", name="word", path="include/x.h", line=8),
    ]
    out = render_tree(entries, color=False)
    assert out.count("word:8") == 2
    assert "[union]" in out and "[typedef]" in out


def test_tree_preserves_the_input_order_for_sort_modes():
    entries = [
        Entry(kind="function", name="late_name", path="x.c", line=1),
        Entry(kind="function", name="early_name", path="x.c", line=20),
    ]
    out = render_tree(entries, color=False)
    assert out.index("late_name") < out.index("early_name")


def test_explicit_json_columns_are_exact_and_keep_nulls():
    e = Entry(kind="file", name="x.c", path="fs/x.c", is_target=True)
    assert entry_dict(e, ["name", "line"]) == {
        "name": "x.c", "line": None, "is_target": True,
    }


def test_path_json_does_not_claim_symbol_linkage():
    row = entry_dict(Entry(kind="dir", name="mm", path="mm"))
    assert "is_static" not in row
    assert "is_inline" not in row
    assert "is_exported" not in row


def test_structure_report_renders_nested_members_without_color():
    payload = {
        "kind": "struct", "name": "sample", "tag": "sample",
        "c_name": "struct sample", "path": "include/sample.h", "line": 10,
        "end_line": 20, "index": "7.2", "is_anonymous": False,
        "signature": "struct sample __packed { 1 member }",
        "aliases": ["sample_t"], "direct_member_count": 1,
        "total_member_count": 2, "documentable_member_count": 1,
        "documented_member_count": 1,
        "semantic_description_count": 2,
        "documentation_coverage": 1.0, "parse_complete": True,
        "summary": "A sample structure.", "description": "Long notes.",
        "subsystems": [], "warnings": [], "unmatched_member_docs": {},
        "related_documentation": [], "links": {}, "layout_limits": [],
        "members": [{
            "ordinal": 0, "name": None, "kind": "union", "type": "union",
            "declaration": "union { int value; };", "line": 12,
            "end_line": 14, "bit_width": None, "array_dimensions": [],
            "description": None, "description_source": None,
            "conditions": [], "visibility": "unspecified",
            "is_anonymous": True, "generated_by": None,
            "referenced_kind": None, "referenced_name": None,
            "children": [{
                "ordinal": 1, "name": "value", "kind": "field",
                "type": "int", "declaration": "int value;", "line": 13,
                "end_line": 13, "bit_width": None, "array_dimensions": [],
                "description": "Stored value.",
                "description_source": "kernel-doc", "conditions": [],
                "visibility": "unspecified", "is_anonymous": False,
                "generated_by": None, "referenced_kind": None,
                "referenced_name": None, "children": [],
            }],
        }],
    }
    out = render.render_structure(payload, color=False, max_width=72)
    assert "struct sample" in out
    assert "struct sample __packed" in out
    assert "<anonymous>" in out and "value" in out
    assert "Stored value. [kernel-doc]" in out
    assert "2 parser-supplied macro explanations" in out
    assert "(undocumented)" in out
    assert "\x1b[" not in out


def test_structure_report_labels_anonymous_typedef_and_missing_source():
    payload = {
        "kind": "union", "name": "value_t", "tag": None, "c_name": None,
        "path": "include/value.h", "line": 1, "end_line": 1,
        "is_anonymous": True, "aliases": ["value_t"], "signature": None,
        "source_path": "/missing/linux/include/value.h",
        "source_exists": False, "subsystems": [],
        "unclassified_ownership": {"unmatched": True},
        "direct_member_count": 0, "total_member_count": 0,
        "documentable_member_count": 0, "documented_member_count": 0,
        "documentation_coverage": 0.0, "parse_complete": True,
        "members": [], "warnings": [], "unmatched_member_docs": {},
        "related_documentation": [], "links": {}, "layout_limits": [],
    }
    out = render.render_structure(payload, color=False)
    assert "value_t (typedef to anonymous union)" in out
    assert "/missing/linux/include/value.h (missing)" in out
    assert "no primary MAINTAINERS match" in out


def test_structure_report_uses_the_render_facade_paint_binding(monkeypatch):
    detail = {
        "kind": "struct",
        "name": "sample",
        "c_name": "struct sample",
        "path": "include/sample.h",
        "line": 1,
        "members": [],
    }

    def marked(text, code, enabled):
        return f"<{code}>{text}</{code}>" if enabled else text

    monkeypatch.setattr(render, "paint", marked)
    out = render.render_structure(detail, color=True)
    direct = structure_render.render_structure(detail, color=True)

    assert out.startswith("<1;36>struct sample</1;36>")
    assert "<1>  Members (source order)</1>" in out
    assert direct.startswith("\x1b[1;36mstruct sample\x1b[0m")
