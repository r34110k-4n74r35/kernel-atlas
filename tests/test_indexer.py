from __future__ import annotations

import time
from pathlib import Path

import pytest

from kernel_atlas import cparse, db, indexer


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
