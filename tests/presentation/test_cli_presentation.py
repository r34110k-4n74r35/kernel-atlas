"""Color is presentation only across the complete CLI output-format matrix."""

import io

import pytest

from kernel_atlas import cli, render
from kernel_atlas.presentation import terminal
from kernel_atlas.query import Entry

from tests.support.cli_matrix import COMMANDS, QUERY_ARGS, _formats
from tests.support.terminal import Terminal as TTY, plain


@pytest.mark.parametrize(
    ("command", "fmt"),
    [(command, fmt) for command in QUERY_ARGS for fmt in _formats(COMMANDS[command])],
)
def test_color_preserves_every_query_output_format(
        mini_index, capsys, monkeypatch, command, fmt):
    outputs = []
    for choice in ("never", "always"):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("ext4_bmap+0x1/0x20\n"))
        args = ["--db", str(mini_index), "--color", choice,
                command, *QUERY_ARGS[command]]
        if fmt is not None:
            args.extend(["-f", fmt])
        assert cli.main(args) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        outputs.append(captured.out)
    assert "\x1b[" not in outputs[0]
    assert plain(outputs[1]) == outputs[0]
    raw = fmt in {"json", "csv", "plain", "names"} or command in {"path", "show"}
    assert ("\x1b[" in outputs[1]) is not raw


@pytest.mark.parametrize("args,exit_code", [
    (["--help"], 0),
    (["ls", "--help"], 0),
    (["ls", "--unknown-option"], 2),
    (["ls", "--limit", "invalid"], 2),
    (["stats"], 1),
])
def test_help_and_errors_honor_color_without_changing_text(capsys, args, exit_code):
    outputs = []
    for choice in ("never", "always"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--color", choice, *args])
        assert exc.value.code == exit_code
        result = capsys.readouterr()
        outputs.append(result.out if exit_code == 0 else result.err)
    assert "\x1b[" not in outputs[0]
    assert "\x1b[" in outputs[1]
    assert plain(outputs[1]) == outputs[0]


@pytest.mark.parametrize("choice", ["auto", "never", "always"])
@pytest.mark.parametrize("preference", [None, "NO_COLOR", "TERM"])
def test_query_auto_color_and_terminal_preferences(
        mini_index, monkeypatch, choice, preference):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    if preference == "NO_COLOR":
        monkeypatch.setenv("NO_COLOR", "")
    elif preference == "TERM":
        monkeypatch.setenv("TERM", "dumb")
    stdout = TTY()
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    assert cli.main(["--db", str(mini_index), "--color", choice, "ls", "fs"]) == 0
    expected = choice == "always" or (choice == "auto" and preference is None)
    assert ("\x1b[" in stdout.getvalue()) == expected


def test_diagnostic_stream_color_is_independent_and_resets(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    stdout, stderr = io.StringIO(), TTY()
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.setattr(cli.sys, "stderr", stderr)
    with pytest.raises(SystemExit):
        cli.main(["--color", "auto", "stats"])
    assert "\x1b[" in stderr.getvalue()
    with pytest.raises(SystemExit):
        cli.main(["--color", "always", "stats"])
    assert not terminal.color_enabled(stdout)


@pytest.mark.parametrize("width", [1, 7, 25, 79])
def test_table_wraps_wide_text_without_losing_data(width):
    value = "文件" * 12
    entries = [Entry(kind="file", name=value, path="目录/" + value, n_symbols=123)]
    output = render.render_table(entries, ["name", "path", "symbols"], True, width)
    report = plain(output)
    assert all(terminal.display_width(line) <= width for line in report.splitlines())
    if width > 1:
        assert report.count("文") == 24
        assert report.count("件") == 24
    assert "123" in report.replace("\n", "").replace(" ", "")


def test_heading_preserves_intentional_line_breaks():
    stream = io.StringIO()
    terminal.Console("never", stream).heading("Listing\n  with details\n")
    assert stream.getvalue() == "Listing\n  with details\n\n"


def test_human_table_escapes_controls_but_machine_data_remains_exact():
    value = "hello\x1b[31m\tworld"
    entry = Entry(kind="file", name=value, path=value)
    output = render.render_table([entry], ["name"], False)
    assert "\x1b" not in output and "\t" not in output
    assert render.render_names([entry]) == value + "\n"


def test_redirected_tables_keep_long_fields(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(render.sys, "stdout", stream)
    value = "long_" * 80
    entry = Entry(kind="function", name="fn", path="a.c", signature=value)
    output = render.render_table([entry], ["name", "signature"], False,
                                 render.term_width())
    assert value in output


def test_color_does_not_change_padding_with_trailing_empty_cells():
    args = (["A", "B", "C"], [["abc", "", ""], ["", "", ""]])
    options = {"tones": {0: "success", 1: "info", 2: "accent"}}
    uncolored = terminal.table_text(*args, color=False, **options)
    colored = terminal.table_text(*args, color=True, **options)
    assert plain(colored) == uncolored
    assert all(line == line.rstrip() for line in plain(colored).splitlines())
