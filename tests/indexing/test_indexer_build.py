"""Build validation, scanning, parser outcomes, timing, and atomic publication."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kernel_atlas import cparse, db, indexer

from .helpers import _tree


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


@pytest.mark.parametrize("untrusted_build_file", ["symlink", "excluded-directory"])
def test_build_evidence_respects_the_indexed_source_boundary(
        tmp_path, untrusted_build_file, monkeypatch):
    tree = _tree(tmp_path / "linux-9.9")
    program = tree / "scripts" / "probe"
    program.mkdir(parents=True)
    (program / "main.c").write_text(
        "int helper(void); int main(void) { return helper(); }\n")
    (program / "helper.c").write_text("int helper(void) { return 1; }\n")
    if untrusted_build_file == "symlink":
        external = tmp_path / "external.Makefile"
        external.write_text("hostprogs := probe\nprobe-objs := main.o helper.o\n")
        try:
            (program / "Makefile").symlink_to(external)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    else:
        excluded = tree / ".git"
        excluded.mkdir()
        (excluded / "Makefile").write_text(
            "hostprogs := ../scripts/probe/probe\n"
            "probe-objs := ../scripts/probe/main.o ../scripts/probe/helper.o\n")

    # Build evidence must reuse the file inventory, not walk excluded paths
    # again in a separate discovery pass.
    monkeypatch.setattr(Path, "rglob", lambda *a, **kw: pytest.fail("unexpected rescan"))
    out = tmp_path / "index.db"
    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    try:
        domains = dict(conn.execute(
            "SELECT path,call_domain FROM files WHERE ext='.c'"))
        assert domains == {
            "scripts/probe/main.c": "isolated:scripts/probe/main.c",
            "scripts/probe/helper.c": "isolated:scripts/probe/helper.c",
        }
        call = conn.execute("SELECT resolution,callee_id FROM calls").fetchone()
        assert tuple(call) == ("unresolved", None)
    finally:
        conn.close()


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
