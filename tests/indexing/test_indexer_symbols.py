"""Persisted symbols, aggregate details, and file/directory ownership evidence."""

from __future__ import annotations

from kernel_atlas import db, indexer

from .helpers import _tree


def test_build_persists_detailed_aggregate_rows_aliases_and_counts(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "types.h").write_text("""\
/**
 * struct study_type - indexed aggregate
 * @value: Primary value.
 * @nested: Nested view.
 * @nested.inner: Nested value.
 */
typedef struct study_type {
    int value;
    struct {
        long inner;
    } nested;
} study_type;
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    meta = db.validate_schema(conn, deep=True)
    symbol = conn.execute(
        "SELECT id,summary,parse_complete FROM symbols"
        " WHERE kind='struct' AND name='study_type'"
    ).fetchone()
    aliases = conn.execute(
        "SELECT name FROM type_aliases WHERE symbol_id=?", (symbol["id"],)
    ).fetchall()
    members = conn.execute(
        "SELECT name,parent_id,description_source FROM type_members"
        " WHERE symbol_id=? ORDER BY ordinal", (symbol["id"],)
    ).fetchall()
    conn.close()

    assert symbol["summary"] == "indexed aggregate"
    assert symbol["parse_complete"] == 1
    assert [row["name"] for row in aliases] == ["study_type"]
    assert [row["name"] for row in members] == ["value", "nested", "inner"]
    assert members[2]["parent_id"] is not None
    assert all(row["description_source"] == "kernel-doc" for row in members)
    assert int(meta["n_type_aliases"]) == 1
    assert int(meta["n_type_members"]) == 3


def test_build_persists_enum_and_struct_typedef_aliases(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "tagged-types.h").write_text("""\
typedef enum transport_mode {
    TRANSPORT_MODE_FAST,
} transport_mode_t;

typedef enum {
    ANONYMOUS_MODE_SAFE,
} anonymous_mode_t;

typedef struct tagged_record {
    int value;
} tagged_record_t;
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    meta = db.validate_schema(conn, deep=True)
    aliases = conn.execute(
        "SELECT s.kind,s.name,s.is_anonymous,a.name AS alias"
        " FROM type_aliases a JOIN symbols s ON s.id=a.symbol_id"
        " JOIN files f ON f.id=s.file_id WHERE f.path='tagged-types.h'"
        " ORDER BY s.start_line,a.name"
    ).fetchall()
    conn.close()

    assert [tuple(row) for row in aliases] == [
        ("enum", "transport_mode", 0, "transport_mode_t"),
        ("enum", "anonymous_mode_t", 1, "anonymous_mode_t"),
        ("struct", "tagged_record", 0, "tagged_record_t"),
    ]
    assert int(meta["n_type_aliases"]) == 3


def test_all_matching_subsystems_are_persisted(tmp_path):
    blocks = []
    for i in range(7):
        blocks.append(f"SECTION {i}\nM: A <a@example.com>\nF: owned.c\n")
    tree = _tree(tmp_path / "linux-9.9", "\n".join(blocks))
    (tree / "owned.c").write_text("int owned(void) { return 0; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    rows = conn.execute(
        "SELECT s.name, p.rank FROM files f "
        "JOIN path_subsys p ON p.ref_kind='file' AND p.ref_id=f.id "
        "JOIN subsystems s ON s.id=p.subsystem_id "
        "WHERE f.path='owned.c' ORDER BY p.rank"
    ).fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [(f"SECTION {i}", i) for i in range(7)]


def test_equal_top_ownership_evidence_is_preserved_as_co_primary(tmp_path):
    maintainers = """\
OWNER A
M: A <a@example.com>
F: owned.c

OWNER B
M: B <b@example.com>
F: owned.c
"""
    tree = _tree(tmp_path / "linux-9.9", maintainers)
    (tree / "owned.c").write_text("int owned(void) { return 0; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    rows = conn.execute(
        "SELECT s.name,p.rank,p.is_primary FROM path_subsys p"
        " JOIN subsystems s ON s.id=p.subsystem_id"
        " JOIN files f ON f.id=p.ref_id WHERE f.path='owned.c'"
        " ORDER BY p.rank").fetchall()
    counts = conn.execute(
        "SELECT name,n_primary_files FROM subsystems ORDER BY name").fetchall()
    conn.close()

    assert [tuple(row) for row in rows] == [
        ("OWNER A", 0, 1), ("OWNER B", 1, 1)]
    assert [tuple(row) for row in counts] == [("OWNER A", 1), ("OWNER B", 1)]


def test_directory_ownership_is_composed_from_files_not_globbed_names(tmp_path):
    tree = _tree(
        tmp_path / "linux-9.9",
        "FUTEX SUBSYSTEM\nM: A <a@example.com>\nF: kernel/futex/*\n",
    )
    directory = tree / "kernel" / "futex"
    directory.mkdir(parents=True)
    (directory / "core.c").write_text("int futex_wait(void) { return 0; }\n")
    (directory / "syscalls.c").write_text("int futex_wake(void) { return 0; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    direct_dir_claims = conn.execute(
        "SELECT COUNT(*) FROM path_subsys WHERE ref_kind='dir'").fetchone()[0]
    row = conn.execute(
        "SELECT s.name,d.n_claimed,d.n_primary,d.coverage"
        " FROM dirs directory"
        " JOIN dir_subsys d ON d.dir_id=directory.id"
        " JOIN subsystems s ON s.id=d.subsystem_id"
        " WHERE directory.path='kernel/futex' ORDER BY d.rank LIMIT 1"
    ).fetchone()
    conn.close()

    assert direct_dir_claims == 0
    assert tuple(row) == ("FUTEX SUBSYSTEM", 2, 2, 1.0)


def test_directory_subsystems_are_derived_from_descendant_files(tmp_path):
    conn = db.create(tmp_path / "directories.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0), (2, "kernel", 1, "kernel", 1),
         (3, "kernel/futex", 2, "futex", 2),
         (4, "mixed", 1, "mixed", 1)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name) VALUES (?,?,?,?)",
        [(1, "kernel/futex/a.c", 3, "a.c"),
         (2, "kernel/futex/b.c", 3, "b.c"),
         (3, "mixed/a.c", 4, "a.c"), (4, "mixed/b.c", 4, "b.c")],
    )
    conn.executemany(
        "INSERT INTO subsystems(id,name) VALUES (?,?)",
        [(1, "FUTEX SUBSYSTEM"), (2, "A"), (3, "B"), (4, "THE REST")],
    )
    claims = [
        ("file", 1, 1, 10, 0, 1), ("file", 1, 4, -1000, 1, 0),
        ("file", 2, 1, 10, 0, 1), ("file", 2, 4, -1000, 1, 0),
        ("file", 3, 2, 10, 0, 1), ("file", 3, 3, 5, 1, 0),
        ("file", 3, 4, -1000, 2, 0),
        ("file", 4, 3, 10, 0, 1), ("file", 4, 2, 5, 1, 0),
        ("file", 4, 4, -1000, 2, 0),
    ]
    conn.executemany(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES (?,?,?,?,?,?)", claims)

    indexer._derive_directory_composition(conn)
    futex = conn.execute(
        "SELECT s.name,d.n_claimed,d.n_primary,d.coverage FROM dir_subsys d"
        " JOIN subsystems s ON s.id=d.subsystem_id WHERE d.dir_id=3"
        " ORDER BY d.rank LIMIT 1"
    ).fetchone()
    mixed = conn.execute(
        "SELECT s.name,d.n_claimed,d.n_primary,d.coverage FROM dir_subsys d"
        " JOIN subsystems s ON s.id=d.subsystem_id WHERE d.dir_id=4"
        " ORDER BY d.rank"
    ).fetchall()
    root_total = conn.execute(
        "SELECT n_files_recursive FROM dirs WHERE id=1").fetchone()[0]
    root_composition = conn.execute(
        "SELECT COUNT(*) FROM dir_subsys WHERE dir_id=1").fetchone()[0]
    conn.close()

    assert tuple(futex) == ("FUTEX SUBSYSTEM", 2, 2, 1.0)
    assert [tuple(row) for row in mixed[:2]] == [
        ("A", 2, 1, 0.5), ("B", 2, 1, 0.5)]
    assert root_total == 4
    assert root_composition == 4
