"""Human browsing reports stay readable without altering machine output."""

import json

import pytest

from kernel_atlas import cli
from kernel_atlas.presentation import structure as structure_render
from kernel_atlas.presentation import terminal

from tests.support.terminal import Terminal, plain


@pytest.mark.parametrize("command", [
    ["info", "mm"],
    ["subsystems", "-n", "2"],
    ["subsystem", "EXT4 FILE SYSTEM", "--files"],
    ["subsystem", "file"],
    ["tree", "mm", "--files"],
    ["show", "fs/ext4/inode.c", "--lines", "1:4"],
    ["struct", "fs/ext4/super.c:ext4_sb_info"],
])
def test_browse_color_does_not_change_report_contents(mini_index, capsys, command):
    outputs = {}
    for choice in ("auto", "never", "always"):
        assert cli.main(["--db", str(mini_index), "--color", choice, *command]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        outputs[choice] = captured.out
    assert "\x1b[" in outputs["always"]
    assert "\x1b[" not in outputs["auto"] + outputs["never"]
    assert plain(outputs["always"]) == outputs["never"] == outputs["auto"]


@pytest.mark.parametrize("command", [
    ["info", "mm"],
    ["subsystems", "-n", "2"],
    ["subsystem", "EXT4 FILE SYSTEM", "--files"],
    ["subsystem", "file"],
    ["tree", "mm", "--files"],
    ["struct", "fs/ext4/super.c:ext4_sb_info"],
])
def test_browse_json_is_identical_when_color_is_forced(mini_index, capsys, command):
    payloads = []
    for choice in ("never", "always"):
        assert cli.main(["--db", str(mini_index), "--color", choice,
                         *command, "--format", "json"]) == 0
        captured = capsys.readouterr()
        assert "\x1b[" not in captured.out
        payloads.append(json.loads(captured.out))
    assert payloads[0] == payloads[1]


def test_raw_source_and_editor_path_ignore_forced_color(mini_index, mini_tree, capsys):
    path = mini_tree / "fs/ext4/inode.c"
    assert cli.main(["--db", str(mini_index), "--color", "always",
                     "path", "fs/ext4/inode.c"]) == 0
    assert capsys.readouterr().out == str(path) + "\n"
    assert cli.main(["--db", str(mini_index), "--color", "always",
                     "show", "fs/ext4/inode.c", "--bare"]) == 0
    assert capsys.readouterr().out == path.read_text()


@pytest.mark.parametrize("width", [8, 40, 79])
@pytest.mark.parametrize("command", [
    ["info", "mm"],
    ["subsystems", "-n", "2"],
    ["subsystem", "EXT4 FILE SYSTEM", "--files"],
])
def test_browse_reports_wrap_in_small_terminals(
        mini_index, monkeypatch, command, width):
    stream = Terminal()
    monkeypatch.setattr(terminal.sys, "stdout", stream)
    monkeypatch.setattr(terminal, "terminal_width", lambda stream: width)
    assert cli.main(["--db", str(mini_index), "--color", "always", *command]) == 0
    for line in plain(stream.getvalue()).splitlines():
        # Suggested shell commands intentionally remain intact for copying.
        if "Next:  " not in line and not line.startswith(f"         {cli.PROG} "):
            assert terminal.display_width(line) <= width, repr(line)


@pytest.mark.parametrize("width", [8, 40, 72])
def test_structure_width_includes_unicode_and_all_report_sections(width):
    detail = {
        "kind": "struct", "name": "配置", "c_name": "struct 配置",
        "path": "include/配置/" + "a" * 80 + ".h", "line": 12,
        "signature": "struct 配置 { int value; }", "parse_complete": False,
        "summary": "A structure containing \x1b[31mterminal codes.",
        "description": "Long source documentation " * 12,
        "warnings": ["A parse warning " * 12],
        "layout_limits": ["One source possibility " * 12],
        "related_documentation": [{"path": "Documentation/配置/" + "b" * 80}],
        "links": {"elixir": "https://example.test/" + "c" * 80},
        "unmatched_member_docs": {"value": "Unmatched docs " * 12},
        "members": [{
            "ordinal": 0, "name": "value" * 15, "kind": "field",
            "line": 13, "end_line": 13, "type": "unsigned int",
            "visibility": "unspecified", "declaration": "unsigned int value;",
            "description": "Stored contents " * 12,
            "description_source": "kernel-doc", "children": [],
        }],
    }
    colored = structure_render.render_structure(detail, color=True, max_width=width)
    output = plain(colored)
    assert "\x1b" not in output
    assert "?" in output
    assert "\x1b[33m" in colored
    assert all(terminal.display_width(line) <= width for line in output.splitlines())


def test_empty_subsystem_result_has_explicit_message(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "subsystems",
                     "--grep", "there-is-no-such-subsystem"]) == 0
    assert "No subsystems match" in capsys.readouterr().out


def test_structure_report_normalizes_source_whitespace_before_sanitizing():
    detail = {
        "kind": "struct", "name": "sample", "path": "include/my  sample.h",
        "line": 1, "summary": "A source\r\n\t documentation summary.",
        "description": "First\n\t paragraph.\n\nSecond\tparagraph.",
        "members": [{
            "ordinal": 0, "name": "value", "kind": "field", "line": 2,
            "end_line": 3, "visibility": "unspecified",
            "declaration": "unsigned\n\t int\tvalue;",
            "description": "Line one.\n\n\tLine two. \x1b[31m",
            "description_source": "kernel-doc", "children": [],
        }],
    }
    output = structure_render.render_structure(detail, color=False, max_width=200)
    assert "A source documentation summary." in output
    assert "First paragraph." in output
    assert "Second paragraph." in output
    assert "unsigned int value;" in output
    assert "Line one. Line two. ?[31m [kernel-doc]" in output
    assert "include/my  sample.h:1" in output
    assert "\x1b" not in output
    assert output.count("?") == 1
