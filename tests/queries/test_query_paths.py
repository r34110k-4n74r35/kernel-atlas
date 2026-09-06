"""Literal subtree boundaries and limits, independent of filesystem casing."""

import json
from contextlib import closing

import pytest

from kernel_atlas import cli, db, query


@pytest.fixture
def path_index(mini_index, tmp_path):
    out = tmp_path / "paths.db"
    with closing(db.connect(mini_index)) as source, closing(
        db.connect(out, readonly=False)
    ) as conn:
        source.backup(conn)
        root_id = conn.execute("SELECT id FROM dirs WHERE path=''").fetchone()[0]
        conn.execute("UPDATE dirs SET name='000-root' WHERE id=?", (root_id,))
        for path in ("Area", "area", "a[bc]*?_%", "abcXYZ"):
            cursor = conn.execute(
                "INSERT INTO dirs(path,parent_id,name,depth) VALUES (?,?,?,1)",
                (path, root_id, path),
            )
            directory = cursor.lastrowid
            cursor = conn.execute(
                "INSERT INTO files(path,dir_id,name,ext) VALUES (?,?,?,'.c')",
                (f"{path}/unit.c", directory, "unit.c"),
            )
            conn.execute(
                "INSERT INTO symbols(file_id,name,kind,start_line,end_line)"
                " VALUES (?,'unit','function',1,1)", (cursor.lastrowid,),
            )
            conn.execute(
                "INSERT INTO dirs(path,parent_id,name,depth) VALUES (?,?,?,2)",
                (f"{path}/child", directory, "child"),
            )
        conn.commit()
    return out


@pytest.mark.parametrize("base", ["Area", "area", "a[bc]*?_%"])
def test_subtree_paths_are_case_sensitive_and_literal(path_index, base):
    with closing(db.connect(path_index)) as conn:
        target = query.resolve(conn, base).target
        scope = query.build_scope(conn, target, "subtree")
        entries = query.collect(conn, scope, query.ALL_KINDS)
        assert {(e.kind, e.path) for e in entries} == {
            ("dir", base), ("dir", f"{base}/child"),
            ("file", f"{base}/unit.c"), ("function", f"{base}/unit.c"),
        }
        assert query.directory_unclaimed_files(conn, base) == 1


@pytest.mark.parametrize("sort", ["name", "path", "kind", "line", "size", "lines"])
def test_hidden_root_does_not_consume_a_directory_limit(path_index, sort):
    with closing(db.connect(path_index)) as conn:
        scope = query.build_scope(conn, query.resolve(conn, ".").target, "subtree")
        all_entries = query.collect(conn, scope, ("dir",), sort=sort)
        assert all_entries
        assert query.collect(conn, scope, ("dir",), sort=sort, limit=1) == all_entries[:1]


@pytest.mark.parametrize("base", ["Area", "area", "a[bc]*?_%"])
def test_tree_command_stays_in_literal_subtree(path_index, base, capsys):
    assert cli.main([
        "--db", str(path_index), "tree", base, "--files", "-f", "json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["path"] for row in rows} == {f"{base}/unit.c", f"{base}/child"}
