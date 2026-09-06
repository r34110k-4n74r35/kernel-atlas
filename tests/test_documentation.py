"""Documentation relevance, filtering, and explainable CLI output."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from kernel_atlas import cli, db, indexer, query
from kernel_atlas.documentation_query import documentation_scope


@pytest.fixture(scope="module")
def study_index(tmp_path_factory):
    root = tmp_path_factory.mktemp("documentation-study")
    tree = root / "linux"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text(
        "USB STUDY\nF: include/linux/usb.h\nF: Documentation/usb/\n"
        "F: Documentation/driver-api/usb/\n"
        "F: Documentation/devicetree/bindings/usb/\n",
        encoding="utf-8",
    )
    contents = {
        "include/linux/usb.h": "struct usb_device { int address; };\n",
        "core.c": "struct packet_queue { int length; };\n",
        "Documentation/usb/index.rst": "USB guide\n",
        "Documentation/driver-api/usb/device.rst": "USB device API\n",
        "Documentation/devicetree/bindings/usb/usb-device.yaml": "title: USB device\n",
        "Documentation/devicetree/bindings/usb/vendor.txt": "USB binding\n",
        "Documentation/usb/Makefile": "# build instructions\n",
        "Documentation/networking/packet_queue.rst": "Queue design\n",
        "Documentation/io_uring/overview.rst": "Literal underscore\n",
        "Documentation/ioxuring/overview.rst": "Different directory\n",
        "Documentation/100%/index.rst": "Literal percent\n",
        "Documentation/100more/index.rst": "Different directory\n",
    }
    for path, content in contents.items():
        full = tree / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    out = root / "study.db"
    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    return out


def test_structure_study_prefers_api_guides_to_bindings(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "usb_device").target
        matches = query.documentation_matches(conn, target, limit=2)
        assert [m.entry.path for m in matches] == [
            "Documentation/driver-api/usb/device.rst",
            "Documentation/usb/index.rst",
        ]
        assert "prose guide preferred for code study" in matches[0].reasons
        assert "claimed by target owner: USB STUDY" in matches[0].reasons
        assert query.documentation_for(conn, target, 2) == [m.entry for m in matches]


def test_symbol_name_supplies_evidence_without_a_maintainers_owner(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "packet_queue").target
        assert [e.path for e in query.documentation_for(conn, target)] == [
            "Documentation/networking/packet_queue.rst"
        ]


def test_scope_is_applied_before_limit_and_keeps_bindings_available(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "usb_device").target
        matches = query.documentation_matches(
            conn, target, 1, under="devicetree/bindings"
        )
        assert matches[0].entry.path.endswith("usb-device.yaml")
        exact = query.resolve(conn, matches[0].entry.path).target
        assert query.documentation_matches(conn, exact, 1)[0].entry.path == exact.path


@pytest.mark.parametrize("scope", ["io_uring", "100%"])
def test_scope_uses_literal_sql_prefix_and_zero_means_all(study_index, scope):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        entries = query.documentation_for(conn, target, 0, under=scope)
        assert len(entries) == 1
        assert entries[0].path.startswith(f"Documentation/{scope}/")


def test_scope_is_case_sensitive_and_accepts_an_exact_file(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        assert query.documentation_for(conn, target, under="USB") == []
        exact = "Documentation/usb/index.rst"
        assert [e.path for e in query.documentation_for(conn, target, under=exact)] == [
            exact
        ]


@pytest.mark.parametrize(
    "scope", ["", "../usb", "/tmp/usb", "usb/../net", "usb\\net", "C:usb"]
)
def test_unsafe_or_empty_scopes_are_rejected(scope):
    with pytest.raises(ValueError):
        documentation_scope(scope)


@pytest.mark.parametrize("limit", [-1, True, 1.5, "2"])
def test_library_rejects_invalid_limits(study_index, limit):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        with pytest.raises(ValueError, match="non-negative integer"):
            query.documentation_for(conn, target, limit)


def test_cli_explanations_are_optional_and_scopes_preserve_json(study_index, capsys):
    args = ["--db", str(study_index), "docs", "usb_device", "-n", "1", "-f", "json"]
    assert cli.main(args) == 0
    ordinary = json.loads(capsys.readouterr().out)
    assert "reasons" not in ordinary[0]
    assert cli.main([*args, "--explain", "--under", "devicetree/bindings"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained[0]["path"].endswith("usb-device.yaml")
    assert explained[0]["reasons"]
    assert explained[0]["index"] == "9.9"


def test_cli_human_explanations_and_local_source_next_step(study_index, capsys):
    assert (
        cli.main(
            ["--db", str(study_index), "docs", "usb_device", "--explain", "-n", "1"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "claimed by target owner: USB STUDY" in output
    assert " show Documentation/driver-api/usb/device.rst" in output


def test_cli_rejects_invalid_scope_without_opening_index(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--db", "nonexistent.db", "docs", "usb_device", "--under", "../usb"])
    assert stopped.value.code == 2
    assert "--under" in capsys.readouterr().err


def test_cli_reports_the_scope_when_no_documents_match(study_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(study_index), "docs", "usb_device", "--under", "missing"])
    assert "under Documentation/missing" in capsys.readouterr().err
