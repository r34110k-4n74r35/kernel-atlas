"""Stored documentation evidence and explicit function-contract CLI behavior."""

import json
import sqlite3

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.indexing import indexer


@pytest.fixture
def study_docs(tmp_path):
    tree = tmp_path / "linux"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text("STUDY\nF: code.c\nF: Documentation/owner/\n")
    (tree / "code.c").write_text('''/**
 * study_get - Acquire a study reference
 * @object: The object being acquired.
 *
 * The caller retains the returned reference.
 *
 * Context: Process context; hold the study lock.
 * Return: One reference, or NULL on failure.
 */
int study_get(int object) { return object; }
int undocumented(void) { return 0; }
struct study_object { int state; };
''')
    for name, text in {
        "Documentation/unrelated/guide.rst": "Study guide\n===========\nUse study_get for a reference.\n",
        "Documentation/owner/guide.rst": "Ownership-only guide with no function mention.\n",
        "Documentation/empty.txt": "",
    }.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    output = tmp_path / "study.db"
    indexer.build(tree, output, "9.9", jobs=1, quiet=True)
    return output, tree


def test_docs_mentions_use_text_evidence_independent_of_ownership(study_docs, capsys):
    index, _ = study_docs
    assert cli.main(["--db", str(index), "docs", "study_get", "--mentions", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert any(hit["path"] == "Documentation/unrelated/guide.rst" for hit in result["matches"])
    assert not any(hit["path"] == "Documentation/owner/guide.rst" for hit in result["matches"])
    assert result["coverage"]["document_max_bytes"] == 1048576
    assert "unique symbol identity" in result["note"]


def test_stored_docs_and_contracts_work_after_source_disappears(study_docs, capsys):
    index, tree = study_docs
    tree.rename(tree.with_name("moved-source"))
    assert cli.main(["--db", str(index), "docs", "--search", "study lock", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["matches"][0]["section"] == "context"
    assert result["matches"][0]["line_kind"] == "documentation_block"
    assert cli.main(["--db", str(index), "info", "study_get", "--detail", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["documentation"]["documented"] is True
    assert result["documentation"]["parameters"] == {"object": "The object being acquired."}
    assert "study lock" in result["documentation"]["context"]
    assert result["source_exists"] is None


def test_search_scopes_and_empty_results_have_tidy_output(study_docs, capsys):
    index, _ = study_docs
    assert cli.main(["--db", str(index), "docs", "--search", "reference", "--under", "unrelated",
                     "--color", "always"]) == 0
    output = capsys.readouterr().out
    assert "Documentation/unrelated/guide.rst:3" in output
    assert "Study guide" in output and "\x1b[" in output
    assert "Oversized" in output
    assert cli.main(["--db", str(index), "docs", "Documentation/empty.txt", "--search", "missing"]) == 0
    assert "No matching text" in capsys.readouterr().out
    assert cli.main(["--db", str(index), "docs", "code.c", "--search", "reference", "-f", "json"]) == 0
    assert {hit["path"] for hit in json.loads(capsys.readouterr().out)["matches"]} == {"code.c"}


def test_info_detail_displays_documented_and_missing_fields(study_docs, capsys):
    index, _ = study_docs
    assert cli.main(["--db", str(index), "info", "study_get", "--detail"]) == 0
    output = capsys.readouterr().out
    assert "Documented function contract" in output
    assert "One reference, or NULL on failure" in output
    assert "not verified behavioral guarantees" in output
    assert cli.main(["--db", str(index), "info", "undocumented", "--detail", "-f", "json"]) == 0
    contract = json.loads(capsys.readouterr().out)["documentation"]
    assert contract["documented"] is False
    assert contract["summary"] is None and contract["context"] is None
    assert cli.main(["--db", str(index), "info", "undocumented", "--detail"]) == 0
    assert "not documented" in capsys.readouterr().out


def test_default_info_and_docs_shapes_are_preserved(study_docs, capsys):
    index, _ = study_docs
    assert cli.main(["--db", str(index), "info", "study_get", "-f", "json"]) == 0
    assert "documentation" not in json.loads(capsys.readouterr().out)
    assert cli.main(["--db", str(index), "docs", "study_get", "-f", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert isinstance(result, list) and "matches" not in result[0]


def test_old_indexes_explain_missing_document_capabilities(study_docs, capsys):
    index, _ = study_docs
    writer = sqlite3.connect(index)
    writer.execute("DELETE FROM meta WHERE key IN ('has_function_docs','has_document_text')")
    writer.execute("DROP TABLE function_docs")
    writer.execute("DROP TABLE document_text")
    writer.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    writer.commit()
    writer.close()
    for command, message in ((["info", "study_get", "--detail"], "no stored function documentation"),
                             (["docs", "--search", "reference"], "no stored documentation text")):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(index), *command])
        error = capsys.readouterr().err
        assert message in error and "--force" in error


@pytest.mark.parametrize("command, message", [
    (["docs", "--search", "reference", "--explain"], "cannot be used with --search or --mentions"),
    (["docs", "study_get", "--mentions", "--explain"], "cannot be used with --search or --mentions"),
    (["info", "study_object", "--detail"], "requires a function"),
    (["docs", "--mentions"], "requires a symbol target"),
    (["docs", "code.c", "--mentions"], "requires a symbol target"),
    (["docs", "study_get", "--search", "reference"], "must be a file or directory scope"),
])
def test_detail_and_search_targets_are_not_silently_reinterpreted(study_docs, capsys, command, message):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(study_docs[0]), *command])
    assert message in capsys.readouterr().err


def test_detail_requires_an_exact_function_definition(study_docs, capsys):
    index, _ = study_docs
    writer = sqlite3.connect(index)
    writer.execute("INSERT INTO symbols(file_id,name,kind,start_line,end_line) "
                   "SELECT file_id,name,kind,100,101 FROM symbols WHERE name='study_get'")
    writer.commit()
    writer.close()
    with pytest.raises(SystemExit):
        cli.main(["--db", str(index), "info", "study_get", "--detail"])
    assert "path:line" in capsys.readouterr().err
