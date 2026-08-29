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
        "n_type_aliases": "0",
        "n_type_members": "0",
        "n_subsystems": "0",
        "n_calls": "0",
        "n_calls_resolved": "0",
        "n_calls_ambiguous": "0",
        "n_calls_macro": "0",
        "n_calls_indirect": "0",
        "n_calls_unresolved": "0",
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


def _identity_index(tmp_path, resolution: str, *, target_static: int = 0,
                    target_name: str = "target"):
    conn = db.create(tmp_path / f"bad-{resolution}.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (1,'',NULL,'linux',0,2,2)")
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext,lines,n_symbols,index_status)"
        " VALUES (?,?,1,?,'.c',1,1,'parsed')",
        [(1, "one.c", "one.c"), (2, "two.c", "two.c")])
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,start_line,end_line,is_static)"
        " VALUES (?,?,?,?,1,1,?)",
        [(1, 1, "caller", "function", 0),
         (2, 2, target_name, "function", target_static)])
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (1,'target',2,?)", (resolution,))
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(has_calls="1", n_files="2", n_symbols="2", n_calls="1",
                  n_calls_resolved="1",
                  kinds="function,syscall,macro,variable"))
    conn.commit()
    return conn


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


@pytest.mark.parametrize("table,column", [
    ("dirs", "path"), ("calls", "callee"),
])
def test_deep_validation_rejects_blob_text_identities(
        tmp_path, table, column):
    conn = _identity_index(tmp_path, "unique_global")
    if table == "dirs":
        conn.execute("UPDATE dirs SET path=? WHERE id=1",
                     (sqlite3.Binary(b"root"),))
    else:
        conn.execute("UPDATE calls SET callee=?",
                     (sqlite3.Binary(b"target"),))
    conn.commit()

    with pytest.raises(db.SchemaError, match=f"table {table}.*invalid value"):
        db.validate_schema(conn, deep=True)
    conn.close()


