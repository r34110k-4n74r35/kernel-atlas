"""Literal text, identifier boundaries, scopes and bounded stored excerpts."""

import json
import sqlite3

import pytest

from kernel_atlas.queries import function_docs, text_search


@pytest.fixture
def texts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT);
        CREATE TABLE symbols(id INTEGER PRIMARY KEY,file_id INTEGER,name TEXT);
        CREATE TABLE document_text(file_id INTEGER PRIMARY KEY,content TEXT);
        CREATE TABLE function_docs(symbol_id INTEGER PRIMARY KEY,line INTEGER,
          summary TEXT,parameters TEXT,description TEXT,context TEXT,returns TEXT);
        INSERT INTO files VALUES(1,'Documentation/a/index.rst'),
          (2,'Documentation/100%/note.md'),(3,'Documentation/100more/note.md'),
          (4,'code.c'),(5,'Documentation/empty.txt');
        INSERT INTO symbols VALUES(1,4,'study_get');
        INSERT INTO document_text VALUES(1,'Study guide
===========
Use study_get safely.
study_get_extended is a different identifier.
A café reference.
'),(2,'# Ownership independent
Study reference with study_get.
'),
          (3,'Study reference outside literal scope.'),(5,'');
    """)
    conn.execute("INSERT INTO function_docs VALUES(1,10,?,?,?,?,?)", (
        "Get a study object", json.dumps({"object": "The study object."}),
        "A retained reference is returned.", "Process context with caller lock held.",
        "A study object, or NULL."))
    yield conn
    conn.close()


def test_literal_matches_retain_lines_headings_and_sources(texts):
    result = text_search.search(texts, "STUDY_GET", include_contracts=False)
    assert [(hit["path"], hit["line"]) for hit in result["matches"]] == [
        ("Documentation/100%/note.md", 2), ("Documentation/a/index.rst", 3),
        ("Documentation/a/index.rst", 4)]
    assert result["matches"][0]["heading"] == "Ownership independent"
    assert result["matches"][1]["heading"] == "Study guide"
    assert all(hit["source_kind"] == "documentation" for hit in result["matches"])
    assert text_search.search(texts, "CAFÉ")["matches"][0]["line"] == 5


def test_mentions_use_c_identifier_boundaries_and_preserve_uncertainty(texts):
    result = text_search.search(texts, "study_get", mentions=True)
    assert len(result["matches"]) == 3
    assert not any(hit["line"] == 4 for hit in result["matches"])
    assert text_search.search(texts, "STUDY_GET", mentions=True)["matches"] == []
    assert "do not establish a unique symbol identity" in result["note"]


def test_scope_is_literal_and_precedes_limits(texts):
    result = text_search.search(texts, "reference", limit=1, under="100%")
    assert result["matches"][0]["path"] == "Documentation/100%/note.md"
    assert result["truncated"] is False
    result = text_search.search(texts, "study", scope="code.c")
    assert {hit["path"] for hit in result["matches"]} == {"code.c"}
    assert all(hit["source_kind"] == "kernel-doc" for hit in result["matches"])


def test_contract_results_anchor_the_block_instead_of_inventing_field_lines(texts):
    result = text_search.search(texts, "caller lock")
    assert len(result["matches"]) == 1
    hit = result["matches"][0]
    assert hit["path"] == "code.c" and hit["line"] == 10
    assert hit["line_kind"] == "documentation_block"
    assert hit["section"] == "context"
    assert hit["symbol"] == "study_get"


def test_literal_quotes_and_metacharacters_never_become_query_syntax(texts):
    for phrase in ("' OR 1=1 --", "study.*", "%", "_get$"):
        assert text_search.search(texts, phrase)["matches"] == []
    texts.execute("UPDATE document_text SET content=? WHERE file_id=1",
                  ("Literal ' OR 1=1 -- stays text.",))
    assert len(text_search.search(texts, "' OR 1=1 --")["matches"]) == 1


def test_result_and_snippet_bounds_are_explicit(texts):
    texts.execute("UPDATE document_text SET content=? WHERE file_id=1",
                  ("x" * 400 + "needle" + "y" * 400 + "\nneedle\nneedle",))
    result = text_search.search(texts, "needle", limit=1)
    assert result["truncated"] and result["truncation_reasons"] == ["result_limit"]
    assert len(result["matches"][0]["snippet"]) <= text_search.SNIPPET_LENGTH + 2
    assert "needle" in result["matches"][0]["snippet"]
    assert text_search.search(texts, "no hits", limit=0)["limit"] == 5000
    assert text_search.search(texts, "no hits", limit=9000)["limit"] == 5000


def test_empty_documents_and_unavailable_sources_are_supported(texts):
    assert text_search.search(texts, "anything", scope="Documentation/empty.txt")["matches"] == []
    assert text_search.search(texts, "study", include_documents=False,
                              include_contracts=False)["matches"] == []
    assert text_search.search(texts, "study", include_contracts=False)["matches"]


def test_function_contract_keeps_unknown_fields_explicit(texts):
    contract = function_docs.contract(texts, 1)
    assert contract["documented"] and contract["context"] == "Process context with caller lock held."
    assert contract["parameters"] == {"object": "The study object."}
    missing = function_docs.contract(texts, 2)
    assert missing["documented"] is False
    assert missing["context"] is None and missing["returns"] is None
    assert "not verified behavioral guarantees" in missing["note"]


@pytest.mark.parametrize("text, options", [
    ("", {}), (" ", {}), ("x" * 1025, {}), ("foo bar", {"mentions": True}),
    ("foo", {"limit": -1}), ("foo", {"limit": True}), ("foo", {"under": "../bad"}),
])
def test_invalid_text_and_limits_are_rejected(texts, text, options):
    with pytest.raises(ValueError):
        text_search.search(texts, text, **options)
