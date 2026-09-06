"""Literal subtree boundaries and limits, independent of filesystem casing."""

from contextlib import closing

import pytest

from kernel_atlas.storage import db
from kernel_atlas.queries import query


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
