"""Browsing, ownership details, listing filters, and output shape."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas.commands import cli


def test_info_omits_the_rest_and_includes_links(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "info", "mm", "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [s["name"] for s in data["subsystems"]]
    assert "THE REST" not in names
    assert "MEMORY MANAGEMENT" in names
    assert "elixir.bootlin.com" in data["links"]["elixir"]
    assert "is_static" not in data["target"]


def test_co_primary_file_owners_are_explicit_in_info_and_relationships(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "co-primary.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/ext4/inode.c'").fetchone()[0]
    owner_id, score = writer.execute(
        "SELECT subsystem_id,score FROM path_subsys"
        " WHERE ref_kind='file' AND ref_id=? AND is_primary=1", (file_id,)
    ).fetchone()
    other_id = writer.execute(
        "SELECT id FROM subsystems WHERE name='FILESYSTEMS (VFS and infrastructure)'"
    ).fetchone()[0]
    rank = writer.execute(
        "SELECT MAX(rank)+1 FROM path_subsys WHERE ref_kind='file' AND ref_id=?",
        (file_id,),
    ).fetchone()[0]
    writer.execute(
        "INSERT INTO path_subsys(ref_kind,ref_id,subsystem_id,score,rank,is_primary)"
        " VALUES ('file',?,?,?,?,1)", (file_id, other_id, score, rank))
    writer.execute(
        "UPDATE subsystems SET n_files=n_files+1,n_primary_files=n_primary_files+1"
        " WHERE id=?", (other_id,))
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "info", "fs/ext4/inode.c",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    primary = [row for row in data["subsystems"] if row["is_primary"]]
    assert {row["name"] for row in primary} == {
        "EXT4 FILE SYSTEM", "FILESYSTEMS (VFS and infrastructure)"}
    assert all("match_score" in row and "match_rank" in row for row in primary)

    assert cli.main(["--db", str(copied), "find", "ext4_bmap", "--exact"]) == 0
    assert "Co-owned (2 subsystems)" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "relationships", "fs/ext4/inode.c"])
    error = capsys.readouterr().err
    assert "co-primary" in error
    assert "EXT4 FILE SYSTEM" in error
    assert "is_inline" not in data["target"]
    assert "is_exported" not in data["target"]

    assert cli.main(["--db", str(mini_index), "info", "__alloc_pages",
                     "-f", "json"]) == 0
    symbol = json.loads(capsys.readouterr().out)["target"]
    assert symbol["is_static"] is False
    assert symbol["is_inline"] is False
    assert symbol["is_exported"] is True


def test_info_reports_complete_path_facts_and_truthful_linkage(
        mini_index, mini_tree, tmp_path, capsys):
    import json
    import shutil

    assert cli.main(["--db", str(mini_index), "info", "fs/ext4/inode.c",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    target = data["target"]
    assert target["size"] > 0 and target["lines"] > 0
    assert target["n_symbols"] > 0
    assert target["symbols_by_kind"]["function"] > 0
    assert target["index_status"] == "parsed"
    assert target["index_error"] is None
    assert target["is_symlink"] is False and target["link_target"] is None
    assert data["source_path"] == str(mini_tree / "fs/ext4/inode.c")

    assert cli.main(["--db", str(mini_index), "info", "fs/ext4/inode.c"]) == 0
    assert "index status parsed" in " ".join(capsys.readouterr().out.split())

    assert cli.main(["--db", str(mini_index), "info", "fs/ext4", "-f",
                     "json"]) == 0
    directory = json.loads(capsys.readouterr().out)["target"]
    assert directory["n_files"] == 3
    assert directory["n_subdirs"] == 0
    assert directory["n_files_subtree"] == 3

    assert cli.main(["--db", str(mini_index), "info", "ext4_bmap"]) == 0
    assert "exported to modules" in capsys.readouterr().out

    assert cli.main(["--db", str(mini_index), "info",
                     "fs/ext4/super.c:ext4_sb_info"]) == 0
    assert "linkage" not in capsys.readouterr().out

    assert cli.main(["--db", str(mini_index), "info",
                     "fs/ext4/super.c:ext4_sb_info", "-f", "json"]) == 0
    aggregate = json.loads(capsys.readouterr().out)["target"]
    assert "linkage" not in aggregate and "is_static" not in aggregate

    status_index = tmp_path / "status.db"
    shutil.copy(mini_index, status_index)
    writer = sqlite3.connect(status_index)
    writer.execute(
        "UPDATE files SET is_symlink=1, link_target='target.h', "
        "index_status='read_error', index_error='permission denied' "
        "WHERE path='fs/ext4/inode.c'")
    writer.commit()
    writer.close()
    assert cli.main(["--db", str(status_index), "info", "fs/ext4/inode.c",
                     "-f", "json"]) == 0
    status = json.loads(capsys.readouterr().out)["target"]
    assert status["is_symlink"] is True and status["link_target"] == "target.h"
    assert status["index_status"] == "read_error"
    assert status["index_error"] == "permission denied"


def test_info_root_and_directory_listings_do_not_invent_plurality_owners(
        mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "info", ".", "-f", "json"]) == 0
    root = json.loads(capsys.readouterr().out)
    assert root["target"]["n_files_subtree"] > 0
    assert root["n_subsystems"] > 1

    assert cli.main(["--db", str(mini_index), "ls", ".", "--kinds", "dir",
                     "-S", "-f", "json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    fs = next(row for row in entries if row["path"] == "fs")
    assert fs["subsystem"] == "Filesystems (mixed; includes unclassified)"


def test_info_reports_a_file_with_no_maintainers_match_even_with_an_area(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "unmatched.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='mm/page_alloc.c'").fetchone()[0]
    writer.execute(
        "DELETE FROM path_subsys WHERE ref_kind='file' AND ref_id=?",
        (file_id,))
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "info", "mm/page_alloc.c",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["area"]["name"] == "Memory management"
    assert data["unclassified_ownership"]["unmatched"] is True
    assert data["unclassified_ownership"]["maintainers_section"] is None

    assert cli.main(["--db", str(copied), "info", "mm/page_alloc.c"]) == 0
    assert "no primary MAINTAINERS match" in capsys.readouterr().out


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


@pytest.mark.parametrize("target", [".", "Makefile"])
def test_root_subtree_symbol_listing_requires_a_limit(
        mini_index, target, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), "siblings", target,
            "--level", "subtree", "--kinds", "all",
        ])
    assert "needs -n N" in capsys.readouterr().err


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


@pytest.mark.parametrize("extra", [
    ["--format", "plain", "--columns", "name"],
    ["--format", "names", "--with-subsystem"],
    ["--format", "tree", "--columns", "subsystem"],
])
def test_fixed_shape_listing_formats_reject_column_controls(
        mini_index, capsys, extra):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "find", "ext4", *extra])
    error = capsys.readouterr().err
    assert "does not apply" in error


def test_plain_find_does_not_compute_invisible_subsystems(
        mini_index, monkeypatch, capsys):
    from kernel_atlas.queries import query

    monkeypatch.setattr(
        query, "annotate_subsystems",
        lambda *args, **kwargs: pytest.fail("invisible subsystem annotation"),
    )
    assert cli.main([
        "--db", str(mini_index), "find", "ext4", "--format", "plain",
    ]) == 0
    assert "fs/ext4" in capsys.readouterr().out


def test_empty_columns_is_a_clear_error(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "mm", "-c", ","])
    assert "at least one column" in capsys.readouterr().err


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

    from kernel_atlas.storage import db
    from kernel_atlas.queries import query
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


@pytest.mark.parametrize("base", ["Area", "area", "a[bc]*?_%"])
def test_tree_command_stays_in_literal_subtree(path_index, base, capsys):
    import json

    assert cli.main([
        "--db", str(path_index), "tree", base, "--files", "-f", "json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["path"] for row in rows} == {f"{base}/unit.c", f"{base}/child"}


def test_limit_counts_siblings_not_the_target(mini_index, capsys):
    """`-n 3` must return three *other* functions, not two plus the target.

    ext4_bmap sorts first among the four functions in inode.c, so applying the
    limit before dropping the target would silently return one row too few.
    """
    from kernel_atlas.commands import cli

    cli.main(["--db", str(mini_index), "siblings", "fs/ext4/inode.c:ext4_bmap",
              "-f", "names", "-n", "3"])
    got = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert got == ["ext4_get_block", "ext4_helper", "ext4_inode_blocks_set"]
