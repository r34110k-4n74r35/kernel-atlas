"""Subsystem, relationship, documentation, link, and cross-index commands."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas.commands import cli


def test_web_and_docs_commands(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "web", "tcp_sendmsg",
                     "--url", "elixir"]) == 0
    url = capsys.readouterr().out.strip()
    assert "elixir.bootlin.com" in url and "tcp.c" in url

    assert cli.main(["--db", str(mini_index), "docs", "fs/ext4"]) == 0
    out = capsys.readouterr().out
    assert "Documentation/filesystems/ext4/about.rst" in out

    assert cli.main(["--db", str(mini_index), "docs", "mm"]) == 0
    out = capsys.readouterr().out
    assert "Documentation/mm/page_alloc.rst" in out
    assert "using mm/" not in out


def test_web_rejects_links_for_a_custom_source_index(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "custom-source.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute(
        "UPDATE meta SET value=? WHERE key='source'",
        (str(tmp_path / "vendor-linux"),),
    )
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(copied), "web", "tcp_sendmsg", "--url", "elixir",
        ])
    error = capsys.readouterr().err
    assert "no upstream release-reference URLs" in error
    assert "use 'path' or 'show'" in error
    assert "Traceback" not in error

    for command in (
            ["info", "tcp_sendmsg"],
            ["struct", "ext4_sb_info"],
            ["docs", "mm"]):
        assert cli.main(["--db", str(copied), *command]) == 0
        output = capsys.readouterr().out
        assert f"{cli.PROG} --db {copied.resolve()} web " not in output


def test_docs_bare_name_picks_the_area_directory_not_a_symbol(mini_index, capsys):
    """`mm` is both the top-level directory and arch/x86/mm/."""
    from kernel_atlas.commands.cli import _resolve_area
    from kernel_atlas.storage import db
    conn = db.connect(mini_index, readonly=True)
    t = _resolve_area(conn, "mm").target
    conn.close()
    assert t.kind == "dir" and t.path == "mm"


def test_subsystem_json_omits_files_unless_asked(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "files" not in data
    assert data["index"] == "6.12.104"
    assert data["directories"]
    assert data["directories"][0]["primary_files"] > 0
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json", "--files"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(p.endswith("inode.c") for p in data["files"])

    assert cli.main(["--db", str(mini_index), "subsystems", "-n", "1",
                     "-f", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["index"] == "6.12.104"


def test_relationships_reports_identity_aware_call_flow(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "relationships", "fs/ext4",
        "--include-internal", "--direction", "outgoing", "-f", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["subsystem"]["name"] == "EXT4 FILE SYSTEM"
    assert payload["subsystem"]["primary_files"] > 0
    assert payload["call_graph_available"] is True
    assert payload["outgoing_call_resolution"]["total"] >= 3
    assert payload["outgoing_call_resolution"]["resolved"] >= 3
    assert payload["call_flows"] == [{
        "direction": "outgoing",
        "subsystem": "EXT4 FILE SYSTEM",
        "edges": 3,
        "callers": 3,
        "callees": 3,
        "source_files": 2,
        "target_files": 2,
        "internal": True,
        "unclassified": False,
    }]


def test_relationships_csv_is_a_stable_machine_view(mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "rels", "EXT4 FILE SYSTEM",
        "--include-internal", "--direction", "outgoing", "--via", "calls",
        "-f", "csv",
    ]) == 0
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert rows[0]["relationship"] == "call"
    assert rows[0]["selected_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["source_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["target_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["edges"] == "3"


def test_relationships_rejects_ambiguous_and_mixed_targets(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "super.c"])
    assert "ambiguous" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs"])
    assert "mixed ownership" in capsys.readouterr().err


def test_relationships_rejects_options_without_effect(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs/ext4",
                  "--via", "ownership", "--direction", "incoming"])
    assert "apply only to call" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs/ext4",
                  "--via", "calls", "--min-shared", "2"])
    assert "applies only to ownership" in capsys.readouterr().err


def test_locate_lists_every_built_index(mini_index, tmp_path, monkeypatch, capsys):
    import json
    import shutil
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    d = tmp_path / "indexes"
    d.mkdir()
    shutil.copy(mini_index, d / "6.12.104.db")
    shutil.copy(mini_index, d / "7.2.db")
    renamed = sqlite3.connect(d / "7.2.db")
    renamed.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    renamed.commit()
    renamed.close()
    assert cli.main(["locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    versions = {r["version"] for r in rows}
    assert versions == {"6.12.104", "7.2"}
    assert all(r["found"] and r["path"].endswith("tcp.c") for r in rows)
    # No pin: the highest built version is the default and is listed first.
    assert rows[0]["version"] == "7.2" and rows[0]["active"]
    assert rows[1]["version"] == "6.12.104" and not rows[1]["active"]


def test_locate_does_not_turn_a_failed_line_selector_into_a_file(
        mini_index, capsys):
    import json

    target = "fs/ext4/inode.c:9999"
    assert cli.main([
        "--db", str(mini_index), "locate", target, "--format", "json",
    ]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["found"] is False
    assert "no symbol spans line 9999" in row["note"]

    assert cli.main(["--db", str(mini_index), "locate", target]) == 0
    output = capsys.readouterr().out
    assert "no symbol spans line 9999" in output


def test_locate_table_exposes_ambiguous_resolution_notes(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "locate", "super.c"]) == 0
    output = capsys.readouterr().out
    assert "fs/ext4/super.c" in output
    assert "2 files named 'super.c'" in output


def test_subsystem_ambiguity_is_valid_json(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "subsystem", "SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ambiguous"] is True
    assert len(data["matches"]) > 1
    assert data["index"] == "6.12.104"


def test_casefold_colliding_subsystem_names_are_never_chosen_arbitrarily(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "casefold.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    next_id = writer.execute("SELECT MAX(id)+1 FROM subsystems").fetchone()[0]
    writer.executemany("INSERT INTO subsystems(id,name) VALUES (?,?)", [
        (next_id, "FOO"), (next_id + 1, "foo")])
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "subsystem", "FoO",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ambiguous"] is True
    assert {row["name"] for row in data["matches"]} == {"FOO", "foo"}

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "relationships", "FoO"])
    assert "ambiguous under case-insensitive" in capsys.readouterr().err


def test_subsystem_no_match_hint_preserves_selector_and_shell_quotes(
        mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "subsystem", "not here"])
    error = capsys.readouterr().err
    assert f"--db {mini_index.resolve()}" in error
    assert "--grep 'not here'" in error


def test_relationships_accepts_ambiguous_definitions_with_one_owner(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "same-owner.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_bmap'"
    ).fetchone()
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], row[3] + 100, row[4] + 100, *row[5:]))
    conn.commit()
    conn.close()

    assert cli.main(["--db", str(copied), "relationships", "ext4_bmap",
                     "--via", "ownership", "-f", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subsystem"]["name"] == "EXT4 FILE SYSTEM"
    assert "all 2 matches" in payload["resolved_from"]
