"""Build reports remain readable, copyable, and honor CLI color choices."""

import io
import os
import shlex
import unicodedata
import warnings

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.indexing import indexer
from kernel_atlas.storage import kernelsrc
from kernel_atlas.presentation import build as build_output
from kernel_atlas.presentation import terminal
from kernel_atlas.commands.lifecycle import _source_warning_style

from tests.support.terminal import Terminal, plain


@pytest.mark.parametrize("choice,expected", [("auto", False), ("never", False),
                                            ("always", True)])
def test_build_color_applies_to_both_output_streams(
        mini_tree, tmp_path, capsys, monkeypatch, choice, expected):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli.main([
        "build", "--src", str(mini_tree), "--output", str(tmp_path / "study.db"),
        "--jobs", "1", "--with-calls", "--color", choice,
    ]) == 0
    captured = capsys.readouterr()
    assert ("\x1b[" in captured.out) == expected
    assert ("\x1b[" in captured.err) == expected
    assert "\r" not in captured.out + captured.err
    for heading in ("Built index for Linux", "Contents", "Call graph", "Try next"):
        assert heading in plain(captured.out)
    assert "Kernel Atlas | Linux" in plain(captured.err)
    assert "Parsing sources" not in captured.out


@pytest.mark.parametrize("stdout_tty", [False, True])
def test_auto_color_uses_the_destination_stream(monkeypatch, stdout_tty):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    stdout = Terminal() if stdout_tty else io.StringIO()
    stderr = io.StringIO() if stdout_tty else Terminal()
    monkeypatch.setattr(build_output.sys, "stdout", stdout)
    monkeypatch.setattr(build_output.sys, "stderr", stderr)
    with build_output.color_mode("auto"):
        build_output.header("9.9", "kernels/linux-9.9", "indexes/9.9.db",
                            calls=False, workers=4)
        build_output.summary("9.9", "indexes/9.9.db", indexer.BuildStats(),
                             size=1024, calls=False, query_cmd="ka -K 9.9")
    assert ("\x1b[" in stdout.getvalue()) == stdout_tty
    assert ("\x1b[" in stderr.getvalue()) == (not stdout_tty)


@pytest.mark.parametrize("variable,value", [("NO_COLOR", ""), ("TERM", "dumb")])
def test_auto_color_respects_terminal_preferences(monkeypatch, variable, value):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv(variable, value)
    stream = Terminal()
    assert not build_output.color_enabled(stream, "auto")
    assert build_output.color_enabled(stream, "always")
    assert not build_output.color_enabled(stream, "never")


def test_color_scope_is_reset_after_failure():
    stream = io.StringIO()
    with pytest.raises(RuntimeError):
        with build_output.color_mode("always"):
            assert build_output.color_enabled(stream)
            raise RuntimeError("failed build")
    assert not build_output.color_enabled(stream)


def test_quiet_build_retains_copyable_quoted_commands(mini_tree, tmp_path, capsys):
    output = tmp_path / "my  study's index.db"
    assert cli.main([
        "build", "--src", str(mini_tree), "--output", str(output),
        "--jobs", "1", "--quiet", "--color", "never",
    ]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"{cli.PROG} --db {shlex.quote(str(output))} info mm" in captured.out
    assert "Call graph" not in captured.out


def test_summary_preserves_counts_and_highlights_parse_failures(capsys):
    stats = indexer.BuildStats(
        dirs=1234, files=123456, parsed=100000, symbols=1234567, subsystems=42,
        skipped=9, oversize=2, failed=3, symlinks=7, calls=0, seconds=3661,
    )
    with build_output.color_mode("always"):
        build_output.summary("9.9", "indexes/9.9.db", stats,
                             size=2 * 1024**3, calls=True, query_cmd="ka -K 9.9")
    output = capsys.readouterr().out
    text = plain(output)
    for value in ("1,234", "123,456", "1,234,567", "2.0 GiB", "1:01:01",
                  "9 inputs (2 oversized)", "3 inputs", "7 recorded"):
        assert value in text
    assert "Call graph" in text and "Records" in text
    assert "Parsing notes" in text and "\x1b[31m" in output


def test_report_shortens_paths_beneath_the_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with build_output.color_mode("never"):
        build_output.header("9.9", tmp_path / "kernels/linux-9.9",
                            tmp_path / "indexes/9.9.db", calls=False, workers=1)
    text = capsys.readouterr().err
    assert "kernels/linux-9.9" in text and "indexes/9.9.db" in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize("width", [8, 20, 40])
def test_header_wraps_wide_paths_to_terminal_columns(tmp_path, monkeypatch, width):
    monkeypatch.chdir(tmp_path)
    stream = Terminal()
    monkeypatch.setattr(build_output.sys, "stderr", stream)
    monkeypatch.setattr(terminal.shutil, "get_terminal_size",
                        lambda **kwargs: os.terminal_size((width, 80)))
    with build_output.color_mode("always"):
        build_output.header("9.9", tmp_path / ("资料" * 15),
                            tmp_path / ("输出" * 15 + ".db"), calls=False, workers=2)
    output = plain(stream.getvalue())
    for line in output.splitlines():
        columns = sum(0 if unicodedata.combining(c) else
                      2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line)
        assert columns <= width - 1
    assert "资料" * 15 in "".join(output.split())
    assert "输出" * 15 + ".db" in "".join(output.split())


def test_control_characters_in_paths_do_not_become_terminal_commands(capsys):
    with build_output.color_mode("never"):
        build_output.header("9.9", "source\x1b[2J\nname", "index\r.db",
                            calls=False, workers=1)
    text = capsys.readouterr().err
    assert "\x1b" not in text and "\r" not in text
    assert "source?[2J?name" in text


def test_rc_warning_is_readable_and_restores_the_warning_handler(capsys):
    original = warnings.showwarning
    with build_output.color_mode("always"), _source_warning_style():
        warnings.simplefilter("always", kernelsrc.UnverifiedRCWarning)
        warnings.warn("RC archive has no published checksum", kernelsrc.UnverifiedRCWarning)
    report = capsys.readouterr().err
    assert "Warning" in report and "no published checksum" in report
    assert "\x1b[33m" in report
    assert "test_build_output.py" not in report and "warnings.warn" not in report
    assert warnings.showwarning is original


def test_source_warning_style_preserves_other_warning_categories():
    with pytest.warns(UserWarning, match="other library warning"):
        with _source_warning_style():
            warnings.warn("other library warning", UserWarning)
