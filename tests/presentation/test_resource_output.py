"""Resource and relationship reports stay readable and keep machine contracts."""

import csv
import io
import json
import os
import shlex

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.presentation import terminal

from tests.support.terminal import Terminal, plain

REPORTS = [
    (["web", "tcp_sendmsg"], ("Linux 6.12.104", "elixir", "github")),
    (["docs", "mm", "--explain"], ("Documentation related to mm", "Next:")),
    (["locate", "tcp_sendmsg"], ("VERSION", "LOCATION / STATUS", "SUBSYSTEM")),
    (["trace", "tcp_sendmsg", "absent_frame"],
     ("Backtrace", "FUNCTION", "not in index", "Areas touched")),
    (["calls", "ext4_bmap"], ("Functions called by", "ext4_get_block")),
    (["relationships", "EXT4 FILE SYSTEM", "--include-internal"],
     ("Ownership overlap", "Direct C invocation flow", "Outgoing resolution")),
]


@pytest.mark.parametrize("command,expected", REPORTS)
def test_resource_reports_honor_color_without_changing_values(
        mini_index, capsys, command, expected):
    outputs = {}
    for color in ("auto", "never", "always"):
        assert cli.main(["--db", str(mini_index), "--color", color, *command]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert ("\x1b[" in captured.out) == (color == "always")
        outputs[color] = plain(captured.out)
        for value in expected:
            assert value in outputs[color]
    assert outputs["auto"] == outputs["never"] == outputs["always"]


@pytest.mark.parametrize("command,_expected", REPORTS)
def test_resource_json_stays_undecorated_with_forced_color(
        mini_index, capsys, command, _expected):
    results = []
    for color in ("never", "always"):
        assert cli.main(["--db", str(mini_index), "--color", color,
                         *command, "-f", "json"]) == 0
        output = capsys.readouterr().out
        assert "\x1b" not in output
        results.append(json.loads(output))
    assert results[0] == results[1]


def test_relationship_csv_retains_values_with_forced_color(mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "--color", "always", "relationships",
        "EXT4 FILE SYSTEM", "--include-internal", "--direction", "outgoing",
        "--via", "calls", "-f", "csv",
    ]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    rows = list(csv.DictReader(io.StringIO(output)))
    assert rows[0]["relationship"] == "call"
    assert rows[0]["edges"] == "3"
    assert rows[0]["subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[-1]["relationship"] == "call_resolution"


def test_web_urls_and_docs_next_commands_remain_copyable(
        mini_index, capsys, monkeypatch):
    assert cli.main(["--db", str(mini_index), "web", "tcp_sendmsg",
                     "-f", "json"]) == 0
    links = json.loads(capsys.readouterr().out)["links"]
    stream = Terminal()
    monkeypatch.setattr(terminal.sys, "stdout", stream)
    monkeypatch.setattr(terminal.shutil, "get_terminal_size",
                        lambda **kwargs: os.terminal_size((30, 50)))
    assert cli.main(["--db", str(mini_index), "--color", "always", "web",
                     "tcp_sendmsg"]) == 0
    output = plain(stream.getvalue())
    for url in links.values():
        assert url in output

    stream.seek(0)
    stream.truncate()
    assert cli.main(["--db", str(mini_index), "--color", "always", "web",
                     "tcp_sendmsg", "--url", "elixir"]) == 0
    assert stream.getvalue() == links["elixir"] + "\n"

    stream.seek(0)
    stream.truncate()
    assert cli.main(["--db", str(mini_index), "--color", "always", "docs", "mm"]) == 0
    output = plain(stream.getvalue())
    assert (f"{cli.PROG} --db {shlex.quote(str(mini_index.resolve()))} "
            "web Documentation/mm/page_alloc.rst") in output


@pytest.mark.parametrize("command", [
    ["locate", "super.c"],
    ["trace", "tcp_sendmsg", "missing_frame"],
    ["relationships", "EXT4 FILE SYSTEM", "--include-internal"],
])
def test_resource_tables_fit_narrow_terminals(mini_index, monkeypatch, command):
    stream = Terminal()
    monkeypatch.setattr(terminal.sys, "stdout", stream)
    monkeypatch.setattr(terminal.shutil, "get_terminal_size",
                        lambda **kwargs: os.terminal_size((32, 80)))
    assert cli.main(["--db", str(mini_index), "--color", "always", *command]) == 0
    lines = plain(stream.getvalue()).splitlines()
    assert lines
    assert all(terminal.display_width(line) <= 31 for line in lines)
