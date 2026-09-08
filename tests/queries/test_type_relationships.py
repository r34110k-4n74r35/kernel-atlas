"""Scoped declaration relationships retain ambiguity and evidence."""

import pytest
import sqlite3

from kernel_atlas.indexing import indexer
from kernel_atlas.queries.structures import resolve_structure
from kernel_atlas.queries.type_relationships import structure_relationships
from kernel_atlas.queries import type_relationships
from kernel_atlas.storage import db


@pytest.fixture
def relation_index(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text("PACKETS\nF: packet.h\nF: users.c\n")
    sources = {
        "packet.h": "typedef struct packet { int value; } packet_t;\n",
        "duplicate.h": "struct packet { long other; };\nunion packet { int n; };\n",
        "users.c": (
            "struct holder {\n"
            " struct packet embedded;\n"
            " struct packet *pointer;\n"
            " packet_t aliases[2];\n"
            " struct packet *(*callback)(union packet *, struct missing *);\n"
            " int packet;\n"
            " struct packet_extra *unrelated;\n"
            " int size[sizeof(struct packet)];\n"
            "};\n"
            "struct packet *receive(struct packet *input) { return input; }\n"
        ),
        "private.c": (
            "struct packet { int private; };\n"
            "struct private_holder { struct packet *value; };\n"
            "int fn(void) {\n"
            " struct packet { char local; };\n"
            " struct local_holder { struct packet *value; };\n"
            " return 0;\n"
            "}\n"
        ),
    }
    for path, content in sources.items():
        (tree / path).write_text(content)
    database = tmp_path / "types.db"
    indexer.build(tree, database, "9.9", jobs=1, quiet=True)
    # The default parser excludes function-local aggregate definitions. Retain
    # explicit local records here to exercise scope filtering independently of
    # the builder's symbol-selection policy.
    with db.connect(database, readonly=False) as writer:
        file_id = writer.execute("SELECT id FROM files WHERE path='private.c'").fetchone()[0]
        writer.execute(
            "INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
            "VALUES (?,'packet','struct',4,4)", (file_id,))
        local_id = writer.execute(
            "INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
            "VALUES (?,'local_holder','struct',5,5)", (file_id,)).lastrowid
        writer.execute(
            "INSERT INTO type_members(symbol_id,ordinal,name,kind,declaration,start_line,end_line) "
            "VALUES (?,0,'value','pointer','struct packet *value;',5,5)", (local_id,))
    with db.connect(database, readonly=True) as conn:
        yield conn


def test_type_relationships_cover_embedding_pointers_aliases_callbacks_and_functions(relation_index):
    target = resolve_structure(relation_index, "packet.h:packet").target
    payload = structure_relationships(relation_index, target)
    assert payload["outgoing"] == []
    incoming = payload["incoming"]
    assert {entry["role"] for entry in incoming} == {
        "embedded_member", "pointer_member", "callback_return",
        "function_return", "function_parameter",
    }
    assert not any(entry["member"] in {"packet", "unrelated", "size"} for entry in incoming)
    assert not any(entry["owner"]["path"] == "private.c" for entry in incoming)
    tagged = next(entry for entry in incoming if entry["member"] == "embedded")
    assert tagged["resolution"] == "ambiguous"
    assert {row["path"] for row in tagged["candidates"]} == {"packet.h", "duplicate.h"}
    assert {row["kind"] for row in tagged["candidates"]} == {"struct"}
    alias = next(entry for entry in incoming if entry["member"] == "aliases")
    assert alias["resolution"] == "candidate" and alias["is_array"]
    assert alias["candidates"][0]["subsystems"][0]["name"] == "PACKETS"
    assert alias["declaration"] == "packet_t aliases[2];"
    assert alias["line"] == 4


def test_outgoing_relationships_preserve_unresolved_tags_and_union_kind(relation_index):
    target = resolve_structure(relation_index, "holder").target
    payload = structure_relationships(relation_index, target, incoming=False)
    assert payload["incoming"] == [] and not payload["incoming_requested"]
    union = next(entry for entry in payload["outgoing"] if entry["kind"] == "union")
    assert union["role"] == "callback_parameter"
    assert union["candidates"][0]["kind"] == "union"
    missing = next(entry for entry in payload["outgoing"] if entry["name"] == "missing")
    assert missing["resolution"] == "unresolved" and missing["candidates"] == []


def test_same_file_and_function_scope_prevent_cross_binding_duplicate_tags(relation_index):
    target = resolve_structure(relation_index, "private_holder").target
    reference, = structure_relationships(relation_index, target)["outgoing"]
    candidate, = reference["candidates"]
    assert candidate["path"] == "private.c" and candidate["line"] == 1
    assert candidate["visibility"] == "same_file"
    target = resolve_structure(relation_index, "local_holder").target
    reference, = structure_relationships(relation_index, target)["outgoing"]
    candidate, = reference["candidates"]
    assert candidate["path"] == "private.c" and candidate["line"] == 4
    assert candidate["visibility"] == "same_function"


def test_relationship_caps_apply_to_each_direction_and_report_truncation(relation_index):
    target = resolve_structure(relation_index, "packet.h:packet").target
    payload = structure_relationships(relation_index, target, limit=2)
    assert len(payload["incoming"]) == 2 and payload["incoming_truncated"]
    assert not payload["outgoing_truncated"]
    for limit in (0, 1001):
        with pytest.raises(ValueError, match="between 1 and 1000"):
            structure_relationships(relation_index, target, limit=limit)


def test_candidate_sample_is_bounded_without_losing_incoming_target_or_ambiguity(relation_index):
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        relation_index.backup(conn)
        directory = conn.execute("SELECT id FROM dirs WHERE path=''").fetchone()[0]
        for number in range(type_relationships.CANDIDATE_LIMIT + 2):
            name = f'other{number:03}.h'
            file_id = conn.execute(
                "INSERT INTO files(path,dir_id,name,ext) VALUES (?,?,?,'.h')",
                (name, directory, name)).lastrowid
            conn.execute(
                "INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
                "VALUES (?,'packet','struct',1,1)", (file_id,))
        target = resolve_structure(conn, 'packet.h:packet').target
        owner = resolve_structure(conn, 'holder').target
        outgoing = structure_relationships(conn, owner, incoming=False, limit=1)
        reference, = outgoing['outgoing']
        assert len(reference['candidates']) == outgoing['candidate_limit'] == 100
        assert reference['candidates_total'] == 104 and reference['candidates_truncated']
        assert reference['resolution'] == 'ambiguous'
        assert not any(candidate['id'] == target.id for candidate in reference['candidates'])

        incoming = structure_relationships(conn, target, outgoing=False, limit=1)
        reference, = incoming['incoming']
        assert reference['owner']['name'] == 'holder'
        assert reference['candidates_total'] == 104
        assert len(reference['candidates']) == 100
        assert any(candidate['id'] == target.id for candidate in reference['candidates'])


def test_ambiguity_counts_complete_set_even_when_only_one_candidate_is_displayed(
        relation_index, monkeypatch):
    monkeypatch.setattr(type_relationships, 'CANDIDATE_LIMIT', 1)
    target = resolve_structure(relation_index, 'holder').target
    relation, = structure_relationships(relation_index, target, limit=1)['outgoing']
    assert len(relation['candidates']) == 1
    assert relation['candidates_total'] == 2
    assert relation['resolution'] == 'ambiguous' and relation['candidates_truncated']
