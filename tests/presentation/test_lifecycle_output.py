"""Lifecycle reports stay readable while machine formats and actions stay stable."""

import io
import json
import os
import re
import shutil
from types import SimpleNamespace

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.storage import config, db, kernelsrc
from kernel_atlas.presentation import terminal
from kernel_atlas.commands import lifecycle

from tests.support.terminal import Terminal, plain


@pytest.fixture
def local_index(mini_index):
    """Only copies in pytest's isolated application home are selected/removed."""
    path = config.index_path("6.12.104")
    path.parent.mkdir(parents=True)
    shutil.copyfile(mini_index, path)
    return path


@pytest.fixture
def releases(monkeypatch):
    values = [
        kernelsrc.Release("longterm", "6.12.104", None, "2026-08-01"),
        kernelsrc.Release("stable", "6.18.45", None, None),
    ]
    monkeypatch.setattr(kernelsrc, "list_releases", lambda: values)
    return values


@pytest.mark.parametrize("choice,colored", [("auto", False), ("never", False),
                                          ("always", True)])
@pytest.mark.parametrize("command,arguments,expected", [
    ("versions", [], "Current kernel.org releases"),
    ("indexes", [], "Built indexes"),
    ("use", ["6.12.104"], "Default index is now 6.12.104"),
    ("remove", ["6.12.104"], "Removal summary"),
    ("stats", [], "Symbols by kind"),
    ("check", [], "index check passed"),
])
def test_lifecycle_commands_honor_color_and_keep_reports_plain_in_pipes(
        local_index, releases, capsys, choice, colored, command, arguments, expected):
    assert cli.main([command, *arguments, "--color", choice]) == 0
    captured = capsys.readouterr()
    assert expected in plain(captured.out)
    assert ("\x1b[" in captured.out) is colored
    assert "\r" not in captured.out
    assert captured.err == ""
    if command == "use":
        assert config.get_default_version() == "6.12.104"
        assert f"{cli.PROG} use --clear" in plain(captured.out)
    elif command == "remove":
        assert not local_index.exists()


@pytest.mark.parametrize("command", ["versions", "indexes", "stats", "check"])
def test_lifecycle_json_is_identical_regardless_of_forced_color(
        local_index, releases, capsys, command):
    results = []
    for choice in ("never", "always"):
        assert cli.main([command, "--format", "json", "--color", choice]) == 0
        captured = capsys.readouterr()
        assert "\x1b" not in captured.out
        assert captured.err == ""
        results.append(json.loads(captured.out))
    assert results[0] == results[1]


def test_removal_errors_use_stderr_color_and_preserve_the_index(
        local_index, monkeypatch):
    stdout, stderr = io.StringIO(), Terminal()
    monkeypatch.setattr(lifecycle.sys, "stdout", stdout)
    monkeypatch.setattr(lifecycle.sys, "stderr", stderr)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")

    def blocked(path):
        raise PermissionError("index is busy")

    monkeypatch.setattr(cli, "_unlink_index", blocked)
    with pytest.raises(SystemExit):
        cli.main(["remove", "6.12.104", "--color", "auto"])
    assert local_index.exists()
    assert "\x1b" not in stdout.getvalue()
    assert "\x1b[31m" in stderr.getvalue()
    assert "could not remove index" in plain(stderr.getvalue())
    assert "index is busy" in plain(stderr.getvalue())


def test_release_table_handles_wide_characters_and_terminal_controls(
        releases, monkeypatch):
    releases[0].moniker = "长期支持" * 5
    releases[1].version = "6.18.45\x1b[2J"
    output = Terminal()
    monkeypatch.setattr(lifecycle.sys, "stdout", output)
    monkeypatch.setattr(terminal.shutil, "get_terminal_size",
                        lambda **kwargs: os.terminal_size((30, 24)))
    assert cli.main(["versions", "--color", "never"]) == 0
    text = output.getvalue()
    assert "\x1b" not in text
    assert "6.18.45?[2J" in text
    assert "长期支持" * 5 in "".join(text.split())
    for line in text.splitlines():
        if "Next:" not in line:
            assert terminal.display_width(line) <= 29
    assert f"{cli.PROG} build lts" in text


def test_stats_groups_parse_outcomes_and_marks_failures(conn, capsys):
    meta = db.validate_schema(conn)
    meta.update(n_parse_skipped="4", n_parse_failed="2", n_oversize="1")
    support = SimpleNamespace(open_index=lambda args: (conn, meta), _linux=cli._linux)
    lifecycle.cmd_stats(SimpleNamespace(format="text", color="always"), support)
    output = capsys.readouterr().out
    text = plain(output)
    for heading in ("Contents", "Parsing", "Call graph", "Symbols by kind",
                    "Largest top-level areas"):
        assert heading in text
    assert re.search(r"Skipped\s+4", text)
    assert re.search(r"Failed\s+2", text)
    assert "\x1b[33m4\x1b[0m" in output
    assert "\x1b[31m2\x1b[0m" in output


def test_index_listing_distinguishes_default_and_broken_entries(
        local_index, capsys):
    broken = local_index.parent / "broken.db"
    broken.write_bytes(b"not a database")
    config.set_default_version("6.12.104")
    assert cli.main(["indexes", "--color", "always"]) == 0
    output = capsys.readouterr().out
    text = plain(output)
    assert "*" in text and "6.12.104" in text
    assert "broken unusable:" in text
    assert "\x1b[32m" in output and "\x1b[31m" in output
    assert "Calls" in text and "Source" in text


def test_small_index_sizes_are_visible_in_listing_and_removal(local_index, capsys):
    size = local_index.stat().st_size
    assert 1024 <= size < 1024**2
    expected = f"{size / 1024:,.1f} KiB"
    assert cli.main(["indexes", "--color", "never"]) == 0
    listing = capsys.readouterr().out
    assert expected in listing
    assert "0 MB" not in listing

    # Machine output retains the pre-existing size string and its units.
    assert cli.main(["indexes", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["size"] == f"{size / 1048576:.0f} MB"

    assert cli.main(["remove", "6.12.104", "--color", "never"]) == 0
    removed = capsys.readouterr().out
    assert f"({expected})" in removed
    assert f"{expected} of index files" in removed
    assert "0 MB" not in removed


@pytest.mark.parametrize("size,expected", [
    (0, "0 B"), (1023, "1,023 B"), (1024, "1.0 KiB"),
    (1024**2, "1.0 MiB"), (1024**3, "1.0 GiB"), (1024**4, "1.0 TiB"),
    (1234 * 1024**4, "1,234.0 TiB"),
])
def test_readable_size_units_cover_boundaries(size, expected):
    assert terminal.format_size(size) == expected
