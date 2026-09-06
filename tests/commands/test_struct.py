"""Structure reports, aliases, and unambiguous reusable selectors."""

from __future__ import annotations

import pytest

from kernel_atlas import cli, db


def test_struct_command_renders_detailed_member_study_report(
        mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "struct",
        "fs/ext4/super.c:ext4_sb_info",
    ]) == 0
    out = capsys.readouterr().out
    assert "struct ext4_sb_info" in out
    assert "in-memory ext4 superblock study fixture" in out
    assert "write_inode" in out and "callback" in out
    assert "state" in out and "bitfield:2" in out
    assert "DECLARE_BITMAP" in out
    assert "#ifdef CONFIG_EXT4_STUDY_FEATURES" in out
    assert "Byte offsets, padding" in out
    assert "Next:" in out and " show " in out


def test_struct_json_is_stable_nested_and_source_independent(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "source-removed.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    missing = tmp_path / "source-that-does-not-exist"
    writer.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(missing),))
    writer.execute("UPDATE meta SET value=? WHERE key='source'", (str(missing),))
    writer.commit()
    writer.close()

    assert cli.main([
        "--db", str(copied), "structure", "ext4_sb_info", "-f", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "ext4_sb_info"
    assert payload["n_definitions"] == 1
    definition = payload["definitions"][0]
    assert definition["source_exists"] is False
    assert definition["source_path"].endswith("fs/ext4/super.c")
    assert definition["c_name"] == "struct ext4_sb_info"
    assert definition["selector"] == "fs/ext4/super.c:ext4_sb_info"
    assert "qualified_name" not in definition
    assert definition["documentable_member_count"] == 11
    assert definition["documentation_coverage"] == 1.0
    assert any(owner["is_primary"] for owner in definition["subsystems"])
    assert definition["direct_member_count"] == 9
    assert definition["members"][6]["children"][1]["children"][0]["name"] == "low"
    assert definition["members"][8]["is_flexible_array"] is True
    assert definition["members"][8]["visibility"] == "private"


def test_struct_command_rejects_ambiguity_without_guessing(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "ambiguous-structure.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/btrfs/super.c'").fetchone()[0]
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'ext4_sb_info','struct',1,1,'struct ext4_sb_info { 0 members }')",
        (file_id,),
    )
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "struct", "ext4_sb_info"])
    error = capsys.readouterr().err
    assert "2 aggregate definitions" in error
    assert "path:name" in error

    assert cli.main([
        "--db", str(copied), "struct", "ext4_sb_info", "--all", "-f", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_definitions"] == 2


def test_struct_ambiguity_recommends_only_reusable_collision_selectors(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "structure-alias-collision.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    tagged = writer.execute(
        "SELECT s.id,s.file_id FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE f.path='fs/ext4/super.c' AND s.name='ext4_sb_info'"
    ).fetchone()
    writer.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'alias_spelling')",
        (tagged["id"],),
    )
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'alias_spelling','struct',1,1,"
        " 'struct alias_spelling { 0 members }')",
        (tagged["file_id"],),
    )
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "struct", "alias_spelling"])
    error = capsys.readouterr().err
    assert "fs/ext4/super.c:ext4_sb_info" in error
    assert "fs/ext4/super.c:1" in error
    assert "fs/ext4/super.c:alias_spelling" not in error


def test_struct_kind_qualified_selector_examples_are_shell_quoted(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "structure-kind-collision.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    tagged = writer.execute(
        "SELECT s.file_id FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE f.path='fs/ext4/super.c' AND s.name='ext4_sb_info'"
    ).fetchone()
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'ext4_sb_info','union',1,1,"
        " 'union ext4_sb_info { 0 members }')",
        (tagged["file_id"],),
    )
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "struct", "ext4_sb_info"])
    error = capsys.readouterr().err
    assert "'struct fs/ext4/super.c:ext4_sb_info'" in error
    assert "'union fs/ext4/super.c:ext4_sb_info'" in error
    assert cli.main([
        "--db", str(copied), "struct",
        "union fs/ext4/super.c:ext4_sb_info", "-f", "json",
    ]) == 0


def test_struct_command_accepts_anonymous_typedef_alias(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "struct", "struct study_mask_t",
        "-f", "json",
    ]) == 0
    detail = json.loads(capsys.readouterr().out)["definitions"][0]
    assert detail["is_anonymous"] is True
    assert detail["aliases"] == ["study_mask_t"]
    assert detail["tag"] is None and detail["c_name"] is None
    assert detail["members"][0]["name"] == "bits"


def test_struct_command_accepts_unions_and_kind_prefixes(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "struct", "union study_value",
        "-f", "json",
    ]) == 0
    detail = json.loads(capsys.readouterr().out)["definitions"][0]
    assert detail["kind"] == "union"
    assert detail["c_name"] == "union study_value"
    assert [member["name"] for member in detail["members"]] == [
        "signed_value", "unsigned_value",
    ]

    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), "struct", "struct study_value",
        ])
    assert "no struct/union tag" in capsys.readouterr().err


def test_struct_line_selector_rejects_overlapping_aggregates(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "overlapping-aggregate.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    row = writer.execute(
        "SELECT file_id,start_line,end_line FROM symbols"
        " WHERE name='ext4_sb_info' AND kind='struct'"
    ).fetchone()
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,'generated_view','struct',?,?,"
        " 'struct generated_view { 0 members }')",
        (row["file_id"], row["start_line"] + 1, row["end_line"] - 1),
    )
    writer.commit()
    writer.close()

    line = row["start_line"] + 2
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(copied), "struct", f"fs/ext4/super.c:{line}",
        ])
    error = capsys.readouterr().err
    assert "2 aggregate definitions" in error
    assert "path:name" in error


def test_struct_rejects_an_ambiguous_basename_line_selector(
        mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "struct", "super.c:20"])
    error = capsys.readouterr().err
    assert "2 files named 'super.c'" in error
    assert "fs/ext4/super.c:20" in error
    assert "fs/btrfs/super.c:20" in error
