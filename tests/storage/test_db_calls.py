"""Persisted call identities, resolution constraints, and occurrence counts."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas.storage import db

from .helpers import _metadata
from .helpers import _identity_index


def test_call_rows_enforce_resolution_identity_consistency(tmp_path):
    conn = db.create(tmp_path / "call-constraint.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calls(caller_id,callee,resolution) "
            "VALUES (1,'target','unique_global')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calls(caller_id,callee,callee_id,resolution) "
            "VALUES (1,'target',2,'ambiguous')")
    conn.close()


def test_validate_schema_rejects_cross_file_same_file_resolution(tmp_path):
    conn = _identity_index(tmp_path, "same_file")
    with pytest.raises(db.SchemaError, match="inconsistent resolved call"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_rejects_unrecorded_included_source_resolution(tmp_path):
    conn = _identity_index(tmp_path, "included_source", target_static=1)
    with pytest.raises(db.SchemaError, match="inconsistent resolved call"):
        db.validate_schema(conn, deep=True)
    conn.close()


@pytest.mark.parametrize("target_static,target_name", [(1, "target"), (0, "other")])
def test_validate_schema_rejects_invalid_unique_global_target(
        tmp_path, target_static, target_name):
    conn = _identity_index(
        tmp_path, "unique_global", target_static=target_static,
        target_name=target_name)
    with pytest.raises(db.SchemaError, match="inconsistent resolved call"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_accepts_a_reproducible_unique_global_target(tmp_path):
    conn = _identity_index(tmp_path, "unique_global")
    assert db.validate_schema(conn, deep=True)["kernel_version"] == "9.9"
    conn.close()


def test_deep_validation_checks_persisted_indirect_call_evidence(tmp_path):
    conn = _identity_index(tmp_path, "unique_global")
    conn.execute(
        "UPDATE calls SET callee_id=NULL,resolution='indirect' WHERE caller_id=1")
    conn.execute("UPDATE meta SET value='0' WHERE key='n_calls_resolved'")
    conn.execute("UPDATE meta SET value='1' WHERE key='n_calls_indirect'")
    conn.commit()

    with pytest.raises(db.SchemaError, match="classification is inconsistent"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_call_rows_require_nonempty_occurrence_evidence(tmp_path):
    conn = db.create(tmp_path / "empty-call-evidence.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO calls(caller_id,callee,direct_count,indirect_count,"
            " macro_count) VALUES (1,'target',0,0,0)")
    conn.close()


def test_deep_validation_rejects_inconsistent_call_occurrence_counts(tmp_path):
    conn = _identity_index(tmp_path, "unique_global")
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("UPDATE calls SET direct_count=999 WHERE caller_id=1")
    conn.execute("PRAGMA ignore_check_constraints=OFF")
    conn.commit()

    with pytest.raises(db.SchemaError, match="occurrence count"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_rejects_duplicate_unique_global_candidates(tmp_path):
    conn = _identity_index(tmp_path, "unique_global")
    conn.execute(
        "INSERT INTO files(id,path,dir_id,name,ext,lines,n_symbols,index_status)"
        " VALUES (3,'three.c',1,'three.c','.c',1,1,'parsed')")
    conn.execute(
        "INSERT INTO symbols(id,file_id,name,kind,start_line,end_line)"
        " VALUES (3,3,'target','function',1,1)")
    conn.execute(
        "UPDATE dirs SET n_files=3,n_files_recursive=3 WHERE id=1")
    conn.execute("UPDATE meta SET value='3' WHERE key IN ('n_files','n_symbols')")
    conn.commit()

    with pytest.raises(db.SchemaError, match="impossible unique_global"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_rejects_cross_program_unique_global_target(tmp_path):
    conn = _identity_index(tmp_path, "unique_global")
    conn.execute(
        "UPDATE files SET call_domain=CASE id WHEN 1 THEN 'program:a'"
        " ELSE 'program:b' END")
    conn.commit()

    with pytest.raises(db.SchemaError, match="impossible unique_global"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_rejects_a_downgraded_unique_local_call(tmp_path):
    conn = db.create(tmp_path / "downgraded-local.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (1,'',NULL,'linux',0,1,1)")
    conn.execute(
        "INSERT INTO files(id,path,dir_id,name,ext,lines,n_symbols,index_status)"
        " VALUES (1,'one.c',1,'one.c','.c',2,2,'parsed')")
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,start_line,end_line)"
        " VALUES (?,1,?,'function',?,?)", [
            (1, "target", 1, 1), (2, "caller", 2, 2),
        ])
    conn.execute(
        "INSERT INTO calls(caller_id,callee,resolution)"
        " VALUES (2,'target','ambiguous')")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(
            has_calls="1", kinds="function,syscall,macro,variable",
            n_files="1", n_symbols="2", n_calls="1",
            n_calls_ambiguous="1"))
    conn.commit()

    with pytest.raises(db.SchemaError, match="expected same_file"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_validate_schema_rejects_dangling_call_identity(tmp_path):
    conn = db.create(tmp_path / "dangling.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth)"
        " VALUES (1,'',NULL,'linux',0)")
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (999,'target',888,'unique_global')")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(has_calls="1", n_calls="1", n_calls_resolved="1",
                  kinds="function,syscall,macro,variable"))
    conn.commit()
    with pytest.raises(db.SchemaError, match="dangling reference"):
        db.validate_schema(conn, deep=True)
    conn.close()
