from kernel_atlas import db, query
from kernel_atlas.cli import _frames_from_text


def names(entries):
    return sorted(e.name for e in entries)


def sib(conn, target, level="auto", kinds=None, include_self=False, **kw):
    t = query.resolve(conn, target).target
    assert t is not None, target
    scope = query.build_scope(conn, t, level)
    ks = kinds or query.default_kinds(t)
    entries = query.collect(conn, scope, ks, **kw)
    if not include_self:
        entries = [e for e in entries
                   if not (e.path == t.path and e.name == t.name)]
    return entries


# ---------------------------------------------------------------- indexing

def test_index_metadata(mini_index):
    conn = db.connect(mini_index)
    meta = db.get_meta(conn)
    assert meta["kernel_version"] == "6.12.104"
    assert int(meta["n_files"]) >= 19
    assert int(meta["n_symbols"]) > 20
    conn.close()


def test_every_file_is_indexed_not_just_c(conn):
    paths = {r["path"] for r in conn.execute("SELECT path FROM files")}
    assert "Makefile" in paths
    assert "fs/ext4/Makefile" in paths
    assert "Documentation/filesystems/ext4/about.rst" in paths


def test_line_counts_recorded(conn):
    row = conn.execute("SELECT lines FROM files WHERE path='fs/ext4/inode.c'").fetchone()
    assert row["lines"] > 10


def test_directory_rollups(conn):
    row = conn.execute("SELECT * FROM dirs WHERE path='fs/ext4'").fetchone()
    assert row["n_files"] == 3
    row = conn.execute("SELECT * FROM dirs WHERE path='fs'").fetchone()
    assert row["n_subdirs"] == 2


# ---------------------------------------------------------------- resolving

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


def test_resolve_bare_symbol(conn):
    t = query.resolve(conn, "ext4_get_block").target
    assert t.kind == "symbol" and t.path == "fs/ext4/inode.c"


def test_resolve_by_line_number(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:3").target
    assert t.kind == "symbol" and t.name == "ext4_inode_blocks_set"


def test_resolve_ambiguous_reports_candidates(conn):
    res = query.resolve(conn, "super.c")
    assert res.target is not None
    assert res.candidates, "expected fs/ext4/super.c and fs/btrfs/super.c"
    assert "2 files" in res.note


def test_resolve_syscall_by_name(conn):
    t = query.resolve(conn, "sys_open").target
    assert t.symbol_kind == "syscall" and t.path == "fs/open.c"


def test_resolve_missing(conn):
    res = query.resolve(conn, "definitely_not_here_xyz")
    assert res.target is None and "nothing in the index" in res.note


# ---------------------------------------------------------------- siblings

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


def test_limit_counts_siblings_not_the_target(mini_index, capsys):
    """`-n 3` must return three *other* functions, not two plus the target.

    ext4_bmap sorts first among the four functions in inode.c, so applying the
    limit before dropping the target would silently return one row too few.
    """
    from kernel_atlas import cli

    cli.main(["--db", str(mini_index), "siblings", "fs/ext4/inode.c:ext4_bmap",
              "-f", "names", "-n", "3"])
    got = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert got == ["ext4_get_block", "ext4_helper", "ext4_inode_blocks_set"]


# ---------------------------------------------------------------- subsystems

def test_file_gets_its_precise_subsystem(conn):
    t = query.resolve(conn, "fs/ext4/inode.c").target
    assert query.subsystem_for_target(conn, t)["name"] == "EXT4 FILE SYSTEM"


def test_symbol_inherits_the_subsystem_of_its_file(conn):
    t = query.resolve(conn, "tcp_sendmsg").target
    assert query.subsystem_for_target(conn, t)["name"] == "NETWORKING [IPv4/IPv6]"


def test_vfs_file_is_not_claimed_by_ext4(conn):
    t = query.resolve(conn, "fs/namei.c").target
    assert query.subsystem_for_target(conn, t)["name"].startswith("FILESYSTEMS")


def test_subsystem_lookup_and_file_counts(conn):
    rows = query.subsystem_by_name(conn, "EXT4 FILE SYSTEM")
    assert rows[0]["n_files"] == 4  # 3 in fs/ext4 + the Documentation page


def test_ancestry_walks_the_path(conn):
    anc = dict(query.ancestry(conn, "fs/ext4/inode.c"))
    assert anc["fs/ext4"] == "EXT4 FILE SYSTEM"
    assert "fs" in anc


# ---------------------------------------------------------------- extras

def test_search_substring_and_exact(conn):
    assert "ext4_bmap" in names(query.search(conn, "bmap"))
    assert names(query.search(conn, "bmap", mode="exact")) == []
    assert names(query.search(conn, "ext4_bmap", mode="exact")) == ["ext4_bmap"]


def test_search_glob_and_kinds(conn):
    got = names(query.search(conn, "ext4_*", mode="glob", kinds=("function",)))
    assert "ext4_bmap" in got and "ext4_sb_info" not in got


def test_call_graph(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:ext4_bmap").target
    assert "ext4_get_block" in query.callees(conn, t.id)
    assert "ext4_bmap" in names(query.callers(conn, "ext4_get_block"))


def test_backtrace_frame_extraction():
    oops = """
    BUG: kernel NULL pointer dereference at 0000000000000000
    Call Trace:
     <TASK>
     ext4_bmap+0x12/0x40
     ? __alloc_pages+0x3f0/0x6c0
     tcp_sendmsg+0x59/0x110
     </TASK>
    """
    assert _frames_from_text(oops) == ["ext4_bmap", "__alloc_pages", "tcp_sendmsg"]


def test_backtrace_gdb_style():
    assert _frames_from_text("#3  0xffffffff81 in ext4_bmap (mapping=0x0)") == \
        ["ext4_bmap"]
