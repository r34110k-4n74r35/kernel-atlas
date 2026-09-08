"""CLI call-site and bounded graph exploration contracts."""

import json
import shutil
import sqlite3

import pytest

from kernel_atlas.commands import cli


def test_calls_sites_show_invocation_evidence(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--sites",
                     "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "sites"
    site = next(site for site in result["sites"] if site["callee"] == "ext4_get_block")
    assert site["path"] == "fs/ext4/inode.c"
    assert site["line"] > result["target"]["line"]
    assert site["byte_offset"] >= 0
    assert site["resolution"] == "same_file"
    assert "runtime" in result["note"]
    assert cli.main(["--db", str(mini_index), "calls", "ext4_get_block", "--sites",
                     "--callers", "--color", "always"]) == 0
    output = capsys.readouterr().out
    assert "CALL SITE" in output
    assert "ext4_bmap" in output
    assert "\x1b[" in output


def test_calls_shortest_chain_and_graph_json(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--to",
                     "ext4_get_block", "--sites", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["found"] is True
    assert [node["name"] for node in result["path"]] == ["ext4_bmap", "ext4_get_block"]
    assert result["limits"]["depth"] == 8
    assert result["edges"][0]["sites"]
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--depth", "2",
                     "--sites", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "graph"
    assert result["limits"]["depth"] == 2
    assert result["nodes"][0]["name"] == "ext4_bmap"


def test_calls_path_table_and_missing_chain_are_careful(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--to",
                     "ext4_get_block", "--sites"]) == 0
    output = capsys.readouterr().out
    assert "Shortest resolved source-level chain" in output
    assert "CALL SITE" in output
    assert "not runtime execution paths" in output
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--to",
                     "tcp_sendmsg"]) == 0
    assert "No resolved chain found within the search limits" in capsys.readouterr().out


def test_graph_truncation_and_limit_control_are_explicit(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "calls", "ext4_bmap", "--depth", "2",
                     "--max-nodes", "1"]) == 0
    output = capsys.readouterr().out
    assert "Results truncated: node_limit" in output
    assert "Exploration boundary" in output


@pytest.mark.parametrize("options, message", [
    (["--sites", "--format", "names"], "support --format table or json"),
    (["--sites", "--columns", "name"], "does not support --columns"),
    (["--depth", "2", "--grep", "ext4"], "does not support --grep"),
    (["--depth", "2", "--limit", "10"], "use --max-nodes"),
    (["--depth", "2", "--sort", "path"], "does not support --sort"),
    (["--sites", "--with-subsystem"], "does not support --with-subsystem"),
    (["--sites", "--static-only"], "does not support --static-only"),
    (["--sites", "--kinds", "function"], "does not support --kinds"),
    (["--to", "tcp_sendmsg", "--callers"], "cannot be used with --callers"),
    (["--max-nodes", "12"], "requires --sites, --depth, or --to"),
])
def test_advanced_options_do_not_silently_ignore_filters(
        mini_index, capsys, options, message):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "ext4_bmap", *options])
    assert message in capsys.readouterr().err


def test_path_destination_requires_exact_callable_identity(mini_index, tmp_path, capsys):
    copied = tmp_path / "ambiguous-path.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    row = writer.execute(
        "SELECT file_id,name,kind,start_line,end_line FROM symbols "
        "WHERE name='ext4_get_block'").fetchone()
    writer.execute("INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
                   "VALUES(?,?,?,?,?)", (*row[:3], row[3] + 100, row[4] + 100))
    writer.commit()
    writer.close()
    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap", "--to", "ext4_get_block"])
    assert "path:line" in capsys.readouterr().err
    assert cli.main(["--db", str(copied), "calls", "ext4_bmap", "--to",
                     f"fs/ext4/inode.c:{row[3]}", "-f", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["found"] is True


def test_old_indexes_support_graphs_but_explain_missing_sites(mini_index, tmp_path, capsys):
    copied = tmp_path / "legacy-sites.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    writer.execute("DELETE FROM meta WHERE key='has_call_sites'")
    writer.execute("DROP TABLE call_sites")
    writer.commit()
    writer.close()
    assert cli.main(["--db", str(copied), "calls", "ext4_bmap", "--depth", "2",
                     "-f", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "graph"
    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap", "--sites"])
    error = capsys.readouterr().err
    assert "no individual call-site evidence" in error
    assert "--with-calls --force" in error


@pytest.mark.parametrize("options", [["--depth", "0"], ["--depth", "65"],
                                     ["--max-nodes", "0"], ["--max-nodes", "5001"]])
def test_exploration_bounds_are_checked_by_parser(capsys, options):
    with pytest.raises(SystemExit) as caught:
        cli.main(["calls", "function", *options])
    assert caught.value.code == 2
    assert "error" in capsys.readouterr().err.lower()
