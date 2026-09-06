"""Ownership, ancestry, documentation, and subsystem queries."""

import sqlite3

from kernel_atlas import db, query


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
