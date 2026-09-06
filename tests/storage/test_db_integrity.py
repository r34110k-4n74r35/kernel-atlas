"""Deep integrity checks for types, references, ownership, and source inclusions."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import db

from .helpers import _identity_index, _metadata


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


def test_deep_validation_rejects_aliases_on_unsupported_symbol_kinds(
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

    with pytest.raises(
            db.SchemaError,
            match="type alias 'not_a_type'.*unsupported symbol kind 'function'"):
        db.validate_schema(conn, deep=True)
    conn.close()


def test_deep_validation_accepts_enum_typedef_aliases(mini_index, tmp_path):
    import shutil

    copied = tmp_path / "enum-alias.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    enum_id = conn.execute(
        "SELECT id FROM symbols WHERE kind='enum' AND name='rw_hint'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'rw_hint_t')",
        (enum_id,),
    )
    count = conn.execute("SELECT COUNT(*) FROM type_aliases").fetchone()[0]
    conn.execute(
        "UPDATE meta SET value=? WHERE key='n_type_aliases'", (str(count),)
    )
    conn.commit()

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
