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
        ("source", f"https://cdn.kernel.org/linux-{version}.tar.xz"),
        ("tree_path", str(root / "kernels" / f"linux-{version}")),
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


def test_index_selection_options_are_unambiguous(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", "one.db", "--kernel", "6.12", "stats"])
    assert "mutually exclusive" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["indexes", "--db", "ignored.db"])
    assert "does not apply" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [
    b"{",
    b"[]",
    b'{"releases":[null]}',
])
def test_versions_turns_a_malformed_release_feed_into_one_line_error(
        monkeypatch, capsys, payload):
    from kernel_atlas import kernelsrc

    monkeypatch.setattr(kernelsrc, "_get", lambda *args, **kwargs: payload)
    with pytest.raises(SystemExit):
        cli.main(["versions"])
    assert "could not reach kernel.org" in capsys.readouterr().err


def test_use_rejects_unknown_and_ambiguous_versions(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["use", "5.15"])
    assert "no index" in capsys.readouterr().err
    _fake_index(home, "6.12.104")
    with pytest.raises(SystemExit):
        cli.main(["use", "6"])
    assert "ambiguous" in capsys.readouterr().err


def test_version_prefix_is_component_aware():
    assert cli.version_prefix_match("6.18.45", "6.18")
    assert cli.version_prefix_match("6.18.45", "6")
    assert cli.version_prefix_match("6.18.45", "6.18.45")
    assert not cli.version_prefix_match("6.18.45", "6.1")
    assert not cli.version_prefix_match("6.18.45", "6.18.4")
    assert cli.version_prefix_match("7.2", "7")
    assert cli.version_prefix_match("next-20260101", "next")
    assert not cli.version_prefix_match("6.18.45", "")


def test_use_6_1_does_not_select_6_18(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["use", "6.1"])
    assert "no index" in capsys.readouterr().err


def test_default_index_is_the_pin_then_the_highest_version(home):
    assert cli.default_index().stem == "7.2"
    config.set_default_version("6.18.45")
    assert cli.default_index().stem == "6.18.45"
    (home / "indexes" / "6.18.45.db").unlink()
    # Stale pin falls back, and warn=False stays quiet.
    assert cli.default_index(warn=False).stem == "7.2"


def test_default_index_sorts_valid_aliases_by_recorded_kernel_version(
        mini_index, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    high_alias = indexes / "9.0.db"
    learning_alias = indexes / "learning.db"
    shutil.copy(mini_index, high_alias)
    shutil.copy(mini_index, learning_alias)
    conn = sqlite3.connect(learning_alias)
    conn.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    conn.commit()
    conn.close()

    assert cli.default_index(warn=False) == learning_alias


def test_default_index_prefers_a_usable_index_over_a_higher_corrupt_alias(
        mini_index, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    usable = indexes / "7.2.db"
    shutil.copy(mini_index, usable)
    conn = sqlite3.connect(usable)
    conn.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    conn.commit()
    conn.close()
    (indexes / "999.db").write_bytes(b"not a sqlite database")

    assert cli.default_index(warn=False) == usable


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


def test_remove_source_uses_recorded_version_not_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    alias_index = indexes / "alias.db"
    shutil.copy(mini_index, alias_index)
    managed = tmp_path / "kernels" / "linux-6.12.104"
    managed.mkdir(parents=True)
    unrelated = tmp_path / "kernels" / "linux-alias"
    unrelated.mkdir()
    conn = sqlite3.connect(alias_index)
    conn.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(managed),))
    conn.commit()
    conn.close()

    assert cli.main(["remove", "alias", "--source"]) == 0
    assert not alias_index.exists()
    assert not managed.exists()
    assert unrelated.is_dir()
    assert "removed source" in capsys.readouterr().out


def test_remove_rejects_path_shaped_versions_without_deleting(home, capsys):
    before = set((home / "indexes").iterdir())
    with pytest.raises(SystemExit):
        cli.main(["remove", "../7.2"])
    assert "unsafe kernel version" in capsys.readouterr().err
    assert set((home / "indexes").iterdir()) == before


