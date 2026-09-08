"""Bounded traversal, exact call identities, and occurrence locations."""

import sqlite3

import pytest

from kernel_atlas.queries import call_graph


@pytest.fixture
def graph():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);
        CREATE TABLE symbols(id INTEGER PRIMARY KEY, name TEXT, kind TEXT,
                             file_id INTEGER, start_line INTEGER);
        CREATE TABLE calls(caller_id INTEGER, callee TEXT, callee_id INTEGER,
                           resolution TEXT, direct_count INTEGER,
                           indirect_count INTEGER, macro_count INTEGER);
        CREATE TABLE call_sites(caller_id INTEGER, callee TEXT, kind TEXT,
                                line INTEGER, byte_offset INTEGER);
        INSERT INTO files VALUES (1,'one.c'),(2,'two.c');
        INSERT INTO symbols VALUES (1,'start','function',1,1),
          (2,'left','function',1,10),(3,'right','function',1,20),
          (4,'end','function',1,30),(5,'end','function',2,1),
          (6,'deep','function',1,40);
        INSERT INTO calls VALUES
          (1,'left',2,'same_file',1,0,0),
          (1,'right',3,'same_file',1,0,0),
          (2,'deep',6,'same_file',1,0,0),
          (3,'end',4,'same_file',2,1,0),
          (6,'end',4,'same_file',1,0,0),
          (4,'start',1,'same_file',1,0,0),
          (1,'unknown',NULL,'ambiguous',1,0,0);
        INSERT INTO call_sites VALUES
          (1,'left','direct',2,12),(1,'right','direct',3,25),
          (1,'unknown','direct',4,38),(3,'end','direct',21,200),
          (3,'end','direct',22,215),(3,'end','indirect',23,230);
    """)
    yield conn
    conn.close()


def test_shortest_path_uses_id_and_breadth_first_search(graph):
    result = call_graph.explore(graph, 1, to_id=4, depth=5)
    assert result["found"] is True
    assert [node["name"] for node in result["path"]] == ["start", "right", "end"]
    assert result["path"][-1]["id"] == 4
    assert result["truncated"] is False
    # Same name in a different file must never be used as the destination.
    assert call_graph.explore(graph, 1, to_id=5, depth=5)["found"] is False


def test_cycles_and_ambiguous_boundary_are_explicit(graph):
    result = call_graph.explore(graph, 1, depth=5)
    assert len(result["nodes"]) == 5
    assert result["cycles_detected"] is True
    assert result["truncated"] is False
    unresolved = next(edge for edge in result["boundary"] if edge["name"] == "unknown")
    assert unresolved["reason"] == "ambiguous"
    assert unresolved["callee"] is None
    mixed = next(edge for edge in result["boundary"]
                 if edge["reason"] == "non_direct_occurrences")
    assert mixed["callee"] is None and mixed["indirect_count"] == 1


def test_converging_paths_are_not_reported_as_cycles(graph):
    graph.execute("DELETE FROM calls WHERE caller_id=4")
    assert call_graph.explore(graph, 1, depth=5)["cycles_detected"] is False


def test_depth_and_node_limits_report_omitted_reachable_nodes(graph):
    shallow = call_graph.explore(graph, 1, depth=1)
    assert {node["id"] for node in shallow["nodes"]} == {1, 2, 3}
    assert shallow["truncation_reasons"] == ["depth_limit"]
    assert any(edge["reason"] == "depth_limit" for edge in shallow["boundary"])
    small = call_graph.explore(graph, 1, depth=5, max_nodes=2)
    assert len(small["nodes"]) == 2
    assert small["truncation_reasons"] == ["node_limit"]
    assert small["boundary"][-1]["reason"] == "node_limit"


def test_leaf_at_requested_depth_is_complete(graph):
    graph.execute("DELETE FROM calls WHERE caller_id != 1")
    result = call_graph.explore(graph, 1, depth=1)
    assert result["truncated"] is False


def test_incoming_traverses_resolved_edges_and_retains_arrow_direction(graph):
    result = call_graph.explore(graph, 4, incoming=True, depth=1)
    assert {node["id"] for node in result["nodes"]} == {3, 4, 6}
    assert {(edge["caller"]["id"], edge["callee"]["id"])
            for edge in result["edges"]} == {(3, 4), (6, 4)}


def test_sites_use_invocation_lines_and_do_not_assign_indirect_identity(graph):
    result = call_graph.call_sites(graph, 3)
    assert [site["line"] for site in result["sites"]] == [21, 22, 23]
    assert [site["byte_offset"] for site in result["sites"]] == [200, 215, 230]
    assert result["sites"][-1]["callee_id"] is None
    assert result["sites"][-1]["resolution"] == "indirect"
    assert [site["line"] for site in call_graph.call_sites(
        graph, 4, incoming=True)["sites"]] == [21, 22]
    assert call_graph.call_sites(graph, 5, incoming=True)["sites"] == []


def test_site_truncation_and_graph_site_evidence(graph):
    result = call_graph.call_sites(graph, 1, limit=2)
    assert result["truncation_reasons"] == ["site_limit"]
    assert len(result["sites"]) == 2
    result = call_graph.explore(graph, 1, to_id=4, sites=True)
    resolved = next(edge for edge in result["edges"] if edge["name"] == "right")
    assert resolved["sites"][0]["line"] == 3
    mixed = next(edge for edge in result["edges"] if edge["name"] == "end")
    assert mixed["sites"][-1]["callee_id"] is None


def test_edge_limit_bounds_unresolved_fan_out(graph):
    graph.execute("DELETE FROM calls")
    graph.executemany("INSERT INTO calls VALUES(1,?,NULL,'unresolved',1,0,0)",
                      [(f"unknown_{index}",) for index in range(30)])
    result = call_graph.explore(graph, 1, max_nodes=2)
    assert len(result["boundary"]) == 20
    assert result["truncation_reasons"] == ["edge_limit"]


def test_sites_have_separate_budget_when_one_edge_has_many_occurrences(graph):
    graph.execute("DELETE FROM calls")
    graph.execute("INSERT INTO calls VALUES(1,'left',2,'same_file',50,0,0)")
    graph.execute("DELETE FROM call_sites")
    graph.executemany("INSERT INTO call_sites VALUES(1,'left','direct',?,?)",
                      [(index + 2, index * 10) for index in range(50)])
    result = call_graph.explore(graph, 1, max_nodes=2, sites=True)
    assert len(result["edges"][0]["sites"]) == 20
    assert result["truncation_reasons"] == ["site_limit"]


def test_zero_hop_path_and_invalid_bounds(graph):
    result = call_graph.explore(graph, 1, to_id=1)
    assert result["found"] and [node["id"] for node in result["path"]] == [1]
    for kwargs in ({"depth": 0}, {"depth": 65}, {"max_nodes": 0},
                   {"max_nodes": 5001}, {"incoming": True, "to_id": 4}):
        with pytest.raises(ValueError):
            call_graph.explore(graph, 1, **kwargs)
