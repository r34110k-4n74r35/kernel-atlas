import sqlite3

import pytest

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


def test_resolve_structure_is_kind_scoped_and_accepts_typedef_alias(conn):
    tagged = query.resolve_structure(
        conn, "fs/ext4/super.c:ext4_sb_info").target
    assert tagged is not None
    assert tagged.symbol_kind == "struct"
    assert tagged.path == "fs/ext4/super.c"

    anonymous = query.resolve_structure(conn, "struct study_mask_t").target
    assert anonymous is not None
    assert anonymous.path == "include/linux/fs.h"
    assert anonymous.is_anonymous

    union = query.resolve_structure(conn, "union study_value").target
    assert union is not None and union.symbol_kind == "union"
    assert query.resolve_structure(conn, "struct study_value").target is None


def test_structure_detail_is_nested_documented_and_source_ordered(conn):
    target = query.resolve_structure(
        conn, "fs/ext4/super.c:ext4_sb_info").target
    detail = query.structure_detail(conn, target)
    assert detail["summary"] == "in-memory ext4 superblock study fixture"
    assert detail["kind"] == "struct"
    assert detail["tag"] == "ext4_sb_info"
    assert detail["c_name"] == "struct ext4_sb_info"
    assert detail["selector"] == "fs/ext4/super.c:ext4_sb_info"
    assert detail["signature"].startswith("struct ext4_sb_info")
    assert detail["direct_member_count"] == 9
    assert detail["total_member_count"] == 13
    assert detail["documentable_member_count"] == 11
    assert detail["described_member_count"] == 11
    assert detail["documented_member_count"] == 11
    assert detail["semantic_description_count"] == 0
    assert detail["documentation_coverage"] == 1.0
    assert detail["members"][0]["name"] == "s_blocks_count"
    assert detail["members"][2]["array_dimensions"] == ["16"]
    assert detail["members"][3]["bit_width"] == "2"
    assert detail["members"][6]["kind"] == "union"
    assert detail["members"][6]["children"][1]["kind"] == "struct"
    assert detail["members"][7]["generated_by"] == "DECLARE_BITMAP"
    assert detail["members"][7]["conditions"] == [
        "#ifdef CONFIG_EXT4_STUDY_FEATURES",
    ]
    assert detail["members"][8]["is_flexible_array"]
    assert detail["members"][8]["visibility"] == "private"


def test_structure_coverage_separates_parser_semantics_from_source_docs(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "semantic-description.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    writer.execute(
        "UPDATE type_members SET description_source='macro-semantics'"
        " WHERE generated_by='DECLARE_BITMAP'"
    )
    writer.execute(
        "UPDATE type_members SET description='Generated container.',"
        " description_source='macro-semantics'"
        " WHERE id=(SELECT id FROM type_members WHERE is_anonymous=1 LIMIT 1)"
    )
    target = query.resolve_structure(writer, "ext4_sb_info").target
    detail = query.structure_detail(writer, target)
    writer.close()
    assert detail["described_member_count"] == 11
    assert detail["documented_member_count"] == 10
    assert detail["semantic_description_count"] == 2
    assert detail["documentation_coverage"] == pytest.approx(10 / 11)


def test_structure_selectors_survive_tag_and_typedef_name_collisions(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "aggregate-namespace-collision.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    tagged = writer.execute(
        "SELECT s.id,s.file_id FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE f.path='fs/ext4/super.c' AND s.name='ext4_sb_info'"
    ).fetchone()
    writer.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'alias_spelling')",
        (tagged["id"],),
    )
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'alias_spelling','struct',1,1,"
        " 'struct alias_spelling { 0 members }')",
        (tagged["file_id"],),
    )
    resolution = query.resolve_structure(writer, "alias_spelling")
    candidates = [resolution.target, *resolution.candidates]
    selectors = {target.name: query.structure_selector(writer, target)
                 for target in candidates}
    writer.close()
    assert selectors == {
        "ext4_sb_info": "fs/ext4/super.c:ext4_sb_info",
        "alias_spelling": "fs/ext4/super.c:1",
    }


def test_structure_resolution_does_not_promise_an_unexpressible_selector(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "unselectable-aggregate.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    row = writer.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature FROM symbols"
        " WHERE name='ext4_sb_info' AND kind='struct'"
    ).fetchone()
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,?,?,?,?,?)", tuple(row),
    )
    resolution = query.resolve_structure(writer, "ext4_sb_info")
    candidates = [resolution.target, *resolution.candidates]
    selectors = [query.structure_selector(writer, target)
                 for target in candidates]
    writer.close()
    assert selectors == [None, None]
    assert "where an exact command selector is expressible" in resolution.note


def test_union_detail_uses_the_actual_c_kind(conn):
    target = query.resolve_structure(conn, "study_value").target
    detail = query.structure_detail(conn, target)
    assert detail["kind"] == "union"
    assert detail["tag"] == "study_value"
    assert detail["c_name"] == "union study_value"
    assert detail["direct_member_count"] == 2
    assert detail["documentation_coverage"] == 1.0


