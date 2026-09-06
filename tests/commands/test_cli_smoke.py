"""Exercise the CLI process boundary, command registry, and output formats."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kernel_atlas
from kernel_atlas.commands import cli
from kernel_atlas.storage import kernelsrc

from tests.support.kernel_tree import make_mini_kernel
from tests.support.cli_matrix import COMMANDS, LIFECYCLE_COMMANDS, QUERY_ARGS, _formats


@pytest.fixture
def run_cli(tmp_path):
    # Preserve the implementation under test even when running from an sdist
    # with a different editable installation in the interpreter's environment.
    env = dict(
        os.environ,
        KERNEL_ATLAS_HOME=str(tmp_path / "home"),
        PYTHONPATH=str(Path(kernel_atlas.__file__).resolve().parents[1]),
        NO_COLOR="1",
    )

    def run(*args, stdin="", expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "kernel_atlas", "--color", "never", *args],
            cwd=tmp_path, env=env, input=stdin, text=True,
            capture_output=True, timeout=30,
        )
        assert result.returncode == expected, (
            args, result.returncode, result.stdout, result.stderr,
        )
        assert "Traceback (most recent call last)" not in result.stderr
        return result

    return run


def test_smoke_matrix_covers_every_registered_command():
    assert set(COMMANDS) == set(QUERY_ARGS) | LIFECYCLE_COMMANDS


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_help_exits_successfully(run_cli, command):
    assert "usage:" in run_cli(command, "--help").stdout


@pytest.mark.parametrize(
    ("command", "fmt"),
    [(name, fmt) for name in QUERY_ARGS for fmt in _formats(COMMANDS[name])],
    ids=lambda value: str(value),
)
def test_query_commands_in_every_advertised_format(run_cli, mini_index, command, fmt):
    args = ["--db", str(mini_index), command, *QUERY_ARGS[command]]
    if fmt is not None:
        args += ["-f", fmt]
    result = run_cli(*args, stdin="ext4_bmap+0x1/0x20\n")
    assert result.stdout.strip()
    if fmt == "json":
        assert isinstance(json.loads(result.stdout), (dict, list))
    if command == "path":
        filename, _, line = result.stdout.strip().rpartition(":")
        assert Path(filename).is_file() and int(line) > 0
    elif command == "show":
        assert "ext4_get_block(NULL, block)" in result.stdout


def test_local_build_selection_and_removal_roundtrip(run_cli, tmp_path):
    tree = make_mini_kernel(tmp_path / "linux")
    assert json.loads(run_cli("indexes", "-f", "json").stdout) == []
    assert "no indexes yet" in run_cli("indexes").stdout
    run_cli("build", "--src", str(tree), "--with-calls", "--jobs", "2", "--quiet")
    run_cli("build", "6.12.105", "--src", str(tree), "--jobs", "1", "--quiet")
    assert len(json.loads(run_cli("indexes", "-f", "json").stdout)) == 2
    assert "6.12.104" in run_cli("use", "6.12.104").stdout
    assert "6.12.104" in run_cli("use").stdout
    assert json.loads(run_cli("check", "-f", "json").stdout)
    versions = json.loads(run_cli("locate", "ext4_bmap", "-f", "json").stdout)
    assert {row["version"] for row in versions} == {"6.12.104", "6.12.105"}
    assert versions[0]["active"] and versions[0]["version"] == "6.12.104"
    missing_calls = run_cli("--kernel", "6.12.105", "calls", "ext4_bmap", expected=1)
    assert "--with-calls" in missing_calls.stderr
    local_web = run_cli("web", "ext4_bmap", expected=1)
    assert "no upstream release-reference URLs" in local_web.stderr
    run_cli("use", "--clear")
    run_cli("rm", "6.12.105")
    run_cli("remove", "6.12.104")
    assert json.loads(run_cli("indexes", "-f", "json").stdout) == []
    run_cli("stats", expected=1)
    assert (tree / "MAINTAINERS").is_file()


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_versions_with_offline_release_feed(monkeypatch, capsys, fmt):
    # Network integration is checked separately; keep the regression suite offline.
    feed = json.dumps({"releases": [{
        "version": "6.12.104", "moniker": "longterm",
        "released": {"isodate": "2026-01-01"},
        "source": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.104.tar.xz",
    }]}).encode()
    monkeypatch.setattr(kernelsrc, "_get", lambda *args, **kwargs: feed)
    assert cli.main(["versions", "-f", fmt]) == 0
    output = capsys.readouterr().out
    if fmt == "json":
        assert json.loads(output)[0]["version"] == "6.12.104"
    else:
        assert "6.12.104" in output and "longterm" in output
