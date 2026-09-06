"""Documentation command scopes, explanations, and follow-up guidance."""

import json

import pytest

from kernel_atlas.commands import cli


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
