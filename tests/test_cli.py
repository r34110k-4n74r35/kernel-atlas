"""Tests for index selection: `use`, `remove`, and default resolution."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import cli, config


def _fake_index(root, version: str) -> None:
    d = root / "indexes"
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / f"{version}.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("kernel_version", version),
        ("n_files", "1"),
        ("n_symbols", "0"),
        ("has_calls", "0"),
        ("built_at", "2026-01-01T00:00:00"),
    ])
    conn.commit()
    conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    _fake_index(tmp_path, "6.18.45")
    _fake_index(tmp_path, "7.2")
    return tmp_path


def test_use_pins_a_version_and_accepts_a_unique_prefix(home, capsys):
    assert config.get_default_version() is None
    assert cli.main(["use", "6.18"]) == 0
    assert config.get_default_version() == "6.18.45"
    out = capsys.readouterr().out
    assert "6.18.45" in out

    assert cli.main(["use"]) == 0
    out = capsys.readouterr().out
    assert "pinned: 6.18.45" in out
    assert "active index: 6.18.45" in out


def test_use_clear_and_both_args_rejected(home, capsys):
    cli.main(["use", "7.2"])
    assert cli.main(["use", "--clear"]) == 0
    assert config.get_default_version() is None
    assert "cleared pin on 7.2" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["use", "7.2", "--clear"])


def test_use_rejects_unknown_and_ambiguous_versions(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["use", "5.15"])
    assert "no index" in capsys.readouterr().err
    _fake_index(home, "6.12.104")
    with pytest.raises(SystemExit):
        cli.main(["use", "6"])
    assert "ambiguous" in capsys.readouterr().err


def test_default_index_is_the_pin_then_the_highest_version(home):
    assert cli.default_index().stem == "7.2"
    config.set_default_version("6.18.45")
    assert cli.default_index().stem == "6.18.45"
    (home / "indexes" / "6.18.45.db").unlink()
    # Stale pin falls back, and warn=False stays quiet.
    assert cli.default_index(warn=False).stem == "7.2"


def test_remove_deletes_the_index_and_clears_a_matching_pin(home, capsys):
    config.set_default_version("6.18.45")
    (home / "kernels" / "linux-6.18.45").mkdir(parents=True)
    (home / "kernels" / "linux-6.18.45" / "MAINTAINERS").write_text("x")

    assert cli.main(["remove", "6.18"]) == 0
    out = capsys.readouterr().out
    assert not (home / "indexes" / "6.18.45.db").is_file()
    assert (home / "kernels" / "linux-6.18.45").is_dir()
    assert "pinned default" in out
    assert "source kept" in out
    assert config.get_default_version() is None


def test_remove_with_source_and_duplicate_specs(home, capsys):
    (home / "kernels" / "linux-7.2").mkdir(parents=True)
    (home / "kernels" / "linux-7.2" / "MAINTAINERS").write_text("x")
    # Prefix + exact must not fail on the second name after the first delete.
    assert cli.main(["remove", "7.2", "7.2", "--source"]) == 0
    assert not (home / "indexes" / "7.2.db").is_file()
    assert not (home / "kernels" / "linux-7.2").exists()
    assert "removed source" in capsys.readouterr().out


def test_indexes_marks_the_default(home, capsys):
    config.set_default_version("6.18.45")
    assert cli.main(["indexes"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "6.18.45" in ln]
    assert lines and lines[0].lstrip().startswith("*")


def test_pin_roundtrip_in_config(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert config.get_default_version() is None
    config.set_default_version("6.1")
    assert config.get_default_version() == "6.1"
    config.clear_default_version()
    assert config.get_default_version() is None


def test_negative_limit_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["siblings", "mm", "-n", "-1"])
    err = capsys.readouterr().err
    assert ">= 0" in err or "invalid" in err.lower()


def test_info_omits_the_rest_and_includes_links(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "info", "mm", "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [s["name"] for s in data["subsystems"]]
    assert "THE REST" not in names
    assert "MEMORY MANAGEMENT" in names
    assert "elixir.bootlin.com" in data["links"]["elixir"]


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


def test_docs_bare_name_picks_the_area_directory_not_a_symbol(mini_index, capsys):
    """`mm` is both the top-level directory and arch/x86/mm/."""
    from kernel_atlas.cli import _resolve_area
    from kernel_atlas import db
    conn = db.connect(mini_index, readonly=True)
    t = _resolve_area(conn, "mm").target
    conn.close()
    assert t.kind == "dir" and t.path == "mm"


def test_tree_files_stay_at_requested_depth(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "tree", "fs", "-d", "1",
                     "--files", "-f", "json"]) == 0
    paths = {e["path"] for e in json.loads(capsys.readouterr().out)}
    assert "fs/open.c" in paths
    assert "fs/ext4" in paths
    assert "fs/ext4/inode.c" not in paths, "depth 1 must not include grandchildren files"


def test_tree_of_a_top_level_file_uses_the_kernel_root(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "tree", "Makefile", "-d", "1",
                     "-f", "json"]) == 0
    paths = {e["path"] for e in json.loads(capsys.readouterr().out)}
    assert "mm" in paths and "fs" in paths
    assert "Makefile" not in paths


def test_subsystem_json_omits_files_unless_asked(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "files" not in data
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json", "--files"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(p.endswith("inode.c") for p in data["files"])


def test_locate_lists_every_built_index(mini_index, tmp_path, monkeypatch, capsys):
    import json
    import shutil
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    d = tmp_path / "indexes"
    d.mkdir()
    shutil.copy(mini_index, d / "6.12.104.db")
    shutil.copy(mini_index, d / "7.2.db")
    assert cli.main(["locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    versions = {r["version"] for r in rows}
    assert versions == {"6.12.104", "7.2"}
    assert all(r["found"] and r["path"].endswith("tcp.c") for r in rows)
