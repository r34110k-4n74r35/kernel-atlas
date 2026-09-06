"""Structure identity resolution, aliases, member details, and source coverage."""

import pytest

from kernel_atlas import db, query


def test_resolve_structure_is_kind_scoped_and_accepts_typedef_alias(conn):
    tagged = query.resolve_structure(
        conn, "fs/ext4/super.c:ext4_sb_info").target
    assert tagged is not None
    assert tagged.symbol_kind == "struct"
    assert tagged.path == "fs/ext4/super.c"

    anonymous = query.resolve_structure(conn, "struct study_mask_t").target
    assert anonymous is not None
    assert anonymous.path == "include/linux/fs.h"
    assert anonymous.is_anonymous

    union = query.resolve_structure(conn, "union study_value").target
    assert union is not None and union.symbol_kind == "union"
    assert query.resolve_structure(conn, "struct study_value").target is None


def test_structure_detail_is_nested_documented_and_source_ordered(conn):
    target = query.resolve_structure(
        conn, "fs/ext4/super.c:ext4_sb_info").target
    detail = query.structure_detail(conn, target)
    assert detail["summary"] == "in-memory ext4 superblock study fixture"
    assert detail["kind"] == "struct"
    assert detail["tag"] == "ext4_sb_info"
    assert detail["c_name"] == "struct ext4_sb_info"
    assert detail["selector"] == "fs/ext4/super.c:ext4_sb_info"
    assert detail["signature"].startswith("struct ext4_sb_info")
    assert detail["direct_member_count"] == 9
    assert detail["total_member_count"] == 13
    assert detail["documentable_member_count"] == 11
    assert detail["described_member_count"] == 11
    assert detail["documented_member_count"] == 11
    assert detail["semantic_description_count"] == 0
    assert detail["documentation_coverage"] == 1.0
    assert detail["members"][0]["name"] == "s_blocks_count"
    assert detail["members"][2]["array_dimensions"] == ["16"]
    assert detail["members"][3]["bit_width"] == "2"
    assert detail["members"][6]["kind"] == "union"
    assert detail["members"][6]["children"][1]["kind"] == "struct"
    assert detail["members"][7]["generated_by"] == "DECLARE_BITMAP"
    assert detail["members"][7]["conditions"] == [
        "#ifdef CONFIG_EXT4_STUDY_FEATURES",
    ]
    assert detail["members"][8]["is_flexible_array"]
    assert detail["members"][8]["visibility"] == "private"


def test_structure_coverage_separates_parser_semantics_from_source_docs(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "semantic-description.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    writer.execute(
        "UPDATE type_members SET description_source='macro-semantics'"
        " WHERE generated_by='DECLARE_BITMAP'"
    )
    writer.execute(
        "UPDATE type_members SET description='Generated container.',"
        " description_source='macro-semantics'"
        " WHERE id=(SELECT id FROM type_members WHERE is_anonymous=1 LIMIT 1)"
    )
    target = query.resolve_structure(writer, "ext4_sb_info").target
    detail = query.structure_detail(writer, target)
    writer.close()
    assert detail["described_member_count"] == 11
    assert detail["documented_member_count"] == 10
    assert detail["semantic_description_count"] == 2
    assert detail["documentation_coverage"] == pytest.approx(10 / 11)


def test_structure_selectors_survive_tag_and_typedef_name_collisions(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "aggregate-namespace-collision.db"
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
    resolution = query.resolve_structure(writer, "alias_spelling")
    candidates = [resolution.target, *resolution.candidates]
    selectors = {target.name: query.structure_selector(writer, target)
                 for target in candidates}
    writer.close()
    assert selectors == {
        "ext4_sb_info": "fs/ext4/super.c:ext4_sb_info",
        "alias_spelling": "fs/ext4/super.c:1",
    }


def test_structure_resolution_does_not_promise_an_unexpressible_selector(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "unselectable-aggregate.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    row = writer.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature FROM symbols"
        " WHERE name='ext4_sb_info' AND kind='struct'"
    ).fetchone()
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature)"
        " VALUES (?,?,?,?,?,?)", tuple(row),
    )
    resolution = query.resolve_structure(writer, "ext4_sb_info")
    candidates = [resolution.target, *resolution.candidates]
    selectors = [query.structure_selector(writer, target)
                 for target in candidates]
    writer.close()
    assert selectors == [None, None]
    assert "where an exact command selector is expressible" in resolution.note


def test_union_detail_uses_the_actual_c_kind(conn):
    target = query.resolve_structure(conn, "study_value").target
    detail = query.structure_detail(conn, target)
    assert detail["kind"] == "union"
    assert detail["tag"] == "study_value"
    assert detail["c_name"] == "union study_value"
    assert detail["direct_member_count"] == 2
    assert detail["documentation_coverage"] == 1.0


def test_structure_resolution_ignores_same_named_function(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "same-name-function.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    file_id = writer.execute(
        "SELECT id FROM files WHERE path='fs/ext4/super.c'").fetchone()[0]
    writer.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line)"
        " VALUES (?,'ext4_sb_info','function',1,1)", (file_id,))
    writer.commit()
    resolved = query.resolve_structure(writer, "ext4_sb_info")
    writer.close()
    assert resolved.target.symbol_kind == "struct"
    assert not resolved.candidates


def test_structure_resolution_ignores_enum_typedef_aliases(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "enum-alias.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    enum_id = writer.execute(
        "SELECT id FROM symbols WHERE kind='enum' AND name='rw_hint'"
    ).fetchone()[0]
    writer.execute(
        "INSERT INTO type_aliases(symbol_id,name) VALUES (?,'rw_hint_t')",
        (enum_id,),
    )
    count = writer.execute("SELECT COUNT(*) FROM type_aliases").fetchone()[0]
    writer.execute(
        "UPDATE meta SET value=? WHERE key='n_type_aliases'", (str(count),)
    )
    writer.commit()
    db.validate_schema(writer, deep=True)

    resolved = query.resolve_structure(writer, "rw_hint_t")
    writer.close()

    assert resolved.target is None
    assert "no struct/union tag or typedef alias" in resolved.note
