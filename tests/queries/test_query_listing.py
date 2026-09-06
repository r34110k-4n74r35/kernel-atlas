"""Listing scopes, sibling filters, search, and result bounds."""

from kernel_atlas.storage import db
from kernel_atlas.queries import query

from .helpers import names, sib


def test_file_siblings_are_files_in_the_same_directory(conn):
    assert names(sib(conn, "fs/ext4/inode.c")) == ["Makefile", "super.c"]


def test_directory_siblings_are_dirs_under_the_same_parent(conn):
    assert names(sib(conn, "fs/ext4")) == ["btrfs"]


def test_top_level_directory_siblings(conn):
    got = names(sib(conn, "mm"))
    assert "fs" in got and "net" in got and "mm" not in got


def test_symbol_siblings_default_to_the_same_file(conn):
    got = names(sib(conn, "fs/ext4/inode.c:ext4_bmap"))
    assert got == ["ext4_get_block", "ext4_helper", "ext4_inode_blocks_set"]


def test_symbol_siblings_at_directory_level(conn):
    got = names(sib(conn, "fs/ext4/inode.c:ext4_bmap", level="dir"))
    assert "ext4_fill_super" in got and "ext4_remount" in got
    assert "ext4_get_block" in got


def test_symbol_siblings_at_subsystem_level(conn):
    got = names(sib(conn, "fs/ext4/inode.c:ext4_bmap", level="subsystem"))
    assert "ext4_fill_super" in got
    assert "path_lookup" not in got, "fs/namei.c is VFS, not ext4"


def test_level_widens_from_dir_to_subsystem(conn):
    dir_level = sib(conn, "net/ipv4/tcp.c", level="dir")
    sub_level = sib(conn, "net/ipv4/tcp.c", level="subsystem")
    assert names(dir_level) == ["udp.c"]
    assert "udp.c" in names(sub_level)


def test_subtree_level(conn):
    got = names(sib(conn, "fs/ext4", level="subtree", kinds=("file",)))
    assert set(got) == {"Makefile", "inode.c", "super.c"}


def test_kinds_override_lets_you_ask_for_symbols_next_to_a_file(conn):
    got = names(sib(conn, "fs/ext4/inode.c", kinds=("function",)))
    assert "ext4_fill_super" in got and "ext4_bmap" in got


def test_kinds_override_lets_you_ask_for_files_next_to_a_symbol(conn):
    got = names(sib(conn, "fs/ext4/inode.c:ext4_bmap", level="dir", kinds=("file",)))
    assert set(got) == {"Makefile", "inode.c", "super.c"}


def test_include_self_marks_the_target(conn):
    entries = sib(conn, "fs/ext4/inode.c", include_self=True)
    assert "inode.c" in names(entries)


def test_grep_filter(conn):
    got = names(sib(conn, "fs/ext4/inode.c:ext4_bmap", grep="^ext4_get"))
    assert got == ["ext4_get_block"]


def test_exported_filter(conn):
    scope = query.build_scope(conn, query.resolve(conn, "mm/page_alloc.c").target, "file")
    got = names(query.collect(conn, scope, ("function",), exported_only=True))
    assert got == ["__alloc_pages"]


def test_static_filters(conn):
    t = query.resolve(conn, "fs/ext4/inode.c").target
    scope = query.build_scope(conn, t, "file")
    assert "ext4_bmap" not in names(query.collect(conn, scope, ("function",),
                                                  static="only"))
    assert "ext4_bmap" in names(query.collect(conn, scope, ("function",),
                                              static="exclude"))


def test_limit(conn):
    assert len(sib(conn, "fs/ext4/inode.c:ext4_bmap", limit=2)) <= 2


def test_bounded_symbol_listing_uses_the_final_tie_break_order(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "bounded-order.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/ext4/inode.c'").fetchone()[0]
    writer.executemany(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line)"
        " VALUES (?,'aaa_conditional','function',?,?)",
        [(file_id, line, line) for line in range(12, 0, -1)])
    writer.commit()
    target = query.resolve(writer, "fs/ext4/inode.c").target
    scope = query.build_scope(writer, target, "file")

    entries = query.collect(
        writer, scope, ("function",), limit=3, sort="name")
    writer.close()
    conditional = [entry for entry in entries
                   if entry.name == "aaa_conditional"]
    assert [entry.line for entry in conditional] == [1, 2, 3]


def test_search_substring_and_exact(conn):
    assert "ext4_bmap" in names(query.search(conn, "bmap"))
    assert names(query.search(conn, "bmap", mode="exact")) == []
    assert names(query.search(conn, "ext4_bmap", mode="exact")) == ["ext4_bmap"]
    assert "ext4_bmap" in names(query.search(conn, "EXT4_BMAP", mode="substring"))
    assert names(query.search(conn, "EXT4_BMAP", mode="exact")) == []


def test_search_glob_and_kinds(conn):
    got = names(query.search(conn, "ext4_*", mode="glob", kinds=("function",)))
    assert "ext4_bmap" in got and "ext4_sb_info" not in got


def test_search_applies_static_and_regex_filters_before_limit(conn):
    got = query.search(conn, "ext4", limit=1, grep="helper$", static="only",
                       with_subsystem=False)
    assert [e.name for e in got] == ["ext4_helper"]


def test_search_sort_columns_are_unambiguous(conn):
    for sort in ("name", "path", "kind", "line", "size", "lines"):
        assert query.search(conn, "ext4", limit=2, sort=sort,
                            with_subsystem=False)
