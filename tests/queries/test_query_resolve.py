"""Literal paths, symbol selectors, ambiguity, and candidate ranking."""

import pytest

from kernel_atlas import db, query


def test_resolve_directory(conn):
    t = query.resolve(conn, "fs/ext4").target
    assert (t.kind, t.path) == ("dir", "fs/ext4")


def test_resolve_directory_with_slashes(conn):
    assert query.resolve(conn, "/fs/ext4/").target.path == "fs/ext4"
    assert query.resolve(conn, "./fs/ext4").target.path == "fs/ext4"


def test_resolve_root(conn):
    t = query.resolve(conn, ".").target
    assert t.kind == "dir" and t.path == ""


def test_resolve_file(conn):
    t = query.resolve(conn, "fs/ext4/inode.c").target
    assert (t.kind, t.name) == ("file", "inode.c")


def test_resolve_qualified_symbol(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:ext4_bmap").target
    assert t.kind == "symbol" and t.symbol_kind == "function"
    assert t.path == "fs/ext4/inode.c" and t.is_exported


def test_resolve_qualified_duplicate_symbol_reports_line_ambiguity(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "duplicate-qualified.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    row = writer.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_bmap'"
    ).fetchone()
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], row[3] + 100, row[4] + 100, *row[5:]))
    writer.commit()

    resolved = query.resolve(writer, "fs/ext4/inode.c:ext4_bmap")
    writer.close()
    assert len(resolved.candidates) == 1
    assert "use path:line" in resolved.note


def test_resolve_bare_symbol(conn):
    t = query.resolve(conn, "ext4_get_block").target
    assert t.kind == "symbol" and t.path == "fs/ext4/inode.c"


def test_resolve_by_line_number(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:3").target
    assert t.kind == "symbol" and t.name == "ext4_inode_blocks_set"


def test_line_resolution_prefers_the_smallest_enclosing_symbol(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "nested-line.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/ext4/inode.c'"
    ).fetchone()[0]
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'local_exact','struct',4,4,'struct local_exact')",
        (file_id,),
    )
    resolved = query.resolve(writer, "fs/ext4/inode.c:4")
    writer.close()

    assert resolved.target.name == "local_exact"
    assert resolved.target.line == resolved.target.end_line == 4
    assert resolved.candidates[0].name == "ext4_inode_blocks_set"


def test_resolve_ambiguous_reports_candidates(conn):
    res = query.resolve(conn, "super.c")
    assert res.target is not None
    assert res.candidates, "expected fs/ext4/super.c and fs/btrfs/super.c"
    assert "2 files" in res.note


def test_resolve_syscall_by_name(conn):
    t = query.resolve(conn, "sys_open").target
    assert t.symbol_kind == "syscall" and t.path == "fs/open.c"


def test_resolve_basename_colon_symbol_picks_the_right_file(conn):
    """'super.c:btrfs_mount' must find fs/btrfs/super.c even though
    fs/ext4/super.c has the shorter path — the symbol disambiguates."""
    t = query.resolve(conn, "super.c:btrfs_mount").target
    assert t is not None and t.path == "fs/btrfs/super.c"
    t = query.resolve(conn, "super.c:ext4_fill_super").target
    assert t.path == "fs/ext4/super.c"


def test_exact_file_with_unknown_symbol_gives_a_precise_error(conn):
    res = query.resolve(conn, "fs/ext4/inode.c:not_a_real_fn")
    assert res.target is None
    assert "defines no symbol" in res.note and "fs/ext4/inode.c" in res.note


def test_path_qualified_symbol_does_not_discard_wrong_directories(conn):
    res = query.resolve(conn, "wrong/place/super.c:btrfs_mount")
    assert res.target is None

    # Basename-only qualification remains an intentional disambiguation form.
    assert query.resolve(conn, "super.c:btrfs_mount").target.path == \
        "fs/btrfs/super.c"


@pytest.mark.parametrize("spec", [
    "fs/ext4/inode.c:0", "fs/ext4/inode.c:-1", "missing.c:0",
    "wrong/place.c:-1", ":0",
])
def test_qualified_line_numbers_must_be_positive(conn, spec):
    res = query.resolve(conn, spec)
    assert res.target is None
    assert "at least 1" in res.note


