from kernel_atlas.query import Entry
from kernel_atlas.render import human_size, render_plain, render_table, render_tree


def _syms():
    return [
        Entry(kind="function", name="a_fn", path="fs/x.c", line=10, end_line=12),
        Entry(kind="function", name="b_fn", path="fs/x.c", line=20, end_line=25),
    ]


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