def test_structure_resolution_ignores_same_named_function(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "same-name-function.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/ext4/super.c'").fetchone()[0]
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line)"
        " VALUES (?,'ext4_sb_info','function',1,1)", (file_id,))
    writer.commit()
    resolved = query.resolve_structure(writer, "ext4_sb_info")
    writer.close()
    assert resolved.target.symbol_kind == "struct"
    assert not resolved.candidates


def test_structure_resolution_ignores_enum_typedef_aliases(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "enum-alias.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    enum_id = writer.execute(
        "SELECT id FROM symbols WHERE kind='enum' AND name='rw_hint'"
    ).fetchone()[0]
    writer.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'rw_hint_t')",
        (enum_id,),
    )
    count = writer.execute("SELECT COUNT(*) FROM type_aliases").fetchone()[0]
    writer.execute(
        "UPDATE meta SET value=? WHERE key='n_type_aliases'", (str(count),)
    )
    writer.commit()
    db.validate_schema(writer, deep=True)

    resolved = query.resolve_structure(writer, "rw_hint_t")
    writer.close()

    assert resolved.target is None
    assert "no struct/union tag or typedef alias" in resolved.note


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


def test_syscall_definitions_have_call_edges(conn):
    """Regression: the DEFINEn body is a sibling node and used to be skipped."""
    t = query.resolve(conn, "sys_open").target
    assert "do_sys_open" in query.callees(conn, t.id)


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


def test_mixed_directory_has_no_invented_single_owner(conn):
    directory = query.resolve(conn, "fs").target
    assert query.subsystem_for_target(conn, directory) is None
    assert query.directory_subsystem_label(conn, directory.id, directory.path) == \
        "Filesystems (mixed; includes unclassified)"


def test_directory_label_detects_files_with_no_maintainers_match(tmp_path):
    conn = db.create(tmp_path / "unclaimed.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (?,?,?,?,?,?,?)", [
            (1, "", None, "linux", 0, 0, 2),
            (2, "drivers", 1, "drivers", 1, 2, 2),
        ])
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,index_status) VALUES (?,?,?,?,?)",
        [(1, "drivers/owned.c", 2, "owned.c", "parsed"),
         (2, "drivers/unowned.c", 2, "unowned.c", "parsed")])
    conn.execute(
        "INSERT INTO subsystems(id,name,n_files,n_primary_files)"
        " VALUES (0,'OWNER',1,1)")
    conn.execute(
        "INSERT INTO path_subsys"
        " (ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES ('file',1,0,10,0,1)")
    conn.execute(
        "INSERT INTO dir_subsys"
        " (dir_id,subsystem_id,n_claimed,n_primary,coverage,rank)"
        " VALUES (2,0,1,1,0.5,0)")

    assert query.directory_subsystem_label(conn, 2, "drivers") == \
        "Device drivers (mixed; includes unclassified)"
    conn.close()


def test_resolve_missing(conn):
    res = query.resolve(conn, "definitely_not_here_xyz")
    assert res.target is None and "nothing in the index" in res.note


# ---------------------------------------------------------------- extras

def test_search_substring_and_exact(conn):
    assert "ext4_bmap" in names(query.search(conn, "bmap"))
    assert names(query.search(conn, "bmap", mode="exact")) == []
    assert names(query.search(conn, "ext4_bmap", mode="exact")) == ["ext4_bmap"]
    assert "ext4_bmap" in names(query.search(conn, "EXT4_BMAP", mode="substring"))
    assert names(query.search(conn, "EXT4_BMAP", mode="exact")) == []


def test_search_glob_and_kinds(conn):
    got = names(query.search(conn, "ext4_*", mode="glob", kinds=("function",)))
    assert "ext4_bmap" in got and "ext4_sb_info" not in got


def test_call_graph(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:ext4_bmap").target
    assert "ext4_get_block" in query.callees(conn, t.id)
    assert "ext4_bmap" in names(query.callers(conn, "ext4_get_block"))

    callee = query.resolve(conn, "fs/ext4/inode.c:ext4_get_block").target
    inbound = query.callers(conn, callee.id)
    assert inbound[0].resolution == "same_file"


def test_call_queries_preserve_mixed_occurrence_evidence(mini_index, tmp_path):
    import shutil

    copied = tmp_path / "mixed-call-evidence.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    writer.execute(
        "UPDATE calls SET direct_count=2,indirect_count=1,macro_count=1"
        " WHERE callee='ext4_get_block'"
    )
    writer.execute(
        "UPDATE meta SET value=(SELECT CAST(SUM(direct_count+indirect_count+"
        " macro_count) AS TEXT) FROM calls) WHERE key='n_call_occurrences'"
    )
    writer.commit()

    caller = query.resolve(writer, "ext4_bmap").target
    outgoing = query.callee_entries(writer, caller.id)
    edge = next(entry for entry in outgoing if entry.name == "ext4_get_block")
    assert (edge.direct_count, edge.indirect_count, edge.macro_count) == (2, 1, 1)

    callee = query.resolve(writer, "ext4_get_block").target
    incoming = query.callers(writer, callee.id)
    edge = next(entry for entry in incoming if entry.name == "ext4_bmap")
    assert (edge.direct_count, edge.indirect_count, edge.macro_count) == (2, 1, 1)
    writer.close()


