from __future__ import annotations

from kernel_atlas import db, relationships


def _relationship_db(tmp_path):
    conn = db.create(tmp_path / "relationships.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0), (2, "a", 1, "a", 1),
         (3, "b", 1, "b", 1), (4, "c", 1, "c", 1)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name) VALUES (?,?,?,?)",
        [(1, "a/one.c", 2, "one.c"), (2, "a/two.c", 2, "two.c"),
         (3, "b/three.c", 3, "three.c"), (4, "c/four.c", 4, "four.c")],
    )
    conn.executemany(
        "INSERT INTO subsystems(id,name,n_files,n_primary_files) VALUES (?,?,?,?)",
        [(1, "SUBSYSTEM A", 3, 2), (2, "SUBSYSTEM B", 2, 1),
         (3, "SUBSYSTEM C", 1, 1), (4, "THE REST", 4, 0)],
    )
    conn.executemany(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES (?,?,?,?,?,?)",
        [
            ("file", 1, 1, 10, 0, 1), ("file", 1, 4, -1000, 1, 0),
            ("file", 2, 1, 10, 0, 1), ("file", 2, 2, 5, 1, 0),
            ("file", 2, 4, -1000, 2, 0),
            ("file", 3, 2, 10, 0, 1), ("file", 3, 1, 5, 1, 0),
            ("file", 3, 4, -1000, 2, 0),
            ("file", 4, 3, 10, 0, 1), ("file", 4, 4, -1000, 1, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind) VALUES (?,?,?,?)",
        [(1, 1, "a_one", "function"), (2, 3, "b_three", "function"),
         (3, 2, "a_two", "function"), (4, 4, "c_four", "function"),
         (5, 3, "b_call", "function"), (6, 1, "a_target", "function")],
    )
    conn.executemany(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution) VALUES (?,?,?,?)",
        [
            (1, "b_three", 2, "unique_global"),
            (3, "c_four", 4, "unique_global"),
            (5, "a_target", 6, "unique_global"),
            (1, "a_two", 3, "same_file"),
            (1, "unknown", None, "unresolved"),
        ],
    )
    conn.commit()
    return conn


def test_ownership_overlap_reports_both_coverages_and_jaccard(tmp_path):
    conn = _relationship_db(tmp_path)
    rows = relationships.ownership_overlaps(conn, 1)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.subsystem == "SUBSYSTEM B"
    assert (row.shared_files, row.selected_files, row.other_files) == (2, 3, 2)
    assert row.selected_coverage == 2 / 3
    assert row.other_coverage == 1.0
    assert row.jaccard == 2 / 3


def test_call_flow_uses_resolved_identities_and_primary_owners(tmp_path):
    conn = _relationship_db(tmp_path)
    rows = relationships.call_flows(conn, 1, direction="both")
    coverage = relationships.call_resolution_coverage(conn, 1)
    conn.close()

    assert [(r.direction, r.subsystem, r.edges) for r in rows] == [
        ("outgoing", "SUBSYSTEM B", 1),
        ("outgoing", "SUBSYSTEM C", 1),
        ("incoming", "SUBSYSTEM B", 1),
    ]
    assert coverage == {
        "same_file": 1,
        "included_source": 0,
        "unique_global": 2,
        "ambiguous": 0,
        "macro": 0,
        "indirect": 0,
        "unresolved": 1,
        "resolved": 3,
        "total": 4,
    }


def test_call_flow_can_include_internal_edges(tmp_path):
    conn = _relationship_db(tmp_path)
    rows = relationships.call_flows(
        conn, 1, direction="outgoing", include_internal=True)
    conn.close()

    internal = next(row for row in rows if row.internal)
    assert (internal.subsystem, internal.edges) == ("SUBSYSTEM A", 1)


def test_call_flow_labels_catch_all_endpoints_as_unclassified(tmp_path):
    conn = _relationship_db(tmp_path)
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (5,'u',1,'u',1)")
    conn.execute(
        "INSERT INTO files(id,path,dir_id,name) VALUES (5,'u/five.c',5,'five.c')")
    conn.execute(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary) "
        "VALUES ('file',5,4,-1000,0,1)")
    conn.execute(
        "INSERT INTO symbols(id,file_id,name,kind) "
        "VALUES (7,5,'unclassified_target','function')")
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution) "
        "VALUES (1,'unclassified_target',7,'unique_global')")
    conn.commit()

    rows = relationships.call_flows(conn, 1, direction="outgoing")
    conn.close()

    unclassified = next(row for row in rows if row.unclassified)
    assert unclassified.subsystem is None
    assert unclassified.edges == 1


def test_call_flow_keeps_endpoints_with_no_ownership_row(tmp_path):
    conn = _relationship_db(tmp_path)
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (5,'u',1,'u',1)")
    conn.execute(
        "INSERT INTO files(id,path,dir_id,name) VALUES (5,'u/five.c',5,'five.c')")
    conn.execute(
        "INSERT INTO symbols(id,file_id,name,kind)"
        " VALUES (7,5,'unowned_target','function')")
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (1,'unowned_target',7,'unique_global')")
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (7,'a_target',6,'unique_global')")
    conn.commit()

    outgoing = relationships.call_flows(conn, 1, direction="outgoing")
    incoming = relationships.call_flows(conn, 1, direction="incoming")
    conn.close()

    unclassified = next(row for row in outgoing if row.unclassified)
    assert unclassified.subsystem is None
    assert unclassified.edges == 1
    unclassified = next(row for row in incoming if row.unclassified)
    assert unclassified.subsystem is None
    assert unclassified.edges == 1


def test_catch_all_internal_and_unowned_flows_remain_distinct(tmp_path):
    conn = _relationship_db(tmp_path)
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (5,'u',1,'u',1)")
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name) VALUES (?,?,5,?)",
        [(5, "u/caller.c", "caller.c"),
         (6, "u/internal.c", "internal.c"),
         (7, "u/unowned.c", "unowned.c")])
    conn.executemany(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES ('file',?,4,-1000,0,1)", [(5,), (6,)])
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind) VALUES (?,?,?,'function')",
        [(7, 5, "rest_caller"), (8, 6, "rest_target"),
         (9, 7, "unowned_target")])
    conn.executemany(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (7,?,?,'unique_global')",
        [("rest_target", 8), ("unowned_target", 9)])
    conn.commit()

    rows = relationships.call_flows(
        conn, 4, direction="outgoing", include_internal=True)
    conn.close()

    assert len(rows) == 2
    assert all(row.unclassified and row.subsystem is None and row.edges == 1
               for row in rows)
    assert {row.internal for row in rows} == {False, True}


def test_call_flow_does_not_invent_boundaries_between_coowners(tmp_path):
    conn = _relationship_db(tmp_path)
    conn.execute(
        "UPDATE path_subsys SET score=10,is_primary=1 "
        "WHERE ref_kind='file' AND ref_id=2 AND subsystem_id=2")
    conn.execute(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES ('file',1,2,10,2,1)")
    conn.commit()

    outgoing = relationships.call_flows(conn, 1, direction="outgoing")
    internal = relationships.call_flows(
        conn, 1, direction="outgoing", include_internal=True)
    conn.close()

    # The same-file a_one -> a_two call and the A/B co-owned a_one -> b_three
    # call both share a primary owner.  Neither is evidence of an A -> B edge.
    assert all(row.subsystem != "SUBSYSTEM B" for row in outgoing)
    own = next(row for row in internal if row.internal)
    assert own.subsystem == "SUBSYSTEM A"
    assert own.edges == 1
