"""Schema compatibility, metadata contracts, and basic validation."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import db

from .helpers import _metadata


def test_validate_schema_accepts_a_complete_current_index(tmp_path):
    conn = db.create(tmp_path / "current.db")
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", _metadata())
    conn.commit()
    assert db.validate_schema(conn)["kernel_version"] == "9.9"
    conn.close()


def test_deep_validation_accepts_a_valid_readonly_empty_index(tmp_path):
    path = tmp_path / "empty.db"
    conn = db.create(path)
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth)"
        " VALUES (1,'',NULL,'linux',0)")
    conn.executemany("INSERT INTO meta(key,value) VALUES (?,?)", _metadata())
    conn.commit()
    conn.close()

    reader = db.connect(path)
    statements = []
    reader.set_trace_callback(statements.append)
    assert db.validate_schema(reader, deep=True)["kernel_version"] == "9.9"
    assert not any("validation_unit_members" in sql for sql in statements)
    reader.close()


def test_deep_validation_compares_core_metadata_to_rows(tmp_path):
    conn = db.create(tmp_path / "wrong-count.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth)"
        " VALUES (1,'',NULL,'linux',0)")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)", _metadata(n_files="999"))
    conn.commit()

    with pytest.raises(db.SchemaError, match="files row count"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_rejects_corrupt_primary_ownership(tmp_path):
    conn = db.create(tmp_path / "bad-primary.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (1,'',NULL,'linux',0,1,1)")
    conn.execute(
        "INSERT INTO files(id,path,dir_id,name,ext,index_status)"
        " VALUES (1,'Makefile',1,'Makefile','','indexed')")
    conn.execute(
        "INSERT INTO subsystems(id,name,n_files,n_primary_files)"
        " VALUES (1,'OWNER',1,0)")
    conn.execute(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES ('file',1,1,10,0,0)")
    conn.execute(
        "INSERT INTO dir_subsys(dir_id,subsystem_id,n_claimed,n_primary,coverage,rank)"
        " VALUES (1,1,1,0,0.0,0)")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(n_files="1", n_subsystems="1"))
    conn.commit()

    with pytest.raises(db.SchemaError, match="co-primary ownership"):
        db.validate_schema(conn, deep=True)
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
    ("n_calls", "0" * 5000),
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


def test_validate_schema_rejects_partial_managed_tree_identity(tmp_path):
    conn = db.create(tmp_path / "partial-tree-id.db")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(managed_tree_id="a" * 64),
    )
    conn.commit()
    with pytest.raises(db.SchemaError, match="incomplete managed source identity"):
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


def test_validate_schema_rejects_inconsistent_call_resolution_counts(tmp_path):
    conn = db.create(tmp_path / "bad-call-counts.db")
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        _metadata(has_calls="1", n_calls="3", n_calls_resolved="1",
                  n_calls_ambiguous="1",
                  kinds="function,syscall,macro,variable"),
    )
    conn.commit()
    with pytest.raises(db.SchemaError, match="do not add up"):
        db.validate_schema(conn)
    conn.close()