def test_indexes_marks_the_default(home, capsys):
    config.set_default_version("6.18.45")
    assert cli.main(["indexes"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "6.18.45" in ln]
    assert lines and lines[0].lstrip().startswith("*")


def test_indexes_reports_metadata_version_separately_from_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys):
    import json
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    shutil.copy(mini_index, indexes / "learning.db")

    assert cli.main(["indexes", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104"
    assert rows[0]["alias"] == "learning"


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
    assert "is_static" not in data["target"]
    assert "is_inline" not in data["target"]
    assert "is_exported" not in data["target"]

    assert cli.main(["--db", str(mini_index), "info", "__alloc_pages",
                     "-f", "json"]) == 0
    symbol = json.loads(capsys.readouterr().out)["target"]
    assert symbol["is_static"] is False
    assert symbol["is_inline"] is False
    assert symbol["is_exported"] is True


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


def test_pin_selects_index_source_tree_and_locate_home(
        mini_index, mini_tree, tmp_path, monkeypatch, capsys):
    """Index selection changes URLs, but source lines use the recorded tree.

    The copies keep the fixture's meta.kernel_version (6.12.104); the filename
    is only a selection alias.  Links, headers, and source lines continue to
    describe the kernel version and exact tree recorded inside the index.
    """
    import json
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    (tmp_path / "indexes").mkdir()
    shutil.copy(mini_index, tmp_path / "indexes" / "6.18.45.db")
    shutil.copy(mini_index, tmp_path / "indexes" / "7.2.db")
    for ver, tag in (("6.18.45", "PINNED618"), ("7.2", "OTHER72")):
        dest = tmp_path / "kernels" / f"linux-{ver}"
        shutil.copytree(mini_tree, dest)
        tcp = dest / "net" / "ipv4" / "tcp.c"
        tcp.write_text(tcp.read_text(encoding="utf-8").replace(
            "return 0;", f"return 0; /* {tag} */", 1), encoding="utf-8")
    config.set_default_version("6.18.45")

    assert cli.main(["web", "tcp_sendmsg", "--url", "elixir"]) == 0
    url = capsys.readouterr().out
    assert "v6.12.104" in url
    assert "v7.2" not in url and "v6.18.45" not in url

    assert cli.main(["docs", "mm", "-f", "json"]) == 0
    docs = json.loads(capsys.readouterr().out)
    assert docs[0]["index"] == "6.12.104"
    assert "v6.12.104" in docs[0]["elixir"]

    assert cli.main(["show", "tcp_sendmsg", "--bare"]) == 0
    shown = capsys.readouterr().out
    assert "return 0;" in shown
    assert "PINNED618" not in shown and "OTHER72" not in shown

    assert cli.main(["path", "tcp_sendmsg"]) == 0
    assert str(mini_tree) in capsys.readouterr().out

    assert cli.main(["info", "tcp_sendmsg", "-f", "json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["index"] == "6.12.104"
    assert "v6.12.104" in info["links"]["elixir"]

    assert cli.main(["locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104" and rows[0]["active"]
    assert rows[1]["version"] == "6.12.104" and not rows[1]["active"]

    assert cli.main(["-K", "7.2", "show", "tcp_sendmsg", "--bare"]) == 0
    shown = capsys.readouterr().out
    assert "return 0;" in shown
    assert "PINNED618" not in shown and "OTHER72" not in shown

    assert cli.main(["-K", "7.2", "locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104" and rows[0]["active"]


def test_ls_json_includes_index_version(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "ls", "mm", "-f", "json", "-n", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data and data[0]["index"] == "6.12.104"


def test_tree_wide_symbol_listing_requires_a_limit(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "siblings", "mm",
                  "--level", "tree", "--kinds", "function"])
    assert "-n" in capsys.readouterr().err
    assert cli.main(["--db", str(mini_index), "siblings", "mm",
                     "--level", "tree", "--kinds", "function", "-n", "5"]) == 0


def test_include_self_is_in_addition_to_the_sibling_limit(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "siblings",
        "fs/ext4/inode.c:ext4_bmap", "--include-self", "-n", "2", "-f", "json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3, "-n 2 means two other rows plus the included target"
    targets = [r for r in rows if r.get("is_target")]
    assert len(targets) == 1 and targets[0]["name"] == "ext4_bmap"


def test_include_self_survives_filters_that_exclude_the_target(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "siblings",
        "fs/ext4/inode.c:ext4_bmap", "--include-self", "-n", "1",
        "--static-only", "-f", "json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert [r["name"] for r in rows if r.get("is_target")] == ["ext4_bmap"]
    assert len([r for r in rows if not r.get("is_target")]) == 1


def test_catch_all_subsystem_scope_requires_a_limit(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "siblings", "Makefile",
                  "--level", "subsystem"])
    err = capsys.readouterr().err
    assert "THE REST" in err and "-n" in err
    assert cli.main(["--db", str(mini_index), "siblings", "Makefile",
                     "--level", "subsystem", "-n", "1", "-f", "names"]) == 0


def test_info_json_honours_zero_list_limits(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "info", "super.c", "-f", "json",
                     "--max-subsystems", "0", "--max-candidates", "0"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["subsystems"] == []
    assert data["other_candidates"] == []
    assert data["n_other_candidates"] >= 1
    assert data["n_subsystems"] >= 1


def test_subsystem_ambiguity_is_valid_json(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "subsystem", "SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ambiguous"] is True
    assert len(data["matches"]) > 1
    assert data["index"] == "6.12.104"


def test_listing_json_columns_shape_each_row(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "ls", "mm", "--kinds", "file",
                     "-n", "1", "-f", "json", "-c", "name,subdirs"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row.keys() == {"name", "subdirs", "index"}
    assert row["subdirs"] is None

    assert cli.main(["--db", str(mini_index), "ls", "mm", "--kinds", "file",
                     "-n", "1", "-f", "json", "-c", "name", "-S"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row.keys() == {"name", "subsystem", "index"}
    assert row["subsystem"] == "MEMORY MANAGEMENT"


def test_empty_columns_is_a_clear_error(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "mm", "-c", ","])
    assert "at least one column" in capsys.readouterr().err


def test_mutually_exclusive_flags_are_rejected_by_parser(capsys):
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--exact", "--glob"])
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--static-only", "--no-static"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_calls_rejects_non_function_targets(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "S_IRWXU"])
    assert "not a function or syscall" in capsys.readouterr().err


def test_trace_does_not_treat_macros_as_stack_frames(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "trace", "S_IRWXU", "tcp_sendmsg",
                     "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["frame"] == "S_IRWXU" and rows[0]["found"] is False
    assert rows[1]["frame"] == "tcp_sendmsg" and rows[1]["found"] is True


def test_linkage_filters_on_path_only_listings_are_rejected(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "mm", "--exported"])
    assert "only applies to symbols" in capsys.readouterr().err


def test_path_and_show_reject_target_inapplicable_flags(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "path", "mm/page_alloc.c", "--line"])
    assert "only applies to symbols" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "show", "__alloc_pages", "--lines", "1"])
    assert "applies to files" in capsys.readouterr().err


@pytest.mark.parametrize("line_range", ["0", "9" * 5000])
def test_show_rejects_invalid_numeric_line_ranges(
        mini_index, line_range, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), "show", "Makefile", "--lines", line_range,
        ])
    assert "--lines" in capsys.readouterr().err


def test_find_and_calls_filter_before_applying_limit(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "many.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    file_id = conn.execute(
        "SELECT id FROM files WHERE path='fs/ext4/inode.c'").fetchone()[0]
    caller_id = conn.execute(
        "SELECT id FROM symbols WHERE name='ext4_bmap'").fetchone()[0]
    symbols = [
        (file_id, f"needle_{i:02d}", "function", 100 + i, 100 + i,
         None, 0, 0, 0)
        for i in range(25)
    ]
    symbols.append((file_id, "needle_zz_keep", "function", 200, 200,
                    None, 1, 0, 0))
    conn.executemany(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        "is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)", symbols)
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(caller_id, row[1]) for row in symbols],
    )
    conn.commit()
    conn.close()

    assert cli.main(["--db", str(copied), "find", "needle", "--static-only",
                     "-n", "1", "-f", "names"]) == 0
    assert capsys.readouterr().out.strip() == "needle_zz_keep"

    assert cli.main(["--db", str(copied), "calls", "ext4_bmap",
                     "--grep", "^needle_zz_keep$", "-n", "1", "-f", "names"]) == 0
    assert capsys.readouterr().out.strip() == "needle_zz_keep"


def test_siblings_uses_symbol_ids_for_same_name_same_line(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "same-location.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    file_id = conn.execute(
        "SELECT id FROM files WHERE path='include/linux/fs.h'").fetchone()[0]
    conn.executemany(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        "is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (file_id, "same_word", "union", 500, 510, None, 0, 0, 0),
            (file_id, "same_word", "typedef", 500, 510, None, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    from kernel_atlas import db, query
    reader = db.connect(copied, readonly=True)
    by_line = query.resolve(reader, "include/linux/fs.h:500")
    reader.close()
    assert by_line.target is not None and len(by_line.candidates) == 1

    args = ["--db", str(copied), "siblings", "include/linux/fs.h:same_word",
            "--kinds", "types", "-f", "json"]
    assert cli.main(args) == 0
    rows = json.loads(capsys.readouterr().out)
    same = [r for r in rows if r["name"] == "same_word"]
    assert len(same) == 1, "only the exact resolved symbol is self"

    assert cli.main(args + ["--include-self"]) == 0
    rows = json.loads(capsys.readouterr().out)
    same = [r for r in rows if r["name"] == "same_word"]
    assert len(same) == 2
    assert len([r for r in same if r.get("is_target")]) == 1


def test_version_sort_places_final_after_rc_and_sorts_rc_numerically():
    from pathlib import Path

    versions = [Path("7.2-rc10.db"), Path("7.2.db"), Path("7.2-rc2.db")]
    got = [p.stem for p in sorted(versions, key=cli._version_key)]
    assert got == ["7.2-rc2", "7.2-rc10", "7.2"]


def test_version_sort_uses_numeric_base_for_vendor_versions():
    from pathlib import Path

    versions = [Path("6.1.db"), Path("6.6.12-acme+debug.db")]
    assert max(versions, key=cli._version_key).stem == "6.6.12-acme+debug"

    same_base = [Path("6.6.12.db"), Path("6.6.12-acme.db"),
                 Path("6.6.12-rc2.db")]
    assert [p.stem for p in sorted(same_base, key=cli._version_key)] == [
        "6.6.12-rc2", "6.6.12-acme", "6.6.12",
    ]


def test_local_build_rejects_an_output_inside_the_source_tree(
        mini_tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit):
        cli.main(["build", "--src", str(mini_tree),
                  "--output", str(mini_tree / "atlas.db"), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
    assert not (mini_tree / "atlas.db").exists()


def test_local_build_rejects_download_alias_as_a_literal_version(
        mini_tree, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "build", "lts", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "does not apply with --src" in capsys.readouterr().err


def test_local_build_requires_detectable_or_explicit_version(tmp_path, capsys):
    tree = tmp_path / "kernel-tree"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text("TEST\nF: *\n")
    (tree / "Makefile").write_text("not a kernel version\n")

    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "could not detect a kernel version" in capsys.readouterr().err


def test_build_reports_expected_indexer_failures_without_a_traceback(
        mini_tree, tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("cannot scan protected directory")

    monkeypatch.setattr(cli.indexer, "build", fail)
    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    err = capsys.readouterr().err
    assert "could not build index: cannot scan protected directory" in err
    assert "Traceback" not in err


def test_output_containment_checks_the_symlink_entry_not_its_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output = source / "atlas.db"
    try:
        output.symlink_to(tmp_path / "outside.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert cli._path_inside(output, source)


def test_custom_build_output_is_not_blocked_by_the_managed_index(
        mini_tree, tmp_path, monkeypatch, capsys):
    from kernel_atlas import indexer, kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    _fake_index(home, "6.12.104")
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release("longterm", "6.12.104", source_url, None),
    )
    seen = {}

    def fake_source(version, **kwargs):
        seen["source_url"] = kwargs["source_url"]
        return mini_tree

    def fake_build(tree, out, version, **kwargs):
        seen["metadata_source"] = kwargs["source"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "ensure_source", fake_source)
    monkeypatch.setattr(cli.indexer, "build", fake_build)
    custom = tmp_path / "custom.db"
    assert cli.main(["build", "lts", "--output", str(custom), "--quiet"]) == 0
    capsys.readouterr()
    assert custom.read_bytes() == b"index"
    assert seen == {"source_url": source_url, "metadata_source": source_url}


def test_stale_recorded_source_is_not_replaced_by_same_version_tree(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = home / "kernels" / "linux-6.12.104"
    managed.mkdir(parents=True)
    (managed / "MAINTAINERS").write_text("different snapshot\n")
    assert cli.find_source_tree({
        "tree_path": str(tmp_path / "missing-original"),
        "kernel_version": "6.12.104",
        "index_stem": "6.12.104",
    }) is None


def test_source_commands_reject_parent_paths_from_an_untrusted_index(
        mini_index, mini_tree, tmp_path, capsys):
    import shutil

    tree = tmp_path / "tree"
    shutil.copytree(mini_tree, tree)
    copied = tmp_path / "unsafe.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    conn.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    conn.execute(
        "UPDATE files SET path='../outside.c' WHERE path='net/ipv4/tcp.c'")
    conn.commit()
    conn.close()

    for command in ("info", "path", "show"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command, "tcp_sendmsg"])
        assert "unsafe path" in capsys.readouterr().err


def test_source_commands_reject_symlinks_escaping_the_recorded_tree(
        mini_index, mini_tree, tmp_path, capsys):
    import shutil

    tree = tmp_path / "tree"
    shutil.copytree(mini_tree, tree)
    outside = tmp_path / "outside.c"
    outside.write_text("secret\n")
    escape = tree / "escape.c"
    try:
        escape.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    copied = tmp_path / "escaping-link.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    conn.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    conn.execute("UPDATE files SET path='escape.c' WHERE path='net/ipv4/tcp.c'")
    conn.commit()
    conn.close()

    for command in ("info", "path", "show"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command, "tcp_sendmsg"])
        assert "escapes the recorded source tree" in capsys.readouterr().err


def test_downloaded_build_rejects_output_inside_managed_source_before_fetch(
        tmp_path, monkeypatch, capsys):
    from kernel_atlas import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104",
            "https://cdn.kernel.org/example/linux-6.12.104.tar.xz", None),
    )
    monkeypatch.setattr(
        kernelsrc, "ensure_source",
        lambda *a, **kw: pytest.fail("unsafe output should fail before download"),
    )
    out = home / "kernels" / "linux-6.12.104" / "atlas.db"
    with pytest.raises(SystemExit):
        cli.main(["build", "lts", "--output", str(out), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