def test_callers_string_api_rejects_ambiguous_callable_identity(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "ambiguous-caller.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_get_block'"
    ).fetchone()
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], row[3] + 100, row[4] + 100, *row[5:]))
    conn.commit()

    with pytest.raises(ValueError, match="pass a concrete symbol id"):
        query.callers(conn, "ext4_get_block")
    conn.close()


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


def test_info_hides_the_rest_when_a_real_subsystem_matched(conn):
    t = query.resolve(conn, "mm/page_alloc.c").target
    all_names = [r["name"] for r in query.all_subsystems(conn, "file", t.id)]
    assert "THE REST" in all_names
    shown = [r["name"] for r in query.visible_subsystems(
        query.all_subsystems(conn, "file", t.id))]
    assert "THE REST" not in shown
    assert shown[0] == "MEMORY MANAGEMENT"


def test_documentation_claimed_by_subsystem_and_by_path(conn):
    ext4 = query.resolve(conn, "fs/ext4").target
    paths = [e.path for e in query.documentation_for(conn, ext4)]
    assert "Documentation/filesystems/ext4/about.rst" in paths

    mm = query.resolve(conn, "mm").target
    paths = [e.path for e in query.documentation_for(conn, mm)]
    assert paths[0] == "Documentation/mm/page_alloc.rst"

    futex = query.resolve(conn, "kernel/futex").target
    subsystem = query.subsystem_for_target(conn, futex)
    assert subsystem["name"] == "FUTEX SUBSYSTEM"
    paths = [e.path for e in query.documentation_for(conn, futex)]
    assert "Documentation/locking/futex.rst" in paths


def test_documentation_does_not_fall_back_to_generic_top_level_tokens(conn):
    driver = query.resolve(
        conn, "drivers/net/ethernet/intel/igb/igb_main.c").target
    assert query.documentation_for(conn, driver) == []


def test_documentation_fallback_includes_a_top_level_named_document(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "top-level-doc.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    dir_id = writer.execute(
        "SELECT id FROM dirs WHERE path='Documentation'").fetchone()[0]
    writer.execute(
        "INSERT INTO files(path,dir_id,name,ext,size,lines,n_symbols,"
        "index_status) VALUES (?,?,?,?,?,?,?,?)",
        ("Documentation/mm.rst", dir_id, "mm.rst", ".rst", 10, 1, 0,
         "indexed"),
    )
    writer.commit()
    writer.close()

    reader = db.connect(copied, readonly=True)
    target = query.resolve(reader, "mm").target
    assert target is not None
    paths = {e.path for e in query.documentation_for(reader, target, limit=30)}
    reader.close()
    assert "Documentation/mm.rst" in paths


def test_annotate_hides_the_rest_in_favour_of_the_area(conn):
    t = query.resolve(conn, "Makefile").target
    e = query.Entry(kind="file", name=t.name, path=t.path)
    query.annotate_subsystems(conn, [e])
    assert e.subsystem != "THE REST"


def test_like_under_escapes_sql_wildcards():
    assert query.like_under("") == "%"
    assert query.like_under("mm") == "mm/%"
    assert query.like_under("io_uring") == r"io\_uring/%"
    assert query.like_escape("100%") == r"100\%"


def test_ancestry_never_labels_the_rest(conn):
    for path in ("mm/page_alloc.c", "net/ipv4/tcp.c", "Makefile"):
        labels = [s for _, s in query.ancestry(conn, path)]
        assert "THE REST" not in labels


def test_licenses_is_a_named_area():
    from kernel_atlas.maintainers import top_level_area
    assert top_level_area("LICENSES/preferred/GPL-2.0")[0] == "Licenses"


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


def test_search_applies_static_and_regex_filters_before_limit(conn):
    got = query.search(conn, "ext4", limit=1, grep="helper$", static="only",
                       with_subsystem=False)
    assert [e.name for e in got] == ["ext4_helper"]


def test_search_sort_columns_are_unambiguous(conn):
    for sort in ("name", "path", "kind", "line", "size", "lines"):
        assert query.search(conn, "ext4", limit=2, sort=sort,
                            with_subsystem=False)


def test_backtrace_preserves_repeated_short_and_optimized_frames():
    text = """
    x+0x1/0x2
    work.isra.0+0x10/0x20
    x+0x3/0x4
    helper.constprop.17
    """
    assert _frames_from_text(text) == ["x", "work", "x", "helper"]


def test_backtrace_accepts_mixed_case_gdb_and_bare_frames():
    text = "#3  0xffffffff81 in Proc_2 (x=0)\nBDEV_I\n"
    assert _frames_from_text(text) == ["Proc_2", "BDEV_I"]