@pytest.mark.parametrize("assignment", [
    "kind='bogus'",
    "start_line=0",
    "end_line=0",
    "is_static=2",
    "is_inline=-1",
    "is_exported=3",
])
def test_deep_validation_rejects_invalid_symbol_domains(tmp_path, assignment):
    conn = _identity_index(tmp_path, "unique_global")
    conn.execute(f"UPDATE symbols SET {assignment} WHERE id=1")
    conn.commit()

    with pytest.raises(db.SchemaError, match="table symbols.*invalid value"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_anchors_aggregate_table_counts(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "truncated-members.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    conn.execute(
        "DELETE FROM type_members WHERE id=(SELECT m.id FROM type_members m"
        " LEFT JOIN type_members child ON child.parent_id=m.id"
        " WHERE child.id IS NULL ORDER BY m.id DESC LIMIT 1)"
    )
    conn.commit()

    with pytest.raises(db.SchemaError, match="type_members row count"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_rejects_aliases_on_nonaggregates(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "function-alias.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    function_id = conn.execute(
        "SELECT id FROM symbols WHERE kind='function' LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'not_a_type')",
        (function_id,),
    )
    count = conn.execute("SELECT COUNT(*) FROM type_aliases").fetchone()[0]
    conn.execute(
        "UPDATE meta SET value=? WHERE key='n_type_aliases'", (str(count),))
    conn.commit()

    with pytest.raises(db.SchemaError, match="non-aggregate symbol"):
        db.validate_schema(conn, deep=True)
    conn.close()


@pytest.mark.parametrize("corruption,error", [
    ("json", "invalid structure metadata"),
    ("description_source", "table type_members.*invalid value"),
])
def test_deep_validation_rejects_invalid_aggregate_metadata(
        mini_index, tmp_path, corruption, error):
    import shutil

    copied = tmp_path / f"bad-aggregate-{corruption}.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    if corruption == "json":
        conn.execute(
            "UPDATE symbols SET conditions='not-json'"
            " WHERE id=(SELECT id FROM symbols WHERE kind='struct' LIMIT 1)"
        )
    else:
        conn.execute(
            "UPDATE type_members SET description=NULL"
            " WHERE id=(SELECT id FROM type_members"
            " WHERE description_source IS NOT NULL LIMIT 1)"
        )
    conn.commit()

    with pytest.raises(db.SchemaError, match=error):
        db.validate_schema(conn, deep=True)
    conn.close()


@pytest.mark.parametrize("corruption,error", [
    ("cross_symbol_parent", "aggregate-member identity"),
    ("field_parent", "aggregate-member identity"),
    ("ordinal_gap", "member ordinals are not contiguous"),
    ("preorder", "member preorder is not contiguous"),
])
def test_deep_validation_rejects_corrupt_aggregate_hierarchies(
        mini_index, tmp_path, corruption, error):
    import shutil

    copied = tmp_path / f"bad-aggregate-{corruption}.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    if corruption == "cross_symbol_parent":
        child = conn.execute(
            "SELECT id,symbol_id FROM type_members WHERE name='s_inodes_count'"
        ).fetchone()
        parent = conn.execute(
            "SELECT id FROM type_members WHERE symbol_id!=? LIMIT 1",
            (child["symbol_id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE type_members SET parent_id=? WHERE id=?",
            (parent, child["id"]),
        )
    elif corruption == "field_parent":
        parent = conn.execute(
            "SELECT id FROM type_members WHERE name='s_blocks_count'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE type_members SET parent_id=? WHERE name='s_inodes_count'",
            (parent,),
        )
    elif corruption == "ordinal_gap":
        conn.execute(
            "UPDATE type_members SET ordinal=ordinal+100"
            " WHERE id=(SELECT id FROM type_members ORDER BY id DESC LIMIT 1)"
        )
    else:
        # ``generation`` is the first child of an anonymous union. Making it a
        # root closes that union before the following nested-struct child tries
        # to re-enter it.
        conn.execute(
            "UPDATE type_members SET parent_id=NULL WHERE name='generation'"
        )
    conn.commit()

    with pytest.raises(db.SchemaError, match=error):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_checks_references_even_without_fk_declarations(
        tmp_path):
    template = tmp_path / "template.db"
    conn = db.create(template)
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth)"
        " VALUES (1,'',NULL,'linux',0)")
    conn.executemany("INSERT INTO meta(key,value) VALUES (?,?)", _metadata())
    conn.commit()
    conn.close()

    forged = tmp_path / "no-constraints.db"
    conn = sqlite3.connect(forged)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS template", (str(template),))
    for table in (
            "meta", "dirs", "files", "symbols", "type_aliases",
            "type_members", "subsystems",
            "path_subsys", "dir_subsys", "source_includes",
            "translation_unit_roots", "calls"):
        conn.execute(
            f"CREATE TABLE {table} AS SELECT * FROM template.{table}")
    conn.execute("DETACH DATABASE template")
    conn.execute("UPDATE meta SET value='1' WHERE key='n_symbols'")
    conn.execute(
        "INSERT INTO symbols(id,file_id,name,kind,start_line,end_line,"
        " signature,summary,description,is_static,is_inline,is_exported,"
        " is_anonymous,parse_complete,parse_warnings,unmatched_member_docs,"
        " conditions)"
        " VALUES (1,999,'orphan','function',1,1,NULL,NULL,NULL,0,0,0,0,1,"
        " '[]','{}','[]')")
    conn.commit()

    with pytest.raises(db.SchemaError, match="dangling symbol file"):
        db.validate_schema(conn, deep=True)
    conn.close()


@pytest.mark.parametrize("table,key,error", [
    ("path_subsys", "ref_id", "file-ownership ranks disagree"),
    ("dir_subsys", "dir_id", "directory-ownership ranks disagree"),
])
def test_deep_validation_checks_the_meaning_of_ownership_ranks(
        mini_index, tmp_path, table, key, error):
    import shutil

    copied = tmp_path / f"bad-{table}-rank.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    identity = conn.execute(
        f"SELECT {key} FROM {table} GROUP BY {key} HAVING COUNT(*)>=2 LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        f"UPDATE {table} SET rank=CASE rank WHEN 0 THEN 1 WHEN 1 THEN 0 END"
        f" WHERE {key}=? AND rank IN (0,1)", (identity,))
    conn.commit()

    with pytest.raises(db.SchemaError, match=error):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_accepts_zero_subsystem_ids_and_signed_scores(
        mini_index):
    conn = db.connect(mini_index)
    assert conn.execute("SELECT 1 FROM subsystems WHERE id=0").fetchone()
    assert conn.execute("SELECT 1 FROM path_subsys WHERE score<0").fetchone()
    assert db.validate_schema(conn, deep=True)["kernel_version"] == "6.12.104"
    conn.close()


def test_deep_validation_rejects_extension_status_symbol_mismatch(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "bad-extension.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    conn.execute(
        "UPDATE files SET ext='.txt',index_status='indexed'"
        " WHERE id=(SELECT file_id FROM symbols LIMIT 1)")
    conn.commit()

    with pytest.raises(db.SchemaError, match="extension metadata"):
        db.validate_schema(conn, deep=True)
    conn.close()


def _source_include_index(tmp_path):
    conn = db.create(tmp_path / "source-include.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (1,'',NULL,'linux',0,2,2)")
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext,lines,index_status)"
        " VALUES (?,?,1,?,'.c',1,'parsed')",
        [(1, "parent.c", "parent.c"), (2, "member.c", "member.c")])
    conn.execute(
        "INSERT INTO source_includes(includer_id,included_id,line)"
        " VALUES (1,2,1)")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)", _metadata(n_files="2"))
    conn.commit()
    return conn


@pytest.mark.parametrize("corruption", ["line", "state"])
def test_deep_validation_checks_source_include_location_and_parse_state(
        tmp_path, corruption):
    conn = _source_include_index(tmp_path)
    assert db.validate_schema(conn, deep=True)["kernel_version"] == "9.9"
    if corruption == "line":
        conn.execute("UPDATE source_includes SET line=2")
    else:
        conn.execute(
            "UPDATE files SET index_status='read_error',index_error='gone'"
            " WHERE id=2")
        conn.execute("UPDATE meta SET value='1' WHERE key='n_parse_failed'")
    conn.commit()

    with pytest.raises(db.SchemaError, match="invalid C-source inclusion"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_rejects_source_include_cycles(tmp_path):
    conn = _source_include_index(tmp_path)
    conn.execute(
        "INSERT INTO source_includes(includer_id,included_id,line)"
        " VALUES (2,1,1)")
    conn.commit()

    with pytest.raises(db.SchemaError, match="inclusion graph contains a cycle"):
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
