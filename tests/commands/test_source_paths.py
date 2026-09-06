"""Source-backed commands, path normalization, and containment boundaries."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import cli


def test_info_and_path_distinguish_a_missing_recorded_source_member(
        mini_index, mini_tree, tmp_path, capsys):
    import json
    import shutil

    tree = tmp_path / "linux-copy"
    shutil.copytree(mini_tree, tree)
    copied = tmp_path / "missing-member.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    writer.commit()
    writer.close()
    (tree / "mm/page_alloc.c").unlink()

    assert cli.main(["--db", str(copied), "info", "mm/page_alloc.c",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source_path"] == str(tree / "mm/page_alloc.c")
    assert data["source_exists"] is False

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "path", "mm/page_alloc.c"])
    assert "missing from the source tree" in capsys.readouterr().err


def test_cli_normalizes_only_absolute_targets_inside_the_recorded_tree(
        mini_index, mini_tree, tmp_path, capsys):
    import json

    source = mini_tree / "fs/ext4/inode.c"
    assert cli.main(["--db", str(mini_index), "info", str(source),
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["target"]["path"] == "fs/ext4/inode.c"

    assert cli.main(["--db", str(mini_index), "locate", str(source),
                     "-f", "json"]) == 0
    located = json.loads(capsys.readouterr().out)
    assert located[0]["found"] and located[0]["path"] == "fs/ext4/inode.c"

    assert cli.main(["--db", str(mini_index), "info", f"{source}:3",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["target"]["name"] == "ext4_inode_blocks_set"

    outside = tmp_path / "inode.c"
    outside.write_text("not the indexed file\n")
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  f"{outside}:ext4_bmap"])
    assert "outside the recorded source tree" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  "wrong/place/inode.c:ext4_bmap"])
    assert "nothing in the index matches" in capsys.readouterr().err


def test_absolute_target_normalization_preserves_an_indexed_symlink_leaf(
        tmp_path):
    tree = tmp_path / "linux-9.9"
    target = tree / "Documentation/process/changes.rst"
    target.parent.mkdir(parents=True)
    target.write_text("changes\n")
    (tree / "MAINTAINERS").write_text("TEST\nF: Documentation/\n")
    link = tree / "Documentation/Changes"
    try:
        link.symlink_to("process/changes.rst")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    normalized = cli._normalize_target_spec(
        {"tree_path": str(tree), "kernel_version": "9.9"}, str(link))
    assert normalized == "Documentation/Changes"


@pytest.mark.parametrize("command", ["show", "path", "web"])
def test_source_identity_commands_reject_unmatched_line_selector(
        mini_index, command, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), command,
                  "fs/ext4/inode.c:9999"])
    assert "no symbol spans line 9999" in capsys.readouterr().err


@pytest.mark.parametrize("command", [
    ["path", "super.c"],
    ["show", "super.c", "--bare"],
    ["web", "super.c", "--url", "elixir"],
])
def test_source_identity_commands_reject_ambiguous_bare_files(
        mini_index, command, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), *command])
    error = capsys.readouterr().err
    assert "2 files" in error
    assert "fs/ext4/super.c" in error
    assert "fs/btrfs/super.c" in error


@pytest.mark.parametrize("line", ["1", "01", "+01"])
@pytest.mark.parametrize("command", [
    ["path"],
    ["show", "--bare"],
    ["web", "--url", "elixir"],
    ["calls"],
])
def test_concrete_commands_reject_ambiguous_basename_line_selectors(
        mini_index, line, command, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), command[0], f"super.c:{line}",
            *command[1:],
        ])
    error = capsys.readouterr().err
    assert "2 files named 'super.c'" in error
    assert "full indexed path:line" in error
    assert f"fs/ext4/super.c:{line}" in error
    assert f"fs/btrfs/super.c:{line}" in error


def test_full_path_line_selectors_remain_exact_for_concrete_commands(
        mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "path", "fs/btrfs/super.c:1", "--line",
    ]) == 0
    assert "fs/btrfs/super.c:1" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "show", "fs/btrfs/super.c:1", "--bare",
    ]) == 0
    assert "btrfs_mount" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "web", "fs/btrfs/super.c:1",
        "--url", "elixir",
    ]) == 0
    assert "fs/btrfs/super.c#L1" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "calls", "fs/btrfs/super.c:1",
        "--format", "json",
    ]) == 0
    assert capsys.readouterr().out.strip() == "[]"

    assert cli.main([
        "--db", str(mini_index), "struct", "fs/ext4/super.c:20",
        "--format", "json",
    ]) == 0
    assert '"name": "ext4_sb_info"' in capsys.readouterr().out


def test_numeric_directory_name_is_never_mistaken_for_a_line_selector(
        mini_index, mini_tree, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "numeric-directory.db"
    copied_tree = tmp_path / "linux-6.12.104"
    shutil.copy(mini_index, copied)
    shutil.copytree(mini_tree, copied_tree)
    (copied_tree / "drivers/8250").mkdir()

    writer = sqlite3.connect(copied)
    writer.execute(
        "INSERT INTO dirs(path,name,parent_id,depth,n_files,n_subdirs,"
        " n_files_recursive) VALUES ('drivers/8250','8250',"
        " (SELECT id FROM dirs WHERE path='drivers'),2,0,0,0)"
    )
    writer.execute(
        "UPDATE dirs SET n_subdirs=n_subdirs+1 WHERE path='drivers'"
    )
    writer.execute(
        "UPDATE meta SET value=printf('%d',CAST(value AS INTEGER)+1)"
        " WHERE key='n_dirs'"
    )
    writer.execute(
        "UPDATE meta SET value=? WHERE key='tree_path'", (str(copied_tree),)
    )
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "locate", "8250", "-f", "json"]) == 0
    located = json.loads(capsys.readouterr().out)[0]
    assert located["found"] is True
    assert located["kind"] == "dir"
    assert located["path"] == "drivers/8250"

    assert cli.main(["--db", str(copied), "path", "8250"]) == 0
    assert capsys.readouterr().out.strip().endswith("drivers/8250")

    assert cli.main([
        "--db", str(copied), "web", "8250", "--url", "elixir",
    ]) == 0
    assert "/source/drivers/8250" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "show", "8250"])
    error = capsys.readouterr().err
    assert "is a directory" in error
    assert "spans line 8250" not in error


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
