from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import db


def _metadata(**overrides):
    values = {
        "schema_version": db.SCHEMA_VERSION,
        "kernel_version": "9.9",
        "source": "test",
        "tree_path": "/tmp/linux-9.9",
        "built_at": "2026-01-01T00:00:00",
        "kinds": "function",
        "has_calls": "0",
        "n_dirs": "1",
        "n_files": "0",
        "n_symbols": "0",
        "n_subsystems": "0",
        "n_parse_skipped": "0",
        "n_parse_failed": "0",
        "n_oversize": "0",
        "n_symlinks": "0",
        "build_seconds": "0.0",
    }
    values.update(overrides)
    return list(values.items())


def test_validate_schema_accepts_a_complete_current_index(tmp_path):
    conn = db.create(tmp_path / "current.db")
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", _metadata())
    conn.commit()
    assert db.validate_schema(conn)["kernel_version"] == "9.9"
    conn.close()


@pytest.mark.parametrize("schema", [None, "1", "999"])
def test_validate_schema_rejects_missing_or_wrong_version(tmp_path, schema):
    conn = db.create(tmp_path / "wrong.db")
    conn.execute("INSERT INTO meta(key, value) VALUES ('kernel_version', '9.9')")
    if schema is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                     (schema,))
    conn.commit()
    with pytest.raises(db.SchemaError):
        db.validate_schema(conn)
    conn.close()


def test_validate_schema_rejects_matching_metadata_with_missing_tables(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", _metadata())
    conn.commit()
    with pytest.raises(db.SchemaError, match="missing table"):
        db.validate_schema(conn)
    conn.close()


def test_validate_schema_wraps_a_non_index_database(tmp_path):
    conn = sqlite3.connect(tmp_path / "other.db")
    with pytest.raises(db.SchemaError, match="metadata table"):
        db.validate_schema(conn)
    conn.close()


def test_validate_schema_rejects_a_claimed_current_schema_missing_columns(tmp_path):
    path = tmp_path / "old-layout.db"
    conn = db.create(path)
    conn.execute("ALTER TABLE files RENAME TO files_current")
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", _metadata())
    conn.commit()
    with pytest.raises(db.SchemaError, match="files is missing column"):
        db.validate_schema(conn)
    conn.close()


@pytest.mark.parametrize("key,value", [
    ("n_files", "oops"),
    ("n_parse_failed", "-1"),
    ("has_calls", "yes"),
    ("build_seconds", "nan"),
])
def test_validate_schema_rejects_invalid_typed_metadata(tmp_path, key, value):
    conn = db.create(tmp_path / "bad-meta.db")
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)", _metadata(**{key: value}))
    conn.commit()
    with pytest.raises(db.SchemaError, match=key):
        db.validate_schema(conn)
    conn.close()


@pytest.mark.parametrize("key,value", [
    ("n_files", None),
    ("n_files", sqlite3.Binary(b"12")),
    ("n_files", "²"),
    ("build_seconds", None),
])
def test_validate_schema_rejects_non_text_or_non_ascii_numeric_metadata(
        tmp_path, key, value):
    conn = db.create(tmp_path / "bad-meta-type.db")
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)", _metadata(**{key: value}))
    conn.commit()
    with pytest.raises(db.SchemaError, match="metadata"):
        db.validate_schema(conn)
    conn.close()


def test_validate_schema_rejects_incomplete_current_metadata(tmp_path):
    conn = db.create(tmp_path / "incomplete.db")
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [
        ("schema_version", db.SCHEMA_VERSION),
        ("kernel_version", "9.9"),
    ])
    conn.commit()
    with pytest.raises(db.SchemaError, match="missing metadata"):
        db.validate_schema(conn)
    conn.close()


def test_validate_schema_rejects_unsafe_kernel_version(tmp_path):
    conn = db.create(tmp_path / "unsafe-version.db")
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        _metadata(kernel_version="../../outside"),
    )
    conn.commit()
    with pytest.raises(db.SchemaError, match="unsafe kernel version"):
        db.validate_schema(conn)
    conn.close()
