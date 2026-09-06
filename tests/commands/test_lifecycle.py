"""Version selection, index inventory, and diagnostic commands."""

from __future__ import annotations

import sqlite3

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.storage import config, kernelsrc

from .helpers import _fake_index


def test_use_pins_a_version_and_accepts_a_unique_prefix(home, capsys):
    assert config.get_default_version() is None
    assert cli.main(["use", "6.18"]) == 0
    assert config.get_default_version() == "6.18.45"
    out = capsys.readouterr().out
    assert "6.18.45" in out


def test_use_clear_and_both_args_rejected(home, capsys):
    cli.main(["use", "7.2"])
    assert cli.main(["use", "--clear"]) == 0
    assert config.get_default_version() is None
    assert "cleared pin on 7.2" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["use", "7.2", "--clear"])


def test_invalid_pin_is_a_clean_cli_error_and_use_clear_repairs_it(home, capsys):
    pin = config.default_version_file()
    pin.write_text("../untrusted\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        cli.main(["stats"])
    error = capsys.readouterr().err
    assert "cannot read the default version pin" in error
    assert "Traceback" not in error

    assert cli.main(["use", "--clear"]) == 0
    assert not pin.exists()
    assert "cleared invalid default pin" in capsys.readouterr().out


def test_stats_reports_parse_input_outcomes(mini_index, capsys):
    conn = sqlite3.connect(mini_index)
    parsed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE index_status='parsed'").fetchone()[0]
    conn.close()

    assert cli.main(["--db", str(mini_index), "stats"]) == 0
    report = " ".join(capsys.readouterr().out.split())
    for field in (f"Parsed C/H {parsed:,}", "Skipped 0", "Failed 0", "Oversized 0"):
        assert field in report

    assert cli.main([
        "--db", str(mini_index), "stats", "--format", "json",
    ]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["parse_inputs"] == {
        "parsed": parsed, "skipped": 0, "failed": 0, "oversized": 0,
    }


@pytest.mark.parametrize("payload", [
    b"{",
    b"[]",
    b'{"releases":[null]}',
])
def test_versions_turns_a_malformed_release_feed_into_one_line_error(
        monkeypatch, capsys, payload):
    from kernel_atlas.storage import kernelsrc

    monkeypatch.setattr(kernelsrc, "_get", lambda *args, **kwargs: payload)
    with pytest.raises(SystemExit):
        cli.main(["versions"])
    assert "could not reach kernel.org" in capsys.readouterr().err


def test_use_rejects_unknown_and_ambiguous_versions(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["use", "5.15"])
    assert "no index" in capsys.readouterr().err
    _fake_index(home, "6.12.104")
    with pytest.raises(SystemExit):
        cli.main(["use", "6"])
    assert "ambiguous" in capsys.readouterr().err


def test_use_rejects_an_unusable_index_before_pinning(home, capsys):
    broken = home / "indexes" / "broken.db"
    broken.write_bytes(b"")

    with pytest.raises(SystemExit):
        cli.main(["use", "broken"])
    assert config.get_default_version() is None
    assert "not a usable index" in capsys.readouterr().err


def test_use_does_not_pin_an_index_removed_while_waiting_for_its_lock(
        home, monkeypatch, capsys):
    from contextlib import contextmanager

    index = home / "indexes" / "7.2.db"

    @contextmanager
    def removed_before_lock(path):
        assert path == index
        path.unlink()
        yield

    monkeypatch.setattr(kernelsrc, "output_lock", removed_before_lock)
    with pytest.raises(SystemExit):
        cli.main(["use", "7.2"])
    assert config.get_default_version() is None
    assert "not a usable index" in capsys.readouterr().err


def test_index_status_surfaces_a_pinned_corrupt_index(home, capsys):
    broken = home / "indexes" / "broken.db"
    broken.write_bytes(b"")
    config.set_default_version("broken")

    assert cli.main(["indexes"]) == 0
    listing = capsys.readouterr().out
    assert "broken" in listing and "unusable:" in listing

    with pytest.raises(SystemExit):
        cli.main(["use"])
    err = capsys.readouterr().err
    assert "active index" in err and "not usable" in err


def test_version_prefix_is_component_aware():
    assert cli.version_prefix_match("6.18.45", "6.18")
    assert cli.version_prefix_match("6.18.45", "6")
    assert cli.version_prefix_match("6.18.45", "6.18.45")
    assert not cli.version_prefix_match("6.18.45", "6.1")
    assert not cli.version_prefix_match("6.18.45", "6.18.4")
    assert cli.version_prefix_match("7.2", "7")
    assert cli.version_prefix_match("next-20260101", "next")
    assert not cli.version_prefix_match("6.18.45", "")


def test_use_6_1_does_not_select_6_18(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["use", "6.1"])
    assert "no index" in capsys.readouterr().err


def test_default_index_is_the_pin_then_the_highest_version(home):
    assert cli.default_index().stem == "7.2"
    config.set_default_version("6.18.45")
    assert cli.default_index().stem == "6.18.45"
    (home / "indexes" / "6.18.45.db").unlink()
    # Stale pin falls back, and warn=False stays quiet.
    assert cli.default_index(warn=False).stem == "7.2"


def test_default_index_sorts_valid_aliases_by_recorded_kernel_version(
        mini_index, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    high_alias = indexes / "9.0.db"
    learning_alias = indexes / "learning.db"
    shutil.copy(mini_index, high_alias)
    shutil.copy(mini_index, learning_alias)
    conn = sqlite3.connect(learning_alias)
    conn.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    conn.commit()
    conn.close()

    assert cli.default_index(warn=False) == learning_alias


def test_default_index_prefers_a_usable_index_over_a_higher_corrupt_alias(
        mini_index, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    usable = indexes / "7.2.db"
    shutil.copy(mini_index, usable)
    conn = sqlite3.connect(usable)
    conn.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    conn.commit()
    conn.close()
    (indexes / "999.db").write_bytes(b"not a sqlite database")

    assert cli.default_index(warn=False) == usable


def test_indexes_marks_the_default(home, capsys):
    config.set_default_version("6.18.45")
    assert cli.main(["indexes"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "6.18.45" in ln]
    assert lines and lines[0].lstrip().startswith("*")


def test_indexes_reports_metadata_version_separately_from_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys):
    import json
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    shutil.copy(mini_index, indexes / "learning.db")

    assert cli.main(["indexes", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104"
    assert rows[0]["alias"] == "learning"


def test_stats_and_check_expose_call_occurrence_totals(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "stats"]) == 0
    assert "Call sites" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "check", "--format", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call_occurrences"] >= payload["calls"] > 0


def test_pin_selects_index_source_tree_and_locate_home(
        mini_index, mini_tree, tmp_path, monkeypatch, capsys):
    """Index selection changes URLs, but source lines use the recorded tree.

    The copies keep the fixture's meta.kernel_version (6.12.104); the filename
    is only a selection alias.  Links, headers, and source lines continue to
    describe the kernel version and exact tree recorded inside the index.
    """
    import json
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    (tmp_path / "indexes").mkdir()
    shutil.copy(mini_index, tmp_path / "indexes" / "6.18.45.db")
    shutil.copy(mini_index, tmp_path / "indexes" / "7.2.db")
    for ver, tag in (("6.18.45", "PINNED618"), ("7.2", "OTHER72")):
        dest = tmp_path / "kernels" / f"linux-{ver}"
        shutil.copytree(mini_tree, dest)
        tcp = dest / "net" / "ipv4" / "tcp.c"
        tcp.write_text(tcp.read_text(encoding="utf-8").replace(
            "return 0;", f"return 0; /* {tag} */", 1), encoding="utf-8")
    config.set_default_version("6.18.45")

    assert cli.main(["web", "tcp_sendmsg", "--url", "elixir"]) == 0
    url = capsys.readouterr().out
    assert "v6.12.104" in url
    assert "v7.2" not in url and "v6.18.45" not in url

    assert cli.main(["docs", "mm", "-f", "json"]) == 0
    docs = json.loads(capsys.readouterr().out)
    assert docs[0]["index"] == "6.12.104"
    assert "v6.12.104" in docs[0]["elixir"]

    assert cli.main(["show", "tcp_sendmsg", "--bare"]) == 0
    shown = capsys.readouterr().out
    assert "return 0;" in shown
    assert "PINNED618" not in shown and "OTHER72" not in shown

    assert cli.main(["path", "tcp_sendmsg"]) == 0
    assert str(mini_tree) in capsys.readouterr().out

    assert cli.main(["info", "tcp_sendmsg", "-f", "json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["index"] == "6.12.104"
    assert "v6.12.104" in info["links"]["elixir"]

    assert cli.main(["locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104" and rows[0]["active"]
    assert rows[1]["version"] == "6.12.104" and not rows[1]["active"]

    assert cli.main(["-K", "7.2", "show", "tcp_sendmsg", "--bare"]) == 0
    shown = capsys.readouterr().out
    assert "return 0;" in shown
    assert "PINNED618" not in shown and "OTHER72" not in shown

    assert cli.main(["-K", "7.2", "locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["version"] == "6.12.104" and rows[0]["active"]


def test_version_sort_places_final_after_rc_and_sorts_rc_numerically():
    from pathlib import Path

    versions = [Path("7.2-rc10.db"), Path("7.2.db"), Path("7.2-rc2.db")]
    got = [p.stem for p in sorted(versions, key=cli._version_key)]
    assert got == ["7.2-rc2", "7.2-rc10", "7.2"]


def test_version_sort_uses_numeric_base_for_vendor_versions():
    from pathlib import Path

    versions = [Path("6.1.db"), Path("6.6.12-acme+debug.db")]
    assert max(versions, key=cli._version_key).stem == "6.6.12-acme+debug"

    same_base = [Path("6.6.12.db"), Path("6.6.12-acme.db"),
                 Path("6.6.12-rc2.db")]
    assert [p.stem for p in sorted(same_base, key=cli._version_key)] == [
        "6.6.12-rc2", "6.6.12-acme", "6.6.12",
    ]


def test_check_reports_invalid_sqlite_value_types_without_a_traceback(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "blob-call.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute(
        "UPDATE calls SET callee=? WHERE rowid=(SELECT rowid FROM calls LIMIT 1)",
        (sqlite3.Binary(b"not-text"),))
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "check", "-f", "json"])
    error = capsys.readouterr().err
    assert "index table calls contains an invalid value" in error
    assert "Traceback" not in error
