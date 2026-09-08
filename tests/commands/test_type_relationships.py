"""Opt-in structure relationships in JSON and human CLI reports."""

import json

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.storage import db
from kernel_atlas.queries import type_relationships


def test_struct_relationships_are_opt_in_and_render_color(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "struct", "ext4_sb_info", "-f", "json"]) == 0
    assert "type_relationships" not in json.loads(capsys.readouterr().out)["definitions"][0]
    assert cli.main([
        "--db", str(mini_index), "--color", "always", "struct", "ext4_sb_info", "--relations",
    ]) == 0
    output = capsys.readouterr().out
    assert "Types referenced by members" in output
    assert "Declarations using this type" in output
    assert "callback parameter" in output and "struct inode" in output
    assert "No indexed definition" in output and "\x1b[" in output


def test_struct_used_by_json_returns_declaration_evidence_without_outgoing(
        mini_index, tmp_path, capsys):
    copied = tmp_path / "relations.db"
    with db.connect(mini_index) as original, db.connect(copied, readonly=False) as writer:
        original.backup(writer)
        file_id = writer.execute(
            "SELECT file_id FROM symbols WHERE name='ext4_sb_info'").fetchone()[0]
        # Explicit same-file evidence can be inspected without source access.
        writer.execute(
            "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature) "
            "VALUES (?,'consume_ext4','function',900,902,"
            "'struct ext4_sb_info *consume_ext4(struct ext4_sb_info *info)')", (file_id,))
    assert cli.main([
        "--db", str(copied), "struct", "ext4_sb_info", "--used-by", "--max-relations", "1",
        "-f", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)["definitions"][0]["type_relationships"]
    assert not result["outgoing_requested"] and result["outgoing"] == []
    assert result["incoming_requested"] and result["incoming_truncated"]
    reference, = result["incoming"]
    assert reference["owner"]["name"] == "consume_ext4"
    assert reference["role"] == "function_return"
    assert reference["candidates"][0]["visibility"] == "same_file"


@pytest.mark.parametrize("limit", ["0", "1001"])
def test_struct_relationship_limit_is_bounded(mini_index, capsys, limit):
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--db", str(mini_index), "struct", "ext4_sb_info", "--relations",
            "--max-relations", limit,
        ])
    assert exc.value.code == 2
    assert "--max-relations" in capsys.readouterr().err


def test_struct_reports_candidate_truncation_separately_from_relationship_truncation(
        mini_index, tmp_path, capsys, monkeypatch):
    copied = tmp_path / 'candidate-limits.db'
    with db.connect(mini_index) as original, db.connect(copied, readonly=False) as writer:
        original.backup(writer)
        directory = writer.execute("SELECT id FROM dirs WHERE path=''").fetchone()[0]
        for name in ('first.h', 'second.h'):
            file_id = writer.execute(
                "INSERT INTO files(path,dir_id,name,ext) VALUES (?,?,?,'.h')",
                (name, directory, name)).lastrowid
            writer.execute(
                "INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
                "VALUES (?,'inode','struct',1,1)", (file_id,))
    monkeypatch.setattr(type_relationships, 'CANDIDATE_LIMIT', 1)
    assert cli.main([
        '--db', str(copied), 'struct', 'ext4_sb_info', '--relations', '--color', 'never',
    ]) == 0
    output = capsys.readouterr().out
    assert 'showing 1 of 2 candidates' in output
    assert 'Candidate definitions are limited to 1 per relationship' in output
    assert 'ambiguity and matching use the full candidate set' in output