def test_qualified_line_number_must_fit_sqlite_integer(conn):
    res = query.resolve(conn, "fs/ext4/inode.c:" + "9" * 100)
    assert res.target is None
    assert "too large" in res.note


def test_resolve_basename_colon_line_picks_the_file_that_has_the_symbol(conn):
    """'super.c:1' used to use the shortest path (fs/ext4/super.c, an include)
    and miss btrfs_mount on line 1 of fs/btrfs/super.c."""
    t = query.resolve(conn, "super.c:1").target
    assert t is not None and t.name == "btrfs_mount"
    assert t.path == "fs/btrfs/super.c"


@pytest.mark.parametrize("spec", ["super.c:1", "super.c:01", "super.c:+01"])
def test_ambiguous_line_paths_match_every_positive_resolver_spelling(conn, spec):
    assert query.line_selector_suffix(spec) == spec.rpartition(":")[2]
    assert query.ambiguous_line_paths(conn, spec) == [
        "fs/ext4/super.c", "fs/btrfs/super.c",
    ]


def test_wholly_numeric_targets_are_not_line_selectors(conn):
    assert query.line_selector_suffix("8250") is None
    assert query.ambiguous_line_paths(conn, "8250") == []
    assert query.ambiguous_line_paths(conn, "fs/btrfs/super.c:1") == []


def test_parent_path_of_a_top_level_file_is_the_root():
    assert query.parent_path("Makefile") == ""
    assert query.parent_path("mm/page_alloc.c") == "mm"
    assert query.parent_path("") == ""


def test_resolve_missing(conn):
    res = query.resolve(conn, "definitely_not_here_xyz")
    assert res.target is None and "nothing in the index" in res.note


def test_rank_prefers_shallow_headers_over_nested_stubs_and_tools():
    def t(path, kind="macro", static=False):
        return query.Target(kind="symbol", id=1, path=path, name="GFP_KERNEL",
                            symbol_kind=kind, is_static=static)

    ranked = sorted(
        [t("include/linux/raid/pq.h"),
         t("include/linux/gfp_types.h"),
         t("tools/include/linux/gfp_types.h")],
        key=query._rank_candidate,
    )
    assert ranked[0].path == "include/linux/gfp_types.h"
    assert ranked[-1].path.startswith("tools/")


def test_resolve_returns_the_complete_candidate_set(mini_index, tmp_path):
    import shutil
    import sqlite3

    copied = tmp_path / "fanout.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='mm/page_alloc.c'").fetchone()[0]
    writer.executemany(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        "is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        [(file_id, "many_defs", "function", 1000 + i, 1000 + i,
          None, 0, 0, 0) for i in range(205)],
    )
    writer.commit()
    writer.close()

    conn = db.connect(copied, readonly=True)
    try:
        res = query.resolve_symbol(conn, "many_defs")
        assert res.target is not None
        assert len(res.candidates) == 204
        assert "205 symbols" in res.note
    finally:
        conn.close()


def test_bare_static_symbol_does_not_hide_an_area_directory(
        mini_index, tmp_path):
    import shutil
    import sqlite3

    copied = tmp_path / "namespaces.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    root_id = writer.execute("SELECT id FROM dirs WHERE path='' ").fetchone()[0]
    writer.execute(
        "INSERT INTO dirs(path,parent_id,name,depth) VALUES (?,?,?,?)",
        ("kernel/collision", root_id, "collision", 2),
    )
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='mm/page_alloc.c'").fetchone()[0]
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        "is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (file_id, "collision", "function", 300, 300, None, 1, 0, 0),
    )
    writer.commit()
    writer.close()

    conn = db.connect(copied, readonly=True)
    try:
        generic = query.resolve(conn, "collision")
        assert generic.target.kind == "dir"
        assert generic.target.path == "kernel/collision"
        assert any(c.kind == "symbol" for c in generic.candidates)
        assert query.resolve_symbol(conn, "collision").target.kind == "symbol"
    finally:
        conn.close()


def test_ranking_prefers_kernel_paths_to_tools_copies_even_if_static():
    kernel = query.Target(kind="symbol", id=1, path="drivers/x/deep.c",
                          name="pick", symbol_kind="function", is_static=True)
    tools = query.Target(kind="symbol", id=2, path="tools/x.c",
                         name="pick", symbol_kind="function", is_static=False)
    assert min((tools, kernel), key=query._rank_candidate) is kernel
