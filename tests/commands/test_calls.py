"""Call graph commands, occurrence evidence, and rebuild advice."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas import cli, config


def test_calls_rejects_non_function_targets(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "S_IRWXU"])
    assert "not a function or syscall" in capsys.readouterr().err


def test_no_call_graph_rebuild_hints_target_the_selected_custom_database(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "without-calls.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.commit()
    tree = writer.execute(
        "SELECT value FROM meta WHERE key='tree_path'").fetchone()[0]
    writer.close()

    for command in (
            ["calls", "ext4_bmap"],
            ["relationships", "EXT4 FILE SYSTEM", "--via", "calls"]):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), *command])
        error = capsys.readouterr().err
        assert "--with-calls --force" in error
        assert f"--output {copied.resolve()}" in error
        assert f"--src {tree}" in error


@pytest.mark.parametrize("pinned", [False, True])
def test_no_call_graph_rebuild_hint_preserves_a_selected_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys, pinned):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    selected = indexes / "study.db"
    shutil.copy(mini_index, selected)
    writer = sqlite3.connect(selected)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    tree = writer.execute(
        "SELECT value FROM meta WHERE key='tree_path'").fetchone()[0]
    writer.commit()
    writer.close()

    selector = [] if pinned else ["-K", "study"]
    if pinned:
        config.set_default_version("study")
    with pytest.raises(SystemExit):
        cli.main([*selector, "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert f"--output {selected.resolve()}" in error
    assert f"--src {tree}" in error
    assert "build 6.12.104" in error

    assert cli.main([*selector, "info", "mm"]) == 0
    output = capsys.readouterr().out
    if pinned:
        assert f"Next:  {cli.PROG} siblings mm" in output
    else:
        assert f"Next:  {cli.PROG} -K study siblings mm" in output


def test_no_call_graph_advice_does_not_replace_missing_custom_source(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "missing-custom-source.db"
    shutil.copy(mini_index, copied)
    missing = tmp_path / "vendor-tree-that-was-removed"
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.executemany("UPDATE meta SET value=? WHERE key=?", [
        (str(missing), "tree_path"), (str(missing), "source")])
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "restore the recorded custom source tree" in error
    assert "--src" not in error


def test_no_call_graph_advice_can_refetch_missing_downloaded_source(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "missing-downloaded-source.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.execute("UPDATE meta SET value=? WHERE key='tree_path'",
                   (str(tmp_path / "removed-cache"),))
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "--with-calls --force" in error
    assert f"--output {copied.resolve()}" in error
    assert "--src" not in error


@pytest.mark.parametrize(
    "kind", ["struct", "macro", "variable", "file", "function,file"])
def test_calls_rejects_result_kinds_that_can_never_occur(
        mini_index, kind, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "ext4_bmap",
                  "--kinds", kind])
    assert "only lists function and syscall" in capsys.readouterr().err


def test_calls_output_exposes_mixed_occurrence_counts(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "mixed-call-output.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute(
        "UPDATE calls SET direct_count=2,indirect_count=1,macro_count=1"
        " WHERE callee='ext4_get_block'"
    )
    writer.execute(
        "UPDATE meta SET value=(SELECT CAST(SUM(direct_count+indirect_count+"
        " macro_count) AS TEXT) FROM calls) WHERE key='n_call_occurrences'"
    )
    writer.commit()
    writer.close()

    assert cli.main([
        "--db", str(copied), "calls", "ext4_bmap", "--format", "json",
    ]) == 0
    outgoing = json.loads(capsys.readouterr().out)[0]
    assert outgoing["direct_count"] == 2
    assert outgoing["indirect_count"] == 1
    assert outgoing["macro_count"] == 1

    assert cli.main([
        "--db", str(copied), "calls", "ext4_bmap",
    ]) == 0
    table = capsys.readouterr().out
    assert "OCCURRENCES" in table
    assert "2d 1i 1m" in table

    assert cli.main([
        "--db", str(copied), "calls", "ext4_get_block", "--callers",
        "--format", "json",
    ]) == 0
    incoming = json.loads(capsys.readouterr().out)[0]
    assert incoming["name"] == "ext4_bmap"
    assert incoming["direct_count"] == 2
    assert incoming["indirect_count"] == 1
    assert incoming["macro_count"] == 1

    assert cli.main([
        "--db", str(copied), "calls", "ext4_get_block", "--callers",
    ]) == 0
    assert "2d 1i 1m" in capsys.readouterr().out


def test_calls_ambiguity_recommends_line_for_same_file_definitions(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "conditional.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_bmap'"
    ).fetchone()
    second_line = row[3] + 100
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], second_line, row[4] + 100, *row[5:]))
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "path:line" in error
    assert "fs/ext4/inode.c:" in error

    for command in ("show", "path", "web"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command,
                      "fs/ext4/inode.c:ext4_bmap"])
        assert "path:line" in capsys.readouterr().err

    assert cli.main(["--db", str(copied), "info",
                     f"fs/ext4/inode.c:{second_line}"]) == 0
    output = capsys.readouterr().out
    assert f"siblings fs/ext4/inode.c:{second_line}" in output


def test_trace_does_not_treat_macros_as_stack_frames(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "trace", "S_IRWXU", "tcp_sendmsg",
                     "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["frame"] == "S_IRWXU" and rows[0]["found"] is False
    assert rows[1]["frame"] == "tcp_sendmsg" and rows[1]["found"] is True


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
