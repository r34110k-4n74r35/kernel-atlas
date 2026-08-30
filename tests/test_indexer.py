from __future__ import annotations

import time
from pathlib import Path

import pytest

from kernel_atlas import cparse, db, indexer, query


def _tree(root: Path, maintainers: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Makefile").write_text(
        "VERSION = 9\nPATCHLEVEL = 9\nSUBLEVEL = 0\nEXTRAVERSION =\n"
    )
    (root / "MAINTAINERS").write_text(
        maintainers or "TEST\nM: A <a@example.com>\nF: *\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    ("kinds", "message"),
    [
        ("function", "iterable"),
        (b"function", "iterable"),
        (None, "iterable"),
        (17, "iterable"),
        ((), "at least one"),
        (("function", "function"), "duplicates"),
        (("function", 17), "every kind must be a string"),
        (("not-a-kind",), "unknown symbol kind"),
    ],
)
def test_build_rejects_invalid_kinds_before_touching_paths(
        tmp_path, kinds, message):
    with pytest.raises(ValueError, match=message):
        indexer.build(
            tmp_path / "missing-tree", tmp_path / "index.db", "9.9",
            kinds=kinds, jobs=1, quiet=True,
        )
    assert not (tmp_path / "index.db").exists()


@pytest.mark.parametrize(
    ("jobs", "message"),
    [
        (True, "integer"),
        (False, "integer"),
        (1.0, "integer"),
        ("1", "integer"),
        (0, "between 1 and 256"),
        (-1, "between 1 and 256"),
        (257, "between 1 and 256"),
    ],
)
def test_build_rejects_invalid_jobs_before_call_requirements(
        tmp_path, jobs, message):
    with pytest.raises(ValueError, match=message):
        indexer.build(
            tmp_path / "missing-tree", tmp_path / "index.db", "9.9",
            kinds=("function",), want_calls=True, jobs=jobs, quiet=True,
        )
    assert not (tmp_path / "index.db").exists()


def test_build_rejects_directory_output_before_scanning(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "index.db"
    out.mkdir()

    monkeypatch.setattr(
        indexer, "_scan_tree",
        lambda *args, **kwargs: pytest.fail("source scan must not start"),
    )
    with pytest.raises(ValueError, match="index output is a directory"):
        indexer.build(tree, out, "9.9", jobs=1, quiet=True)

    assert out.is_dir()


def test_oversize_header_is_counted_and_explicitly_skipped(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    line = b"#define GENERATED_VALUE 1\n"
    data = line * (indexer.MAX_READ // len(line) + 10)
    (tree / "generated.h").write_bytes(data)
    out = tmp_path / "index.db"

    stats = indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT lines, n_symbols, index_status, index_error FROM files "
        "WHERE path='generated.h'"
    ).fetchone()
    meta = db.validate_schema(conn)
    conn.close()

    assert tuple(row) == (data.count(b"\n"), 0, "skipped_oversize", None)
    assert stats.parsed == 0
    assert stats.skipped == stats.oversize == 1
    assert meta["n_parse_skipped"] == "1"
    assert meta["n_oversize"] == "1"


def test_direct_build_records_the_supplied_tree_as_local_source(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    meta = db.validate_schema(conn)
    conn.close()

    assert meta["source"] == str(tree.resolve())


def test_pre_publish_failure_preserves_the_previous_index(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "index.db"
    previous = b"previous index"
    out.write_bytes(previous)

    def reject_publication() -> None:
        raise RuntimeError("source changed")

    with pytest.raises(RuntimeError, match="source changed"):
        indexer.build(
            tree, out, "9.9", jobs=1, quiet=True,
            pre_publish=reject_publication)

    assert out.read_bytes() == previous
    assert list(tmp_path.glob(".index.db.*.building")) == []


def test_build_uses_the_parser_size_contract_and_allows_a_custom_limit(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    source = b"#define VALUE 1\n" * 20
    (tree / "limited.h").write_bytes(source)
    out = tmp_path / "index.db"

    assert indexer.MAX_READ == cparse.MAX_FILE_BYTES
    stats = indexer.build(
        tree, out, "9.9", jobs=1, quiet=True, max_file_bytes=128)
    conn = db.connect(out)
    status = conn.execute(
        "SELECT index_status FROM files WHERE path='limited.h'"
    ).fetchone()[0]
    conn.close()

    assert status == "skipped_oversize"
    assert stats.oversize == 1
    assert cparse.parse_source(
        source, cparse.DEFAULT_KINDS, max_file_bytes=128) == []


def test_shipped_c_and_header_sources_are_parsed(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "generated.c_shipped").write_text(
        "int shipped_function(void) { return 1; }\n")
    (tree / "generated.h_shipped").write_text(
        "struct shipped_type { int value; };\n")
    out = tmp_path / "index.db"

    stats = indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    rows = list(conn.execute(
        "SELECT f.path,s.name,s.kind FROM symbols s"
        " JOIN files f ON f.id=s.file_id"
        " WHERE f.path LIKE '%_shipped' ORDER BY f.path,s.kind,s.name"))
    statuses = dict(conn.execute(
        "SELECT path,index_status FROM files WHERE path LIKE '%_shipped'"))
    conn.close()

    assert stats.parsed == 2
    assert statuses == {
        "generated.c_shipped": "parsed",
        "generated.h_shipped": "parsed",
    }
    assert ("generated.c_shipped", "shipped_function", "function") in {
        tuple(row) for row in rows
    }
    assert ("generated.h_shipped", "shipped_type", "struct") in {
        tuple(row) for row in rows
    }


def test_symlink_is_represented_without_following_it(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    target = tree / "real.h"
    target.write_text("#define REAL 1\n")
    link = tree / "alias.h"
    try:
        link.symlink_to("real.h")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    out = tmp_path / "index.db"

    stats = indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT is_symlink, link_target, index_status, n_symbols FROM files "
        "WHERE path='alias.h'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (1, "real.h", "symlink", 0)
    assert stats.symlinks == 1
    assert stats.skipped == 1
    assert stats.parsed == 1


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


def test_worker_records_parse_and_read_errors(monkeypatch, tmp_path):
    source = tmp_path / "bad.c"
    source.write_text("int bad(void) { return 0; }\n")
    monkeypatch.setattr(indexer, "_W_ROOT", str(tmp_path))
    monkeypatch.setattr(indexer, "_W_KINDS", frozenset(cparse.DEFAULT_KINDS))

    def broken_parser(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(cparse, "parse_source", broken_parser)
    parsed, missing = indexer._work([(1, "bad.c", True), (2, "gone.c", True)])
    assert parsed[3] == "parse_error"
    assert "parser exploded" in parsed[4]
    assert missing[3] == "read_error"
    assert "FileNotFoundError" in missing[4]


def test_build_uses_a_unique_scratch_and_cleans_it_on_failure(
        monkeypatch, tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "same.db"
    seen = []

    def fail_create(path):
        seen.append(Path(path))
        raise RuntimeError("stop")

    monkeypatch.setattr(db, "create", fail_create)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop"):
            indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    assert seen[0] != seen[1]
    assert all(not path.exists() for path in seen)


def test_library_build_rejects_output_entry_inside_source_tree(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    outside = tmp_path / "outside.db"
    output = tree / "index.db"
    try:
        output.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="inside the source tree"):
        indexer.build(tree, output, "9.9", jobs=1, quiet=True)
    assert output.is_symlink()
    assert not outside.exists()


def test_build_time_includes_database_finalization(monkeypatch, tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "one.c").write_text("int one(void) { return 1; }\n")
    out = tmp_path / "index.db"
    original = db.finalize

    def slow_finalize(conn):
        time.sleep(0.05)
        original(conn)

    monkeypatch.setattr(db, "finalize", slow_finalize)
    stats = indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    recorded = float(db.get_meta(conn)["build_seconds"])
    conn.close()
    assert stats.seconds >= 0.05
    # Metadata is rounded to one decimal place, but must include the sleep.
    assert recorded >= 0.1


def test_build_time_includes_publication_validation(monkeypatch, tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "one.c").write_text("int one(void) { return 1; }\n")
    out = tmp_path / "index.db"
    original = db.validate_schema

    def slow_validation(conn, *, deep=False):
        result = original(conn, deep=deep)
        if deep:
            time.sleep(0.05)
        return result

    monkeypatch.setattr(db, "validate_schema", slow_validation)
    stats = indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    conn = db.connect(out)
    recorded = float(db.get_meta(conn)["build_seconds"])
    conn.close()

    assert stats.seconds >= 0.05
    assert recorded >= 0.1


def test_build_time_includes_pre_publish_validation(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "one.c").write_text("int one(void) { return 1; }\n")
    out = tmp_path / "index.db"

    def slow_pre_publish():
        time.sleep(0.05)

    stats = indexer.build(
        tree, out, "9.9", jobs=1, quiet=True,
        pre_publish=slow_pre_publish,
    )
    conn = db.connect(out)
    recorded = float(db.get_meta(conn)["build_seconds"])
    conn.close()

    assert stats.seconds >= 0.05
    assert recorded >= 0.1


def test_call_resolution_is_identity_aware_and_conservative(tmp_path):
    conn = db.create(tmp_path / "calls.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0), (2, "one", 1, "one", 1),
         (3, "two", 1, "two", 1), (4, "three", 1, "three", 1)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext) VALUES (?,?,?,?,?)",
        [(1, "one/a.c", 2, "a.c", ".c"),
         (2, "two/b.c", 3, "b.c", ".c"),
         (3, "three/c.c", 4, "c.c", ".c"),
         (4, "blockers.h", 1, "blockers.h", ".h")],
    )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,is_static) VALUES (?,?,?,?,?)",
        [
            (1, 1, "caller", "function", 0),
            (2, 1, "helper", "function", 1),
            (3, 2, "helper", "function", 1),
            (4, 2, "unique", "function", 0),
            (5, 2, "duplicate", "function", 0),
            (6, 3, "duplicate", "function", 0),
            (7, 1, "local_duplicate", "function", 1),
            (8, 1, "local_duplicate", "function", 1),
            (9, 4, "macro_only", "macro", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(1, name) for name in (
            "helper", "unique", "duplicate", "local_duplicate",
            "macro_only", "missing")],
    )
    db.finalize(conn)

    counts = indexer._resolve_calls(conn)
    rows = {
        row["callee"]: (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT callee,callee_id,resolution FROM calls ORDER BY callee")
    }
    outbound = query.callee_entries(conn, 1)
    local_callers = query.callers(conn, 2)
    unrelated_static_callers = query.callers(conn, 3)
    conn.close()

    assert rows == {
        "helper": (2, "same_file"),
        "unique": (4, "unique_global"),
        "duplicate": (None, "ambiguous"),
        "local_duplicate": (None, "ambiguous"),
        "macro_only": (None, "macro"),
        "missing": (None, "unresolved"),
    }
    assert counts == {
        "same_file": 1, "included_source": 0, "unique_global": 1,
        "ambiguous": 2, "macro": 1, "indirect": 0, "unresolved": 1,
    }
    assert next(row for row in outbound if row.name == "helper").ref_id == 2
    assert [row.name for row in local_callers] == ["caller"]
    assert unrelated_static_callers == []


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


def test_call_resolution_respects_domains_and_identity_blockers(tmp_path):
    conn = db.create(tmp_path / "call-domains.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext) VALUES (?,?,?,?,?)",
        [
            (1, "block/a.c", 1, "a.c", ".c"),
            (2, "tools/testing/helper.c", 1, "helper.c", ".c"),
            (3, "include/linux/api.h", 1, "api.h", ".h"),
            (4, "arch/arm/kernel/a.c", 1, "a.c", ".c"),
            (5, "arch/alpha/kernel/b.c", 1, "b.c", ".c"),
            (6, "kernel/helper.c", 1, "helper.c", ".c"),
            (7, "tools/accounting/main.c", 1, "main.c", ".c"),
            (8, "tools/testing/selftests/arm64/signal.c", 1, "signal.c", ".c"),
            (9, "tools/accounting/helper.c", 1, "helper.c", ".c"),
            (10, "tools/testing/selftests/kvm/main.c", 1, "main.c", ".c"),
            (11, "drivers/misc/blockers.c", 1, "blockers.c", ".c"),
            (12, "arch/x86/lib/checksum.c", 1, "checksum.c", ".c"),
            (13, "arch/x86/tools/relocs.c", 1, "relocs.c", ".c"),
        ],
    )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,is_static) VALUES (?,?,?,?,?)",
        [
            (1, 1, "kernel_caller", "function", 0),
            (2, 4, "arm_caller", "function", 0),
            (3, 2, "spin_lock", "function", 0),
            (4, 3, "spin_lock", "macro", 0),
            (5, 2, "tool_only", "function", 0),
            (6, 5, "alpha_only", "function", 0),
            (7, 6, "generic_ok", "function", 0),
            (8, 3, "inline_only", "function", 1),
            (9, 3, "callback", "variable", 1),
            (10, 6, "conflicted", "function", 0),
            (11, 3, "conflicted", "macro", 0),
            (12, 7, "accounting_caller", "function", 0),
            (13, 8, "sigaddset", "function", 0),
            (14, 9, "accounting_ok", "function", 0),
            (15, 10, "kvm_caller", "function", 0),
            (16, 7, "accounting_local", "function", 0),
            (17, 6, "device_add", "function", 0),
            (18, 6, "device_remove", "function", 0),
            (19, 11, "device_add", "macro", 0),
            (20, 11, "device_remove", "variable", 1),
            (21, 6, "csum_partial", "function", 0),
            (22, 12, "csum_partial", "function", 0),
            (23, 13, "relocs_caller", "function", 0),
            (24, 6, "fprintf", "function", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(1, name) for name in (
            "spin_lock", "tool_only", "inline_only", "callback", "conflicted",
            "device_add", "device_remove", "csum_partial")]
        + [(2, "alpha_only"), (2, "generic_ok")]
        + [(12, "sigaddset"), (12, "accounting_ok"),
           (12, "accounting_local"), (15, "sigaddset"), (23, "fprintf")],
    )
    db.finalize(conn)

    indexer._resolve_calls(conn)
    rows = {
        (row["caller_id"], row["callee"]):
            (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT caller_id,callee,callee_id,resolution FROM calls")
    }
    conn.close()

    assert rows[(1, "spin_lock")] == (None, "macro")
    assert rows[(1, "tool_only")] == (None, "unresolved")
    assert rows[(1, "inline_only")] == (None, "ambiguous")
    assert rows[(1, "callback")] == (None, "ambiguous")
    assert rows[(1, "conflicted")] == (None, "ambiguous")
    assert rows[(1, "device_add")] == (17, "unique_global")
    assert rows[(1, "device_remove")] == (18, "unique_global")
    assert rows[(1, "csum_partial")] == (None, "ambiguous")
    assert rows[(2, "alpha_only")] == (None, "unresolved")
    assert rows[(2, "generic_ok")] == (7, "unique_global")
    assert rows[(12, "sigaddset")] == (None, "unresolved")
    assert rows[(12, "accounting_ok")] == (None, "unresolved")
    assert rows[(12, "accounting_local")] == (16, "same_file")
    assert rows[(15, "sigaddset")] == (None, "unresolved")
    assert rows[(23, "fprintf")] == (None, "unresolved")


def test_call_build_requires_identity_blocker_kinds(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "index.db"

    with pytest.raises(ValueError, match="requires indexing: macro, variable"):
        indexer.build(
            tree, out, "9.9", kinds=("function",), want_calls=True,
            jobs=1, quiet=True,
        )


def test_call_build_retains_local_function_pointer_calls_as_indirect(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "callbacks.c").write_text("""
int callback(void) { return 1; }
int caller(int (*callback)(void)) { return callback(); }
""")
    out = tmp_path / "index.db"

    stats = indexer.build(
        tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee,c.callee_id,c.resolution FROM calls c "
        "JOIN symbols s ON s.id=c.caller_id WHERE s.name='caller'"
    ).fetchone()
    meta = db.validate_schema(conn)
    conn.close()

    assert tuple(row) == ("callback", None, "indirect")
    assert stats.calls_indirect == 1
    assert meta["n_calls_indirect"] == "1"


def test_file_scope_function_pointer_blocks_same_named_global(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "fp.c").write_text("""\
static int (*callback)(void);
int fp_caller(void) { return callback(); }
""")
    (tree / "other.c").write_text(
        "int callback(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='fp_caller' AND c.callee='callback'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "indirect")


def test_same_file_macro_blocks_same_named_global(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "macro.c").write_text("""\
#define callback() 1
int macro_caller(void) { return callback(); }
""")
    (tree / "other.c").write_text(
        "int callback(void) { return 7; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='macro_caller' AND c.callee='callback'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "macro")


def test_call_resolution_respects_same_file_macro_source_intervals(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "nfs.c").write_text("""\
static int rpc_call_sync(void) { return 1; }
static int before_define(void) { return rpc_call_sync(); }
#define rpc_call_sync() 2
static int while_defined(void) { return rpc_call_sync(); }
#undef rpc_call_sync
static int after_undef(void) { return rpc_call_sync(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["caller"]: (
            row["resolution"], row["direct_count"], row["macro_count"])
        for row in conn.execute(
            "SELECT caller.name AS caller,c.resolution,c.direct_count,"
            " c.macro_count FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE c.callee='rpc_call_sync'")
    }
    db.validate_schema(conn, deep=True)
    conn.close()

    assert rows == {
        "before_define": ("same_file", 1, 0),
        "while_defined": ("macro", 0, 1),
        "after_undef": ("same_file", 1, 0),
    }


@pytest.mark.parametrize("suffix", [".h", ".h_shipped"])
def test_future_macro_in_same_header_does_not_block_earlier_call(
        tmp_path, suffix):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / f"api{suffix}").write_text("""\
static int target(void) { return 1; }
static int caller(void) { return target(); }
#define target() 2
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.resolution,c.direct_count,c.macro_count FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='target'"
    ).fetchone()
    conn.close()

    assert tuple(row) == ("same_file", 1, 0)


def test_macro_from_included_c_source_remains_a_conservative_blocker(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text("#define wrapped_call() 1\n")
    (tree / "wrapper.c").write_text("""\
#include "member.c"
int caller(void) { return wrapped_call(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='wrapped_call'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "macro")


def test_call_edges_persist_mixed_and_expression_level_indirect_evidence(
        tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "calls.c").write_text("""\
static void target(void) { }
static void caller(struct ops *ops)
{
    target();
    {
        void (*target)(void) = 0;
        target();
    }
    ops->target();
    (*ops->finish)();
}
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["callee"]: (
            row["resolution"], row["direct_count"], row["indirect_count"])
        for row in conn.execute(
            "SELECT c.callee,c.resolution,c.direct_count,c.indirect_count"
            " FROM calls c JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE caller.name='caller'")
    }
    db.validate_schema(conn, deep=True)
    conn.close()

    assert rows == {
        "target": ("same_file", 1, 1),
        "ops->target": ("indirect", 0, 1),
        "*ops->finish": ("indirect", 0, 1),
    }


def test_call_build_resolves_quoted_c_members_in_the_same_unit(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "static int load_firmware(void) { return 1; }\n")
    (tree / "aggregate.c").write_text("""\
#include "member.c"
int aggregate(void) { return load_firmware(); }
""")
    other = tree / "drivers" / "other"
    other.mkdir(parents=True)
    (other / "firmware.c").write_text(
        "int load_firmware(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT parent.path,member.path,i.line FROM source_includes i"
        " JOIN files parent ON parent.id=i.includer_id"
        " JOIN files member ON member.id=i.included_id").fetchone()
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='aggregate'").fetchone()
    conn.close()

    assert tuple(include) == ("aggregate.c", "member.c", 1)
    assert tuple(call) == ("included_source", "member.c")


def test_commented_source_include_does_not_invent_a_translation_unit(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "static int member_helper(void) { return 1; }\n")
    (tree / "caller.c").write_text("""\
/* This is an example, not a preprocessing directive:
#include "member.c"
 */
int caller(void) { return member_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute("SELECT COUNT(*) FROM source_includes").fetchone()[0]
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='member_helper'"
    ).fetchone()[0]
    conn.close()

    assert includes == 0
    assert resolution == "unresolved"


def test_global_in_quoted_member_is_visible_through_root_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "int member_global(void) { return 1; }\n")
    (tree / "aggregate.c").write_text('#include "member.c"\n')
    (tree / "outside.c").write_text(
        "int outside(void) { return member_global(); }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='outside' AND c.callee='member_global'"
    ).fetchone()
    conn.close()

    assert tuple(row) == ("unique_global", "member.c")


def test_call_build_resolves_tree_root_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    member = tree / "lib" / "vdso" / "getrandom.c"
    member.parent.mkdir(parents=True)
    member.write_text(
        "static int __cvdso_getrandom(void) { return 1; }\n")
    wrapper = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    wrapper.mkdir(parents=True)
    (wrapper / "vgetrandom.c").write_text("""\
#include "lib/vdso/getrandom.c"
int __vdso_getrandom(void) { return __cvdso_getrandom(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT parent.path,member.path FROM source_includes i"
        " JOIN files parent ON parent.id=i.includer_id"
        " JOIN files member ON member.id=i.included_id").fetchone()
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='__vdso_getrandom'").fetchone()
    conn.close()

    assert tuple(include) == (
        "arch/x86/entry/vdso/vdso64/vgetrandom.c",
        "lib/vdso/getrandom.c",
    )
    assert tuple(call) == ("included_source", "lib/vdso/getrandom.c")


def test_multi_root_member_requires_every_translation_unit_to_agree(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "common.c").write_text(
        "static int common_call(void) { return helper(); }\n")
    (tree / "a.c").write_text("""\
static int helper(void) { return 1; }
#include "common.c"
""")
    (tree / "b.c").write_text('#include "common.c"\n')
    (tree / "global.c").write_text(
        "int helper(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='common_call' AND c.callee='helper'"
    ).fetchone()
    conn.close()

    # a.c binds its static helper; b.c binds the global one.  The source row
    # represents both instantiations and must not claim either identity.
    assert tuple(row) == (None, "ambiguous")


def test_kbuild_object_remains_a_root_when_also_included(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    with (tree / "Makefile").open("a") as makefile:
        makefile.write("obj-y += dual.o helper.o caller.o\n")
    (tree / "dual.c").write_text(
        "int exported_dual(void) { return helper(); }\n")
    (tree / "helper.c").write_text(
        "int helper(void) { return 1; }\n")
    (tree / "caller.c").write_text(
        "int outside(void) { return exported_dual(); }\n")
    wrapper = tree / "tools" / "testing"
    wrapper.mkdir(parents=True)
    (wrapper / "wrapper.c").write_text('#include "dual.c"\n')
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        (row["caller"], row["callee"]):
            (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT caller.name AS caller,c.callee,c.callee_id,c.resolution"
            " FROM calls c JOIN symbols caller ON caller.id=c.caller_id")
    }
    is_root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id WHERE f.path='dual.c'"
    ).fetchone()
    conn.close()

    assert is_root is not None
    # The call written in dual.c has different kernel/tools contexts.
    assert rows[("exported_dual", "helper")] == (None, "ambiguous")
    # Other kernel sources may still bind dual.c's standalone global identity.
    assert rows[("outside", "exported_dual")][1] == "unique_global"


def test_non_build_make_object_reference_does_not_create_a_root(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    with (tree / "Makefile").open("a") as makefile:
        makefile.write("clean-files += member.o\n")
    (tree / "member.c").write_text(
        "int image_only(void) { return 1; }\n")
    image = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    image.mkdir(parents=True)
    (image / "wrapper.c").write_text('#include "member.c"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "caller.c").write_text(
        "int kernel_call(void) { return image_only(); }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='kernel_call' AND c.callee='image_only'"
    ).fetchone()
    false_root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id WHERE f.path='member.c'"
    ).fetchone()
    conn.close()

    assert false_root is None
    assert tuple(row) == (None, "unresolved")


def test_tools_build_object_list_records_a_translation_unit_root(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    library = tree / "tools" / "lib" / "bpf"
    library.mkdir(parents=True)
    (library / "Build").write_text("libbpf-y += member.o\n")
    (library / "member.c").write_text(
        "int tools_member(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id"
        " WHERE f.path='tools/lib/bpf/member.c'"
    ).fetchone()
    conn.close()

    assert root is not None


def test_literal_make_compile_and_link_rules_record_translation_unit_roots(
        tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    ring = tree / "tools" / "virtio" / "ringtest"
    ring.mkdir(parents=True)
    (ring / "Makefile").write_text("""\
dual.o: dual.c main.h
standalone: dual.o
poll.o: poll.c dual.c main.h
poll: poll.o
""")
    (ring / "main.h").write_text("\n")
    (ring / "dual.c").write_text(
        "int dual_source(void) { return 1; }\n")
    (ring / "poll.c").write_text('#include "dual.c"\n')
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    roots = {
        row[0] for row in conn.execute(
            "SELECT f.path FROM translation_unit_roots root"
            " JOIN files f ON f.id=root.file_id")
    }
    conn.close()

    assert "tools/virtio/ringtest/dual.c" in roots
    assert "tools/virtio/ringtest/poll.c" in roots


def test_kbuild_include_paths_resolve_angle_and_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    shared = tree / "shared"
    shared.mkdir()
    (shared / "angle_member.c").write_text(
        "static int angle_helper(void) { return 1; }\n")
    (shared / "quote_member.c").write_text(
        "static int quote_helper(void) { return 1; }\n")
    probes = tree / "tools" / "probes"
    probes.mkdir(parents=True)
    (probes / "Makefile").write_text("""\
hostprogs := angle quote
HOSTCFLAGS_angle.o := -I$(srctree)/shared/
HOSTCFLAGS_quote.o := -I $(srctree)/shared/
""")
    (probes / "angle.c").write_text("""\
#include <angle_member.c>
int angle(void) { return angle_helper(); }
""")
    (probes / "quote.c").write_text("""\
#include "quote_member.c"
int quote(void) { return quote_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = {
        (row["parent"], row["member"])
        for row in conn.execute(
            "SELECT parent.path AS parent,member.path AS member"
            " FROM source_includes edge"
            " JOIN files parent ON parent.id=edge.includer_id"
            " JOIN files member ON member.id=edge.included_id")
    }
    resolutions = {
        row["caller"]: row["resolution"]
        for row in conn.execute(
            "SELECT caller.name AS caller,c.resolution FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id")
    }
    conn.close()

    assert includes == {
        ("tools/probes/angle.c", "shared/angle_member.c"),
        ("tools/probes/quote.c", "shared/quote_member.c"),
    }
    assert resolutions["angle"] == "included_source"
    assert resolutions["quote"] == "included_source"


def test_target_include_order_distinguishes_srctree_from_objtree(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    tools_lib = tree / "tools" / "arch" / "x86" / "lib"
    arch_lib = tree / "arch" / "x86" / "lib"
    probe_dir = tree / "arch" / "x86" / "tools"
    tools_lib.mkdir(parents=True)
    arch_lib.mkdir(parents=True)
    probe_dir.mkdir(parents=True)
    (tools_lib / "insn.c").write_text(
        "static int decode_insn(void) { return 1; }\n")
    (arch_lib / "insn.c").write_text(
        "static int decode_insn(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
HOSTCFLAGS_probe.o := -I$(srctree)/tools/arch/x86/lib/ \\
                      -I$(objtree)/arch/x86/lib/
""")
    (probe_dir / "probe.c").write_text("""\
#include <insn.c>
int probe(void) { return decode_insn(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='arch/x86/tools/probe.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='probe' AND c.callee='decode_insn'"
    ).fetchone()
    conn.close()

    assert include[0] == "tools/arch/x86/lib/insn.c"
    assert tuple(resolution) == (
        "included_source", "tools/arch/x86/lib/insn.c")


def test_general_include_directory_precedes_target_specific_directory(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    general = tree / "general"
    specific = tree / "specific"
    probe_dir = tree / "tools" / "probe"
    general.mkdir()
    specific.mkdir()
    probe_dir.mkdir(parents=True)
    (general / "member.c").write_text(
        "static int selected_helper(void) { return 1; }\n")
    (specific / "member.c").write_text(
        "static int selected_helper(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
ccflags-y := -I$(srctree)/general
CFLAGS_probe.o := -I$(srctree)/specific
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='tools/probe/probe.c'"
    ).fetchone()
    conn.close()

    assert include[0] == "general/member.c"


def test_c_include_flag_categories_follow_makefile_lib_order(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    probe_dir = tree / "drivers" / "probe"
    probe_dir.mkdir(parents=True)
    for dirname in ("cpp", "cflags", "subdir", "local", "target", "linker"):
        include_dir = tree / dirname
        include_dir.mkdir()
        (include_dir / "member.c").write_text(
            f"static int selected_helper(void) {{ return {len(dirname)}; }}\n")
    # Deliberately list the variables in reverse semantic order. LDFLAGS is not
    # part of C compilation at all; the remaining order comes from Makefile.lib.
    (probe_dir / "Makefile").write_text("""\
LDFLAGS_probe.o := -I$(srctree)/linker
CFLAGS_probe.o := -I$(srctree)/target
ccflags-y := -I$(srctree)/local
subdir-ccflags-y := -I$(srctree)/subdir
KBUILD_CFLAGS := -I$(srctree)/cflags
KBUILD_CPPFLAGS := -I$(srctree)/cpp
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='drivers/probe/probe.c'"
    ).fetchone()
    conn.close()

    assert include[0] == "cpp/member.c"


def test_opaque_include_directory_blocks_a_later_source_guess(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    specific = tree / "specific"
    probe_dir = tree / "tools" / "probe"
    specific.mkdir()
    probe_dir.mkdir(parents=True)
    (specific / "member.c").write_text(
        "static int selected_helper(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
CFLAGS_probe.o := -I$(objtree)/generated -I$(srctree)/specific
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT 1 FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " WHERE parent.path='tools/probe/probe.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='probe' AND c.callee='selected_helper'"
    ).fetchone()[0]
    conn.close()

    assert include is None
    assert resolution == "unresolved"


@pytest.mark.parametrize("obj", ["$(obj)", "${obj}"])
def test_obj_include_paths_do_not_alias_generated_output_to_sources(
        tmp_path, obj):
    tree = _tree(tmp_path / "linux-9.9")
    probe_dir = tree / "drivers" / "probe"
    generated = probe_dir / "generated"
    generated.mkdir(parents=True)
    (generated / "member.c").write_text(
        "static int generated_helper(void) { return 1; }\n")
    (probe_dir / "Makefile").write_text(
        f"ccflags-y := -I{obj}/generated\n")
    (probe_dir / "caller.c").write_text("""\
#include <member.c>
int caller(void) { return generated_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT 1 FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " WHERE parent.path='drivers/probe/caller.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='generated_helper'"
    ).fetchone()[0]
    conn.close()

    assert include is None
    assert resolution == "unresolved"


def test_directory_local_include_flags_do_not_leak_into_children(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    shared = tree / "shared"
    shared.mkdir()
    (shared / "member.c").write_text(
        "static int member_helper(void) { return 1; }\n")
    parent = tree / "tools" / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "Makefile").write_text(
        "ccflags-y := -I$(srctree)/shared\n")
    (child / "caller.c").write_text("""\
#include <member.c>
int caller(void) { return member_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute(
        "SELECT COUNT(*) FROM source_includes").fetchone()[0]
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='member_helper'"
    ).fetchone()[0]
    conn.close()

    assert includes == 0
    assert resolution == "unresolved"


def test_included_member_uses_its_root_translation_unit_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    member = tree / "lib" / "vdso" / "member.c"
    member.parent.mkdir(parents=True)
    member.write_text(
        "static int member_call(void) { return kernel_only(); }\n")
    wrapper = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    wrapper.mkdir(parents=True)
    (wrapper / "wrapper.c").write_text(
        '#include "lib/vdso/member.c"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "helper.c").write_text(
        "int kernel_only(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='member_call' AND c.callee='kernel_only'"
    ).fetchone()
    domain = conn.execute(
        "SELECT call_domain FROM files"
        " WHERE path='arch/x86/entry/vdso/vdso64/wrapper.c'"
    ).fetchone()[0]
    conn.close()

    assert domain.startswith("image:")
    assert tuple(row) == (None, "unresolved")


def test_header_call_sites_do_not_guess_a_linked_image_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    header = tree / "include" / "linux" / "shared.h"
    header.parent.mkdir(parents=True)
    header.write_text("""\
static inline int header_local(void) { return 1; }
static inline int header_call(void)
{
    return header_local() + kernel_only();
}
""")
    image = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    image.mkdir(parents=True)
    (image / "wrapper.c").write_text(
        '#include "include/linux/shared.h"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "helper.c").write_text(
        "int kernel_only(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["callee"]: (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE caller.name='header_call'")
    }
    conn.close()

    assert rows["header_local"][1] == "same_file"
    assert rows["kernel_only"] == (None, "ambiguous")


def test_image_call_respects_shared_header_identity_blockers(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    header = tree / "include" / "linux" / "api.h"
    header.parent.mkdir(parents=True)
    header.write_text("#define shadowed() 0\n")
    image = tree / "arch" / "x86" / "boot"
    image.mkdir(parents=True)
    (image / "main.c").write_text("""\
#include <linux/api.h>
int image_call(void) { return shadowed(); }
""")
    (image / "helper.c").write_text(
        "int shadowed(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='image_call' AND c.callee='shadowed'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "ambiguous")


def test_call_build_ignores_unparseable_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_bytes(b"\0binary input\n")
    (tree / "aggregate.c").write_text("""\
#include "member.c"
int aggregate(void) { return missing_member(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute("SELECT COUNT(*) FROM source_includes").fetchone()[0]
    call = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='aggregate' AND c.callee='missing_member'"
    ).fetchone()
    status = conn.execute(
        "SELECT index_status FROM files WHERE path='member.c'").fetchone()[0]
    conn.close()

    assert includes == 0
    assert tuple(call) == (None, "unresolved")
    assert status == "skipped_binary"


def test_sysfs_attribute_callback_does_not_make_call_ambiguous(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "dock.c").write_text("""\
static int undock(void) { return 0; }
static int handle_eject(void) { return undock(); }
static DEVICE_ATTR_WO(undock);
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    definitions = list(conn.execute(
        "SELECT name,kind FROM symbols WHERE name IN ('undock','dev_attr_undock')"
        " ORDER BY name,kind"))
    call = conn.execute(
        "SELECT c.resolution,target.name FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " WHERE caller.name='handle_eject' AND c.callee='undock'"
    ).fetchone()
    conn.close()

    assert [tuple(row) for row in definitions] == [
        ("dev_attr_undock", "variable"), ("undock", "function")]
    assert tuple(call) == ("same_file", "undock")


def test_call_domains_follow_independently_linked_kbuild_programs(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    sample = tree / "samples" / "bpf"
    sample.mkdir(parents=True)
    (sample / "Makefile").write_text("""\
tprogs-y := alpha beta
alpha-objs := alpha_main.o alpha_helper.o shared.o
beta-objs := beta_main.o shared.o
""")
    (sample / "alpha_main.c").write_text("""\
int alpha_helper(void);
int fprintf(void);
int alpha_entry(void) { return alpha_helper() + fprintf(); }
""")
    (sample / "alpha_helper.c").write_text(
        "int alpha_helper(void) { return 1; }\n")
    (sample / "beta_main.c").write_text(
        "int beta_entry(void) { return 2; }\n")
    (sample / "shared.c").write_text(
        "int shared_helper(void) { return 3; }\n")
    (sample / "trace_kern.c").write_text(
        "int bpf_program(void) { return 4; }\n")
    (sample / "kernel_piece.c").write_text(
        "int kernel_piece(void) { return 5; }\n")
    hid = tree / "samples" / "hid"
    hid.mkdir()
    (hid / "mouse.bpf.c").write_text(
        "int hid_bpf_program(void) { return 7; }\n")
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "print.c").write_text(
        "int fprintf(void) { return 6; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'samples/%'"))
    calls = {row["callee"]: (row["callee_id"], row["resolution"])
             for row in conn.execute(
                 "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
                 " JOIN symbols s ON s.id=c.caller_id"
                 " WHERE s.name='alpha_entry'")}
    conn.close()

    assert domains["samples/bpf/alpha_main.c"] == \
        "program:samples/bpf:alpha"
    assert domains["samples/bpf/alpha_helper.c"] == \
        "program:samples/bpf:alpha"
    assert domains["samples/bpf/shared.c"] == "isolated:samples/bpf/shared.c"
    assert domains["samples/bpf/trace_kern.c"] == \
        "isolated:samples/bpf/trace_kern.c"
    assert domains["samples/bpf/kernel_piece.c"] == \
        "isolated:samples/bpf/kernel_piece.c"
    assert domains["samples/hid/mouse.bpf.c"] == \
        "isolated:samples/hid/mouse.bpf.c"
    assert calls["alpha_helper"][1] == "unique_global"
    assert calls["alpha_helper"][0] is not None
    assert calls["fprintf"] == (None, "unresolved")


def test_kbuild_program_header_bindings_block_false_global_targets(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    program = tree / "scripts" / "host-tool"
    program.mkdir(parents=True)
    (program / "Makefile").write_text("""\
hostprogs := analyzer
analyzer-objs := main.o helpers.o
""")
    (program / "bindings.h").write_text("""\
static int header_helper(void) { return 1; }
#define header_macro() 2
static int (*header_pointer)(void);
""")
    (program / "main.c").write_text("""\
#include "bindings.h"
int analyze(void)
{
    return header_helper() + header_macro() + header_pointer();
}
""")
    (program / "helpers.c").write_text("""\
int header_helper(void) { return 10; }
int header_macro(void) { return 20; }
int header_pointer(void) { return 30; }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'scripts/host-tool/%'"))
    calls = {row["callee"]: (row["callee_id"], row["resolution"])
             for row in conn.execute(
                 "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
                 " JOIN symbols caller ON caller.id=c.caller_id"
                 " WHERE caller.name='analyze'")}
    conn.close()

    domain = "program:scripts/host-tool:analyzer"
    assert domains["scripts/host-tool/main.c"] == domain
    assert domains["scripts/host-tool/helpers.c"] == domain
    assert calls == {
        "header_helper": (None, "ambiguous"),
        "header_macro": (None, "ambiguous"),
        "header_pointer": (None, "ambiguous"),
    }


def test_boot_image_companions_share_a_domain_separate_from_vmlinux(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    boot = tree / "arch" / "x86" / "boot"
    boot.mkdir(parents=True)
    (boot / "main.c").write_text("""\
int detect_memory(void);
int main(void) { return detect_memory(); }
""")
    (boot / "memory.c").write_text(
        "int detect_memory(void) { return 1; }\n")
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "memory.c").write_text(
        "int detect_memory(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'arch/x86/boot/%'"))
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='main' AND c.callee='detect_memory'").fetchone()
    conn.close()

    assert domains["arch/x86/boot/main.c"] == "image:arch:x86:boot"
    assert domains["arch/x86/boot/memory.c"] == "image:arch:x86:boot"
    assert tuple(call) == ("unique_global", "arch/x86/boot/memory.c")


@pytest.mark.parametrize("name", [
    "hostprogs", "host-progs", "userprogs", "hostprogs-always-y",
    "hostprogs-always-m", "userprogs-always-$(CONFIG_CC_CAN_LINK)",
])
def test_kbuild_program_list_names_are_recognized(name):
    assert indexer._is_program_list(name)


@pytest.mark.parametrize("name", [
    "hostprogs-installed", "userprogs-always-n", "obj-y", "always-y",
])
def test_unrelated_kbuild_lists_are_not_programs(name):
    assert not indexer._is_program_list(name)


def test_pure_kbuild_addprefix_object_list_is_expanded():
    values = {
        "libfdt-objs": ["fdt.o fdt_ro.o"],
        "libfdt": ["$(addprefix libfdt/,$(libfdt-objs))"],
        "fdtoverlay-objs": ["fdtoverlay.o $(libfdt)"],
    }
    assert indexer._expand_make_value(
        " ".join(values["fdtoverlay-objs"]), values
    ) == "fdtoverlay.o libfdt/fdt.o libfdt/fdt_ro.o"


@pytest.mark.parametrize("name", [
    "obj-y", "obj-$(CONFIG_TEST)", "lib-m", "module-objs", "module-y",
    "module-$(CONFIG_TEST)", "always-y",
])
def test_kbuild_compile_link_object_lists_are_recognized(name):
    assert indexer._is_kbuild_object_list(name)


@pytest.mark.parametrize("name", [
    "clean-files", "targets", "ccflags-y", "subdir-ccflags-y", "CFLAGS_x.o",
])
def test_non_build_object_lists_are_not_compile_evidence(name):
    assert not indexer._is_kbuild_object_list(name)
