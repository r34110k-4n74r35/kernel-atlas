"""Tests for index selection: `use`, `remove`, and default resolution."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from kernel_atlas import __version__, cli, config, db, kernelsrc


def _fake_index(root, version: str) -> None:
    d = root / "indexes"
    d.mkdir(parents=True, exist_ok=True)
    conn = db.create(d / f"{version}.db")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("schema_version", db.SCHEMA_VERSION),
        ("kernel_version", version),
        ("source", f"https://cdn.kernel.org/linux-{version}.tar.xz"),
        ("tree_path", str(root / "kernels" / f"linux-{version}")),
        ("built_at", "2026-01-01T00:00:00"),
        ("kinds", "function"),
        ("has_calls", "0"),
        ("n_dirs", "0"),
        ("n_files", "1"),
        ("n_symbols", "0"),
        ("n_type_aliases", "0"),
        ("n_type_members", "0"),
        ("n_subsystems", "0"),
        ("n_calls", "0"),
        ("n_call_occurrences", "0"),
        ("n_calls_resolved", "0"),
        ("n_calls_ambiguous", "0"),
        ("n_calls_macro", "0"),
        ("n_calls_indirect", "0"),
        ("n_calls_unresolved", "0"),
        ("n_parse_skipped", "0"),
        ("n_parse_failed", "0"),
        ("n_oversize", "0"),
        ("n_symlinks", "0"),
        ("build_seconds", "0"),
    ])
    db.finalize(conn)
    conn.close()


def _authorize_source(root, version: str, tree, *, index=None):
    source = f"https://cdn.kernel.org/linux-{version}.tar.xz"
    identity = kernelsrc._write_source_identity(
        version, tree, source, authoritative=True)
    index = index or root / "indexes" / f"{version}.db"
    if index.is_file():
        conn = sqlite3.connect(index)
        conn.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", [
            ("managed_tree_id", identity.token),
            ("managed_tree_device", str(identity.device)),
            ("managed_tree_inode", str(identity.inode)),
            ("managed_tree_digest", identity.digest),
        ])
        conn.commit()
        conn.close()
    return identity


def _fake_source(root, version: str, *, reports: str | None = None,
                 authorize: bool = True, index=None):
    tree = root / "kernels" / f"linux-{version}"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "MAINTAINERS").write_text("TEST\nF: *\n")
    parts = (reports or version).split(".")
    major, patch = parts[:2]
    sublevel = parts[2] if len(parts) > 2 else "0"
    (tree / "Makefile").write_text(
        f"VERSION = {major}\nPATCHLEVEL = {patch}\n"
        f"SUBLEVEL = {sublevel}\nEXTRAVERSION =\n",
    )
    if authorize:
        _authorize_source(root, version, tree, index=index)
    return tree


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    _fake_index(tmp_path, "6.18.45")
    _fake_index(tmp_path, "7.2")
    return tmp_path


def test_use_pins_a_version_and_accepts_a_unique_prefix(home, capsys):
    assert config.get_default_version() is None
    assert cli.main(["use", "6.18"]) == 0
    assert config.get_default_version() == "6.18.45"
    out = capsys.readouterr().out
    assert "6.18.45" in out


def test_remove_does_not_clear_a_concurrently_selected_different_pin(
        home, monkeypatch, capsys):
    config.set_default_version("7.2")
    original_get = config.get_default_version
    remove_read_pin = threading.Event()
    allow_remove_to_clear = threading.Event()
    results = []

    def delayed_get():
        if threading.current_thread().name == "remove-worker":
            value = original_get()
            remove_read_pin.set()
            assert allow_remove_to_clear.wait(2)
            return value
        return original_get()

    monkeypatch.setattr(config, "get_default_version", delayed_get)

    remove_worker = threading.Thread(
        name="remove-worker",
        target=lambda: results.append(cli.main(["remove", "7.2"])),
    )
    use_worker = threading.Thread(
        name="use-worker",
        target=lambda: results.append(cli.main(["use", "6.18.45"])),
    )
    remove_worker.start()
    assert remove_read_pin.wait(2)
    use_worker.start()
    assert use_worker.is_alive(), "the concurrent pin writer should wait"
    allow_remove_to_clear.set()
    remove_worker.join(2)
    use_worker.join(2)

    assert sorted(results) == [0, 0]
    assert original_get() == "6.18.45"
    capsys.readouterr()

    assert cli.main(["use"]) == 0
    out = capsys.readouterr().out
    assert "pinned: 6.18.45" in out
    assert "active index: 6.18.45" in out


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


@pytest.mark.parametrize(
    "command", ["versions", "build", "indexes", "use", "remove"])
def test_lifecycle_help_does_not_advertise_index_selection(command, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--kernel" not in help_text
    assert "--db" not in help_text
    assert "--color" in help_text


def test_top_level_version_reports_the_installed_implementation(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"{cli.PROG} {__version__}"


@pytest.mark.parametrize("argv", [
    ["--db", "", "stats"],
    ["stats", "--db", "   "],
    ["--kernel", "", "stats"],
    ["stats", "--kernel", "\t"],
    ["build", ""],
    ["build", "--src", ""],
    ["build", "--output", "   "],
    ["build", "--kinds", "\t"],
])
def test_empty_selectors_and_build_values_are_rejected_by_argparse(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    assert "must not be empty" in capsys.readouterr().err


@pytest.mark.parametrize(("command", "phrases"), [
    ("build", ["keep a downloaded source archive", "suppress download"]),
    ("find", ["complete, case-sensitive name", "name prefix"]),
    ("subsystems", ["only names matching", "sort key", "max subsystems"]),
    ("subsystem", ["max directory rows", "does not limit the --files list"]),
    ("info", ["maximum ownership matches", "ambiguous target candidates"]),
    ("tree", ["maximum directory depth"]),
])
def test_high_use_command_help_explains_its_options(command, phrases, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for phrase in phrases:
        assert phrase in help_text


def test_lifecycle_index_selection_after_subcommand_is_unrecognized(capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--db", "study.db"])
    assert "unrecognized arguments: --db" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--db", "one.db", "--kernel", "6.12", "stats"],
    ["--db", "one.db", "stats", "--kernel", "6.12"],
])
def test_index_selection_options_are_unambiguous(capsys, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "message"), [
    (["--kernel", "6.12", "indexes"], "does not apply"),
    (["indexes", "--db", "ignored.db"], "unrecognized arguments"),
    (["build", "--kernel", "6.12"], "unrecognized arguments"),
    (["--db", "ignored.db", "versions"], "does not apply"),
])
def test_index_selectors_are_rejected_where_they_have_no_effect(
        capsys, argv, message):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert message in capsys.readouterr().err


def test_stats_reports_parse_input_outcomes(mini_index, capsys):
    conn = sqlite3.connect(mini_index)
    parsed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE index_status='parsed'").fetchone()[0]
    conn.close()

    assert cli.main(["--db", str(mini_index), "stats"]) == 0
    assert f"parse inputs {parsed:,} parsed, 0 skipped, 0 failed" in (
        capsys.readouterr().out)

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
    from kernel_atlas import kernelsrc

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


def test_remove_deletes_the_index_and_clears_a_matching_pin(home, capsys):
    config.set_default_version("6.18.45")
    (home / "kernels" / "linux-6.18.45").mkdir(parents=True)
    (home / "kernels" / "linux-6.18.45" / "MAINTAINERS").write_text("x")

    assert cli.main(["remove", "6.18"]) == 0
    out = capsys.readouterr().out
    assert not (home / "indexes" / "6.18.45.db").is_file()
    assert (home / "kernels" / "linux-6.18.45").is_dir()
    assert "pinned default" in out
    assert "source kept" in out
    assert config.get_default_version() is None


def test_remove_with_source_and_duplicate_specs(home, capsys):
    _fake_source(home, "7.2")
    # Prefix + exact must not fail on the second name after the first delete.
    assert cli.main(["remove", "7.2", "7.2", "--source"]) == 0
    assert not (home / "indexes" / "7.2.db").is_file()
    assert not (home / "kernels" / "linux-7.2").exists()
    assert "removed source" in capsys.readouterr().out


def test_remove_source_failure_keeps_index_and_pin_for_a_retry(
        home, monkeypatch, capsys):
    from kernel_atlas import cli_lifecycle

    index = home / "indexes" / "7.2.db"
    tree = _fake_source(home, "7.2")
    config.set_default_version("7.2")
    monkeypatch.setattr(
        cli_lifecycle.shutil, "rmtree",
        lambda path: (_ for _ in ()).throw(PermissionError("tree is busy")),
    )

    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])

    assert index.is_file()
    marker = kernelsrc.source_identity_marker("7.2")
    assert marker is not None and marker.removing
    assert not tree.exists()
    assert kernelsrc.source_quarantine_path(marker).is_dir()
    assert config.get_default_version() == "7.2"
    error = capsys.readouterr().err
    assert "could not remove source" in error
    assert "correct the errors and retry" in error


def test_remove_source_finishes_before_deleting_its_authorizing_index(
        home, monkeypatch):
    tree = _fake_source(home, "7.2")
    original = cli._unlink_index

    def checked_unlink(path):
        assert not tree.exists()
        return original(path)

    monkeypatch.setattr(cli, "_unlink_index", checked_unlink)
    assert cli.main(["remove", "7.2", "--source"]) == 0


def test_remove_source_retry_survives_index_unlink_failure(
        home, monkeypatch, capsys):
    index = home / "indexes" / "7.2.db"
    tree = _fake_source(home, "7.2")
    original = cli._unlink_index

    def fail(path):
        raise PermissionError("database is in use")

    monkeypatch.setattr(cli, "_unlink_index", fail)
    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])
    assert index.is_file() and not tree.exists()
    marker = kernelsrc.source_identity_marker("7.2")
    assert marker is not None and marker.removing
    capsys.readouterr()

    monkeypatch.setattr(cli, "_unlink_index", original)
    assert cli.main(["remove", "7.2", "--source"]) == 0
    assert not index.exists()
    assert kernelsrc.source_identity_marker("7.2") is None


def test_remove_two_indexes_authorizing_the_same_source_in_one_batch(
        home, capsys):
    import shutil

    tree = _fake_source(home, "7.2")
    standard = home / "indexes" / "7.2.db"
    study = home / "indexes" / "study.db"
    shutil.copyfile(standard, study)

    assert cli.main(["remove", "7.2", "study", "--source"]) == 0
    assert not tree.exists()
    assert not standard.exists() and not study.exists()
    assert kernelsrc.source_identity_marker("7.2") is None
    out = capsys.readouterr().out
    assert "removed source" in out
    assert "source already removed" in out


def test_remove_index_failure_is_nonzero_and_preserves_its_pin(
        home, monkeypatch, capsys):
    index = home / "indexes" / "7.2.db"
    config.set_default_version("7.2")

    def fail(path):
        raise PermissionError("database is in use")

    monkeypatch.setattr(cli, "_unlink_index", fail)
    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2"])

    assert index.is_file()
    assert config.get_default_version() == "7.2"
    assert "could not remove index" in capsys.readouterr().err


def test_remove_rechecks_source_authorization_after_acquiring_output_lock(
        home, monkeypatch, capsys):
    from contextlib import contextmanager

    from kernel_atlas import kernelsrc

    index = home / "indexes" / "7.2.db"
    managed = _fake_source(home, "7.2")
    replacement_tree = home / "someone-elses-tree"

    @contextmanager
    def changed_while_waiting(path):
        assert path == index
        writer = sqlite3.connect(index)
        writer.execute(
            "UPDATE meta SET value=? WHERE key='tree_path'",
            (str(replacement_tree),),
        )
        writer.commit()
        writer.close()
        yield

    monkeypatch.setattr(kernelsrc, "output_lock", changed_while_waiting)

    assert cli.main(["remove", "7.2", "--source"]) == 0
    assert managed.is_dir()
    assert not index.exists()
    assert "index changed" in capsys.readouterr().out


def test_remove_source_refuses_a_replacement_tree_at_the_recorded_path(
        home, capsys):
    index = home / "indexes" / "7.2.db"
    replacement = _fake_source(home, "7.2", reports="9.9")
    notes = replacement / "personal-notes"
    notes.write_text("keep this\n")
    config.set_default_version("7.2")

    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])

    assert notes.read_text() == "keep this\n"
    assert index.is_file()
    assert config.get_default_version() == "7.2"
    error = capsys.readouterr().err
    assert "not the pristine tool-owned source" in error
    assert "index kept" in error


def test_remove_source_refuses_same_version_replacement_with_new_identity(
        home, capsys):
    import shutil

    index = home / "indexes" / "7.2.db"
    original = _fake_source(home, "7.2")
    shutil.rmtree(original)
    replacement = _fake_source(home, "7.2", authorize=False)
    notes = replacement / "personal-notes"
    notes.write_text("keep this\n")

    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])

    assert notes.read_text() == "keep this\n"
    assert index.is_file()
    assert "not the pristine tool-owned source" in capsys.readouterr().err


def test_partial_source_removal_can_resume_with_the_same_nonce(
        home, monkeypatch, capsys):
    import shutil

    from kernel_atlas import cli_lifecycle

    index = home / "indexes" / "7.2.db"
    tree = _fake_source(home, "7.2")
    original_rmtree = shutil.rmtree

    def partial(path):
        (path / "MAINTAINERS").unlink()
        (path / "Makefile").unlink()
        raise PermissionError("tree became busy")

    monkeypatch.setattr(cli_lifecycle.shutil, "rmtree", partial)
    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])
    assert index.is_file() and not tree.exists()
    marker = kernelsrc.source_identity_marker("7.2")
    assert marker is not None and marker.removing
    assert kernelsrc.source_quarantine_path(marker).is_dir()
    capsys.readouterr()

    monkeypatch.setattr(cli_lifecycle.shutil, "rmtree", original_rmtree)
    assert cli.main(["remove", "7.2", "--source"]) == 0
    assert not index.exists() and not tree.exists()
    assert kernelsrc.source_identity_marker("7.2") is None


def test_source_retry_never_deletes_a_new_conventional_tree(
        home, monkeypatch, capsys):
    import shutil

    from kernel_atlas import cli_lifecycle

    index = home / "indexes" / "7.2.db"
    tree = _fake_source(home, "7.2")
    original_rmtree = shutil.rmtree
    monkeypatch.setattr(
        cli_lifecycle.shutil, "rmtree",
        lambda path: (_ for _ in ()).throw(PermissionError("busy")),
    )
    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])
    marker = kernelsrc.source_identity_marker("7.2")
    assert marker is not None and marker.removing
    assert kernelsrc.source_quarantine_path(marker).is_dir()

    tree.mkdir()
    notes = tree / "new-research.txt"
    notes.write_text("created after the failed removal\n")
    monkeypatch.setattr(cli_lifecycle.shutil, "rmtree", original_rmtree)
    capsys.readouterr()

    assert cli.main(["remove", "7.2", "--source"]) == 0
    assert notes.read_text() == "created after the failed removal\n"
    assert not index.exists()


def test_source_entry_swap_before_quarantine_is_preserved(
        home, monkeypatch, tmp_path, capsys):
    tree = _fake_source(home, "7.2")
    index = home / "indexes" / "7.2.db"
    original_tree = tmp_path / "original-tool-tree"
    victim = tmp_path / "personal-tree"
    victim.mkdir()
    (victim / "notes").write_text("keep this\n")
    actual_rename = kernelsrc._rename_noreplace
    raced = False

    def swapping_rename(source, destination):
        nonlocal raced
        if Path(source) == tree and not raced:
            tree.rename(original_tree)
            victim.rename(tree)
            raced = True
        actual_rename(source, destination)

    monkeypatch.setattr(kernelsrc, "_rename_noreplace", swapping_rename)

    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])

    assert raced
    assert (tree / "notes").read_text() == "keep this\n"
    assert original_tree.is_dir()
    assert index.is_file()
    assert "nothing was deleted" in capsys.readouterr().err


def test_remove_source_preserves_a_replacement_symlink_and_its_target(
        home, tmp_path, capsys):
    index = home / "indexes" / "7.2.db"
    link = home / "kernels" / "linux-7.2"
    original = _fake_source(home, "7.2")
    import shutil
    shutil.rmtree(original)
    victim = tmp_path / "personal-tree"
    victim.mkdir()
    notes = victim / "notes"
    notes.write_text("keep this\n")
    try:
        link.symlink_to(victim, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(SystemExit):
        cli.main(["remove", "7.2", "--source"])
    assert link.is_symlink()
    assert notes.read_text() == "keep this\n"
    assert index.exists()
    assert "index kept" in capsys.readouterr().err


def test_remove_exact_dangling_index_alias_unlinks_the_alias(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    alias = config.index_path("dangling")
    alias.parent.mkdir()
    try:
        alias.symlink_to(tmp_path / "missing" / "study.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert cli.main(["remove", "dangling"]) == 0
    assert not alias.is_symlink()
    assert "removed index" in capsys.readouterr().out


def test_remove_source_uses_recorded_version_not_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    alias_index = indexes / "alias.db"
    shutil.copy(mini_index, alias_index)
    managed = _fake_source(
        tmp_path, "6.12.104", authorize=False)
    unrelated = tmp_path / "kernels" / "linux-alias"
    unrelated.mkdir()
    conn = sqlite3.connect(alias_index)
    conn.executemany("UPDATE meta SET value=? WHERE key=?", [
        (str(managed), "tree_path"),
        ("https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.104.tar.xz",
         "source"),
    ])
    conn.commit()
    conn.close()
    _authorize_source(tmp_path, "6.12.104", managed, index=alias_index)

    assert cli.main(["remove", "alias", "--source"]) == 0
    assert not alias_index.exists()
    assert not managed.exists()
    assert unrelated.is_dir()
    assert "removed source" in capsys.readouterr().out


def test_invalid_metadata_only_database_cannot_authorize_source_removal(
        tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    managed = tmp_path / "kernels" / "linux-9.9"
    managed.mkdir(parents=True)
    forged = tmp_path / "forged.db"
    conn = sqlite3.connect(forged)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?,?)", [
        ("schema_version", db.SCHEMA_VERSION),
        ("kernel_version", "9.9"),
        ("tree_path", str(managed)),
    ])
    conn.commit()
    conn.close()

    assert cli._managed_source_recorded_by(forged) is None
    assert managed.is_dir()


def test_remove_source_never_deletes_a_custom_tree_at_the_cache_path(
        mini_index, tmp_path, monkeypatch, capsys):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    custom_index = indexes / "custom.db"
    shutil.copy(mini_index, custom_index)
    custom_tree = tmp_path / "kernels" / "linux-6.12.104"
    custom_tree.mkdir(parents=True)
    conn = sqlite3.connect(custom_index)
    conn.executemany("UPDATE meta SET value=? WHERE key=?", [
        (str(custom_tree), "tree_path"), (str(custom_tree), "source")])
    conn.commit()
    conn.close()

    assert cli.main(["remove", "custom", "--source"]) == 0
    assert custom_tree.is_dir()
    assert "source kept" in capsys.readouterr().out


def test_remove_rejects_path_shaped_versions_without_deleting(home, capsys):
    before = set((home / "indexes").iterdir())
    with pytest.raises(SystemExit):
        cli.main(["remove", "../7.2"])
    assert "unsafe kernel version" in capsys.readouterr().err
    assert set((home / "indexes").iterdir()) == before


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


def test_pin_roundtrip_in_config(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert config.get_default_version() is None
    config.set_default_version("6.1")
    assert config.get_default_version() == "6.1"
    config.clear_default_version()
    assert config.get_default_version() is None


def test_negative_limit_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["siblings", "mm", "-n", "-1"])
    err = capsys.readouterr().err
    assert ">= 0" in err or "invalid" in err.lower()


@pytest.mark.parametrize("command", [
    ["ls", "fs", "-n"],
    ["find", "ext4", "-n"],
    ["tree", "fs", "-d"],
])
def test_sqlite_bound_counts_reject_unreasonably_large_values_cleanly(
        mini_index, capsys, command):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), *command,
            str(cli._MAX_CLI_COUNT + 1),
        ])
    error = capsys.readouterr().err
    assert f"<= {cli._MAX_CLI_COUNT}" in error
    assert "Traceback" not in error


def test_build_jobs_have_a_rational_upper_bound(capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--jobs", str(cli._MAX_JOBS + 1)])
    assert f"<= {cli._MAX_JOBS}" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "message"), [
    (",", "at least one"),
    ("function,function", "duplicate symbol kind"),
])
def test_build_rejects_empty_or_duplicate_kind_lists_before_fetching(
        value, message, capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--kinds", value])
    assert message in capsys.readouterr().err


def test_huge_target_line_is_a_clean_error(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  "fs/ext4/inode.c:" + "9" * 100])
    assert "too large" in capsys.readouterr().err


def test_find_rejects_size_sort_for_symbols(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "find", "ext4", "--sort", "size"])
    assert "use --sort lines" in capsys.readouterr().err


@pytest.mark.parametrize("command", [
    ["siblings", "ext4_bmap", "--sort", "size"],
    ["ls", "fs/ext4/inode.c", "--sort", "size"],
    ["calls", "fs/ext4/inode.c:ext4_bmap", "--sort", "size"],
])
def test_every_symbol_only_listing_rejects_size_sort(
        mini_index, capsys, command):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), *command])
    assert "use --sort lines" in capsys.readouterr().err


def test_info_omits_the_rest_and_includes_links(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "info", "mm", "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [s["name"] for s in data["subsystems"]]
    assert "THE REST" not in names
    assert "MEMORY MANAGEMENT" in names
    assert "elixir.bootlin.com" in data["links"]["elixir"]
    assert "is_static" not in data["target"]


def test_stats_and_check_expose_call_occurrence_totals(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "stats"]) == 0
    assert "call sites" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "check", "--format", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["call_occurrences"] >= payload["calls"] > 0


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


def test_info_and_path_distinguish_a_missing_recorded_source_member(
        mini_index, mini_tree, tmp_path, capsys):
    import json
    import shutil

    tree = tmp_path / "linux-copy"
    shutil.copytree(mini_tree, tree)
    copied = tmp_path / "missing-member.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    writer.commit()
    writer.close()
    (tree / "mm/page_alloc.c").unlink()

    assert cli.main(["--db", str(copied), "info", "mm/page_alloc.c",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source_path"] == str(tree / "mm/page_alloc.c")
    assert data["source_exists"] is False

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "path", "mm/page_alloc.c"])
    assert "missing from the source tree" in capsys.readouterr().err


def test_cli_normalizes_only_absolute_targets_inside_the_recorded_tree(
        mini_index, mini_tree, tmp_path, capsys):
    import json

    source = mini_tree / "fs/ext4/inode.c"
    assert cli.main(["--db", str(mini_index), "info", str(source),
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["target"]["path"] == "fs/ext4/inode.c"

    assert cli.main(["--db", str(mini_index), "locate", str(source),
                     "-f", "json"]) == 0
    located = json.loads(capsys.readouterr().out)
    assert located[0]["found"] and located[0]["path"] == "fs/ext4/inode.c"

    assert cli.main(["--db", str(mini_index), "info", f"{source}:3",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["target"]["name"] == "ext4_inode_blocks_set"

    outside = tmp_path / "inode.c"
    outside.write_text("not the indexed file\n")
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  f"{outside}:ext4_bmap"])
    assert "outside the recorded source tree" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  "wrong/place/inode.c:ext4_bmap"])
    assert "nothing in the index matches" in capsys.readouterr().err


def test_absolute_target_normalization_preserves_an_indexed_symlink_leaf(
        tmp_path):
    tree = tmp_path / "linux-9.9"
    target = tree / "Documentation/process/changes.rst"
    target.parent.mkdir(parents=True)
    target.write_text("changes\n")
    (tree / "MAINTAINERS").write_text("TEST\nF: Documentation/\n")
    link = tree / "Documentation/Changes"
    try:
        link.symlink_to("process/changes.rst")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    normalized = cli._normalize_target_spec(
        {"tree_path": str(tree), "kernel_version": "9.9"}, str(link))
    assert normalized == "Documentation/Changes"


def test_web_and_docs_commands(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "web", "tcp_sendmsg",
                     "--url", "elixir"]) == 0
    url = capsys.readouterr().out.strip()
    assert "elixir.bootlin.com" in url and "tcp.c" in url

    assert cli.main(["--db", str(mini_index), "docs", "fs/ext4"]) == 0
    out = capsys.readouterr().out
    assert "Documentation/filesystems/ext4/about.rst" in out

    assert cli.main(["--db", str(mini_index), "docs", "mm"]) == 0
    out = capsys.readouterr().out
    assert "Documentation/mm/page_alloc.rst" in out
    assert "using mm/" not in out


def test_web_rejects_links_for_a_custom_source_index(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "custom-source.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute(
        "UPDATE meta SET value=? WHERE key='source'",
        (str(tmp_path / "vendor-linux"),),
    )
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(copied), "web", "tcp_sendmsg", "--url", "elixir",
        ])
    error = capsys.readouterr().err
    assert "no upstream release-reference URLs" in error
    assert "use 'path' or 'show'" in error
    assert "Traceback" not in error

    for command in (
            ["info", "tcp_sendmsg"],
            ["struct", "ext4_sb_info"],
            ["docs", "mm"]):
        assert cli.main(["--db", str(copied), *command]) == 0
        output = capsys.readouterr().out
        assert f"{cli.PROG} --db {copied.resolve()} web " not in output


def test_docs_bare_name_picks_the_area_directory_not_a_symbol(mini_index, capsys):
    """`mm` is both the top-level directory and arch/x86/mm/."""
    from kernel_atlas.cli import _resolve_area
    from kernel_atlas import db
    conn = db.connect(mini_index, readonly=True)
    t = _resolve_area(conn, "mm").target
    conn.close()
    assert t.kind == "dir" and t.path == "mm"


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


def test_subsystem_json_omits_files_unless_asked(mini_index, capsys):
    import json
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "files" not in data
    assert data["index"] == "6.12.104"
    assert data["directories"]
    assert data["directories"][0]["primary_files"] > 0
    assert cli.main(["--db", str(mini_index), "subsystem", "EXT4 FILE SYSTEM",
                     "-f", "json", "--files"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(p.endswith("inode.c") for p in data["files"])

    assert cli.main(["--db", str(mini_index), "subsystems", "-n", "1",
                     "-f", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["index"] == "6.12.104"


def test_relationships_reports_identity_aware_call_flow(mini_index, capsys):
    import json

    assert cli.main([
        "--db", str(mini_index), "relationships", "fs/ext4",
        "--include-internal", "--direction", "outgoing", "-f", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["subsystem"]["name"] == "EXT4 FILE SYSTEM"
    assert payload["subsystem"]["primary_files"] > 0
    assert payload["call_graph_available"] is True
    assert payload["outgoing_call_resolution"]["total"] >= 3
    assert payload["outgoing_call_resolution"]["resolved"] >= 3
    assert payload["call_flows"] == [{
        "direction": "outgoing",
        "subsystem": "EXT4 FILE SYSTEM",
        "edges": 3,
        "callers": 3,
        "callees": 3,
        "source_files": 2,
        "target_files": 2,
        "internal": True,
        "unclassified": False,
    }]


def test_relationships_csv_is_a_stable_machine_view(mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "rels", "EXT4 FILE SYSTEM",
        "--include-internal", "--direction", "outgoing", "--via", "calls",
        "-f", "csv",
    ]) == 0
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert rows[0]["relationship"] == "call"
    assert rows[0]["selected_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["source_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["target_subsystem"] == "EXT4 FILE SYSTEM"
    assert rows[0]["edges"] == "3"


def test_relationships_rejects_ambiguous_and_mixed_targets(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "super.c"])
    assert "ambiguous" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs"])
    assert "mixed ownership" in capsys.readouterr().err


def test_relationships_rejects_options_without_effect(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs/ext4",
                  "--via", "ownership", "--direction", "incoming"])
    assert "apply only to call" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "relationships", "fs/ext4",
                  "--via", "calls", "--min-shared", "2"])
    assert "applies only to ownership" in capsys.readouterr().err


def test_locate_lists_every_built_index(mini_index, tmp_path, monkeypatch, capsys):
    import json
    import shutil
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    d = tmp_path / "indexes"
    d.mkdir()
    shutil.copy(mini_index, d / "6.12.104.db")
    shutil.copy(mini_index, d / "7.2.db")
    renamed = sqlite3.connect(d / "7.2.db")
    renamed.execute("UPDATE meta SET value='7.2' WHERE key='kernel_version'")
    renamed.commit()
    renamed.close()
    assert cli.main(["locate", "tcp_sendmsg", "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    versions = {r["version"] for r in rows}
    assert versions == {"6.12.104", "7.2"}
    assert all(r["found"] and r["path"].endswith("tcp.c") for r in rows)
    # No pin: the highest built version is the default and is listed first.
    assert rows[0]["version"] == "7.2" and rows[0]["active"]
    assert rows[1]["version"] == "6.12.104" and not rows[1]["active"]


def test_locate_does_not_turn_a_failed_line_selector_into_a_file(
        mini_index, capsys):
    import json

    target = "fs/ext4/inode.c:9999"
    assert cli.main([
        "--db", str(mini_index), "locate", target, "--format", "json",
    ]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["found"] is False
    assert "no symbol spans line 9999" in row["note"]

    assert cli.main(["--db", str(mini_index), "locate", target]) == 0
    output = capsys.readouterr().out
    assert "no symbol spans line 9999" in output


def test_locate_table_exposes_ambiguous_resolution_notes(mini_index, capsys):
    assert cli.main(["--db", str(mini_index), "locate", "super.c"]) == 0
    output = capsys.readouterr().out
    assert "fs/ext4/super.c" in output
    assert "2 files named 'super.c'" in output


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


def test_subsystem_ambiguity_is_valid_json(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "subsystem", "SYSTEM",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ambiguous"] is True
    assert len(data["matches"]) > 1
    assert data["index"] == "6.12.104"


def test_casefold_colliding_subsystem_names_are_never_chosen_arbitrarily(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "casefold.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    next_id = writer.execute("SELECT MAX(id)+1 FROM subsystems").fetchone()[0]
    writer.executemany("INSERT INTO subsystems(id,name) VALUES (?,?)", [
        (next_id, "FOO"), (next_id + 1, "foo")])
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "subsystem", "FoO",
                     "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ambiguous"] is True
    assert {row["name"] for row in data["matches"]} == {"FOO", "foo"}

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "relationships", "FoO"])
    assert "ambiguous under case-insensitive" in capsys.readouterr().err


def test_subsystem_no_match_hint_preserves_selector_and_shell_quotes(
        mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "subsystem", "not here"])
    error = capsys.readouterr().err
    assert f"--db {mini_index.resolve()}" in error
    assert "--grep 'not here'" in error


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
    from kernel_atlas import query

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


def test_mutually_exclusive_flags_are_rejected_by_parser(capsys):
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--exact", "--glob"])
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--static-only", "--no-static"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_calls_rejects_non_function_targets(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "S_IRWXU"])
    assert "not a function or syscall" in capsys.readouterr().err


def test_no_call_graph_rebuild_hints_target_the_selected_custom_database(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "without-calls.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.commit()
    tree = writer.execute(
        "SELECT value FROM meta WHERE key='tree_path'").fetchone()[0]
    writer.close()

    for command in (
            ["calls", "ext4_bmap"],
            ["relationships", "EXT4 FILE SYSTEM", "--via", "calls"]):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), *command])
        error = capsys.readouterr().err
        assert "--with-calls --force" in error
        assert f"--output {copied.resolve()}" in error
        assert f"--src {tree}" in error


@pytest.mark.parametrize("pinned", [False, True])
def test_no_call_graph_rebuild_hint_preserves_a_selected_filename_alias(
        mini_index, tmp_path, monkeypatch, capsys, pinned):
    import shutil

    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    selected = indexes / "study.db"
    shutil.copy(mini_index, selected)
    writer = sqlite3.connect(selected)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    tree = writer.execute(
        "SELECT value FROM meta WHERE key='tree_path'").fetchone()[0]
    writer.commit()
    writer.close()

    selector = [] if pinned else ["-K", "study"]
    if pinned:
        config.set_default_version("study")
    with pytest.raises(SystemExit):
        cli.main([*selector, "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert f"--output {selected.resolve()}" in error
    assert f"--src {tree}" in error
    assert "build 6.12.104" in error

    assert cli.main([*selector, "info", "mm"]) == 0
    output = capsys.readouterr().out
    if pinned:
        assert f"Next:  {cli.PROG} siblings mm" in output
    else:
        assert f"Next:  {cli.PROG} -K study siblings mm" in output


def test_no_call_graph_advice_does_not_replace_missing_custom_source(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "missing-custom-source.db"
    shutil.copy(mini_index, copied)
    missing = tmp_path / "vendor-tree-that-was-removed"
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.executemany("UPDATE meta SET value=? WHERE key=?", [
        (str(missing), "tree_path"), (str(missing), "source")])
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "restore the recorded custom source tree" in error
    assert "--src" not in error


def test_no_call_graph_advice_can_refetch_missing_downloaded_source(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "missing-downloaded-source.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute("DELETE FROM calls")
    writer.execute(
        "UPDATE meta SET value='0' WHERE key='has_calls'"
        " OR key LIKE 'n_calls%' OR key='n_call_occurrences'")
    writer.execute("UPDATE meta SET value=? WHERE key='tree_path'",
                   (str(tmp_path / "removed-cache"),))
    writer.commit()
    writer.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "--with-calls --force" in error
    assert f"--output {copied.resolve()}" in error
    assert "--src" not in error


@pytest.mark.parametrize(
    "kind", ["struct", "macro", "variable", "file", "function,file"])
def test_calls_rejects_result_kinds_that_can_never_occur(
        mini_index, kind, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "calls", "ext4_bmap",
                  "--kinds", kind])
    assert "only lists function and syscall" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["show", "path", "web"])
def test_source_identity_commands_reject_unmatched_line_selector(
        mini_index, command, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), command,
                  "fs/ext4/inode.c:9999"])
    assert "no symbol spans line 9999" in capsys.readouterr().err


@pytest.mark.parametrize("command", [
    ["path", "super.c"],
    ["show", "super.c", "--bare"],
    ["web", "super.c", "--url", "elixir"],
])
def test_source_identity_commands_reject_ambiguous_bare_files(
        mini_index, command, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), *command])
    error = capsys.readouterr().err
    assert "2 files" in error
    assert "fs/ext4/super.c" in error
    assert "fs/btrfs/super.c" in error


@pytest.mark.parametrize("line", ["1", "01", "+01"])
@pytest.mark.parametrize("command", [
    ["path"],
    ["show", "--bare"],
    ["web", "--url", "elixir"],
    ["calls"],
])
def test_concrete_commands_reject_ambiguous_basename_line_selectors(
        mini_index, line, command, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), command[0], f"super.c:{line}",
            *command[1:],
        ])
    error = capsys.readouterr().err
    assert "2 files named 'super.c'" in error
    assert "full indexed path:line" in error
    assert f"fs/ext4/super.c:{line}" in error
    assert f"fs/btrfs/super.c:{line}" in error


def test_struct_rejects_an_ambiguous_basename_line_selector(
        mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "struct", "super.c:20"])
    error = capsys.readouterr().err
    assert "2 files named 'super.c'" in error
    assert "fs/ext4/super.c:20" in error
    assert "fs/btrfs/super.c:20" in error


def test_full_path_line_selectors_remain_exact_for_concrete_commands(
        mini_index, capsys):
    assert cli.main([
        "--db", str(mini_index), "path", "fs/btrfs/super.c:1", "--line",
    ]) == 0
    assert "fs/btrfs/super.c:1" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "show", "fs/btrfs/super.c:1", "--bare",
    ]) == 0
    assert "btrfs_mount" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "web", "fs/btrfs/super.c:1",
        "--url", "elixir",
    ]) == 0
    assert "fs/btrfs/super.c#L1" in capsys.readouterr().out

    assert cli.main([
        "--db", str(mini_index), "calls", "fs/btrfs/super.c:1",
        "--format", "json",
    ]) == 0
    assert capsys.readouterr().out.strip() == "[]"

    assert cli.main([
        "--db", str(mini_index), "struct", "fs/ext4/super.c:20",
        "--format", "json",
    ]) == 0
    assert '"name": "ext4_sb_info"' in capsys.readouterr().out


def test_numeric_directory_name_is_never_mistaken_for_a_line_selector(
        mini_index, mini_tree, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "numeric-directory.db"
    copied_tree = tmp_path / "linux-6.12.104"
    shutil.copy(mini_index, copied)
    shutil.copytree(mini_tree, copied_tree)
    (copied_tree / "drivers/8250").mkdir()

    writer = sqlite3.connect(copied)
    writer.execute(
        "INSERT INTO dirs(path,name,parent_id,depth,n_files,n_subdirs,"
        " n_files_recursive) VALUES ('drivers/8250','8250',"
        " (SELECT id FROM dirs WHERE path='drivers'),2,0,0,0)"
    )
    writer.execute(
        "UPDATE dirs SET n_subdirs=n_subdirs+1 WHERE path='drivers'"
    )
    writer.execute(
        "UPDATE meta SET value=printf('%d',CAST(value AS INTEGER)+1)"
        " WHERE key='n_dirs'"
    )
    writer.execute(
        "UPDATE meta SET value=? WHERE key='tree_path'", (str(copied_tree),)
    )
    writer.commit()
    writer.close()

    assert cli.main(["--db", str(copied), "locate", "8250", "-f", "json"]) == 0
    located = json.loads(capsys.readouterr().out)[0]
    assert located["found"] is True
    assert located["kind"] == "dir"
    assert located["path"] == "drivers/8250"

    assert cli.main(["--db", str(copied), "path", "8250"]) == 0
    assert capsys.readouterr().out.strip().endswith("drivers/8250")

    assert cli.main([
        "--db", str(copied), "web", "8250", "--url", "elixir",
    ]) == 0
    assert "/source/drivers/8250" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "show", "8250"])
    error = capsys.readouterr().err
    assert "is a directory" in error
    assert "spans line 8250" not in error


def test_calls_output_exposes_mixed_occurrence_counts(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "mixed-call-output.db"
    shutil.copy(mini_index, copied)
    writer = sqlite3.connect(copied)
    writer.execute(
        "UPDATE calls SET direct_count=2,indirect_count=1,macro_count=1"
        " WHERE callee='ext4_get_block'"
    )
    writer.execute(
        "UPDATE meta SET value=(SELECT CAST(SUM(direct_count+indirect_count+"
        " macro_count) AS TEXT) FROM calls) WHERE key='n_call_occurrences'"
    )
    writer.commit()
    writer.close()

    assert cli.main([
        "--db", str(copied), "calls", "ext4_bmap", "--format", "json",
    ]) == 0
    outgoing = json.loads(capsys.readouterr().out)[0]
    assert outgoing["direct_count"] == 2
    assert outgoing["indirect_count"] == 1
    assert outgoing["macro_count"] == 1

    assert cli.main([
        "--db", str(copied), "calls", "ext4_bmap",
    ]) == 0
    table = capsys.readouterr().out
    assert "OCCURRENCES" in table
    assert "2d 1i 1m" in table

    assert cli.main([
        "--db", str(copied), "calls", "ext4_get_block", "--callers",
        "--format", "json",
    ]) == 0
    incoming = json.loads(capsys.readouterr().out)[0]
    assert incoming["name"] == "ext4_bmap"
    assert incoming["direct_count"] == 2
    assert incoming["indirect_count"] == 1
    assert incoming["macro_count"] == 1

    assert cli.main([
        "--db", str(copied), "calls", "ext4_get_block", "--callers",
    ]) == 0
    assert "2d 1i 1m" in capsys.readouterr().out


def test_calls_ambiguity_recommends_line_for_same_file_definitions(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "conditional.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_bmap'"
    ).fetchone()
    second_line = row[3] + 100
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], second_line, row[4] + 100, *row[5:]))
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        cli.main(["--db", str(copied), "calls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert "path:line" in error
    assert "fs/ext4/inode.c:" in error

    for command in ("show", "path", "web"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command,
                      "fs/ext4/inode.c:ext4_bmap"])
        assert "path:line" in capsys.readouterr().err

    assert cli.main(["--db", str(copied), "info",
                     f"fs/ext4/inode.c:{second_line}"]) == 0
    output = capsys.readouterr().out
    assert f"siblings fs/ext4/inode.c:{second_line}" in output


def test_relationships_accepts_ambiguous_definitions_with_one_owner(
        mini_index, tmp_path, capsys):
    import json
    import shutil

    copied = tmp_path / "same-owner.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_bmap'"
    ).fetchone()
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], row[3] + 100, row[4] + 100, *row[5:]))
    conn.commit()
    conn.close()

    assert cli.main(["--db", str(copied), "relationships", "ext4_bmap",
                     "--via", "ownership", "-f", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subsystem"]["name"] == "EXT4 FILE SYSTEM"
    assert "all 2 matches" in payload["resolved_from"]


def test_trace_does_not_treat_macros_as_stack_frames(mini_index, capsys):
    import json

    assert cli.main(["--db", str(mini_index), "trace", "S_IRWXU", "tcp_sendmsg",
                     "-f", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["frame"] == "S_IRWXU" and rows[0]["found"] is False
    assert rows[1]["frame"] == "tcp_sendmsg" and rows[1]["found"] is True


def test_linkage_filters_on_path_only_listings_are_rejected(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "mm", "--exported"])
    assert "only applies to symbols" in capsys.readouterr().err


def test_path_and_show_reject_target_inapplicable_flags(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "path", "mm/page_alloc.c", "--line"])
    assert "only applies to symbols" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "show", "__alloc_pages", "--lines", "1"])
    assert "applies to files" in capsys.readouterr().err


def test_correction_hints_preserve_the_explicit_database_selector(
        mini_index, capsys):
    selected = str(mini_index.resolve())

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert f"--db {selected}" in error
    assert "siblings fs/ext4/inode.c:" in error

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "show", "fs/ext4"])
    error = capsys.readouterr().err
    assert f"--db {selected}" in error
    assert " ls fs/ext4" in error


@pytest.mark.parametrize("line_range", ["0", "9" * 5000])
def test_show_rejects_invalid_numeric_line_ranges(
        mini_index, line_range, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), "show", "Makefile", "--lines", line_range,
        ])
    assert "--lines" in capsys.readouterr().err


def test_find_and_calls_filter_before_applying_limit(
        mini_index, tmp_path, capsys):
    import shutil

    copied = tmp_path / "many.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    file_id = conn.execute(
        "SELECT id FROM files WHERE path='fs/ext4/inode.c'").fetchone()[0]
    caller_id = conn.execute(
        "SELECT id FROM symbols WHERE name='ext4_bmap'").fetchone()[0]
    symbols = [
        (file_id, f"needle_{i:02d}", "function", 100 + i, 100 + i,
         None, 0, 0, 0)
        for i in range(25)
    ]
    symbols.append((file_id, "needle_zz_keep", "function", 200, 200,
                    None, 1, 0, 0))
    conn.executemany(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        "is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)", symbols)
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(caller_id, row[1]) for row in symbols],
    )
    conn.commit()
    conn.close()

    assert cli.main(["--db", str(copied), "find", "needle", "--static-only",
                     "-n", "1", "-f", "names"]) == 0
    assert capsys.readouterr().out.strip() == "needle_zz_keep"

    assert cli.main(["--db", str(copied), "calls", "ext4_bmap",
                     "--grep", "^needle_zz_keep$", "-n", "1", "-f", "names"]) == 0
    assert capsys.readouterr().out.strip() == "needle_zz_keep"


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

    from kernel_atlas import db, query
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


def test_local_build_rejects_an_output_inside_the_source_tree(
        mini_tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit):
        cli.main(["build", "--src", str(mini_tree),
                  "--output", str(mini_tree / "atlas.db"), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
    assert not (mini_tree / "atlas.db").exists()


def test_local_build_rejects_download_alias_as_a_literal_version(
        mini_tree, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "build", "lts", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "does not apply with --src" in capsys.readouterr().err


def test_local_build_requires_detectable_or_explicit_version(tmp_path, capsys):
    tree = tmp_path / "kernel-tree"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text("TEST\nF: *\n")
    (tree / "Makefile").write_text("not a kernel version\n")

    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "could not detect a kernel version" in capsys.readouterr().err


def test_build_reports_expected_indexer_failures_without_a_traceback(
        mini_tree, tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("cannot scan protected directory")

    monkeypatch.setattr(cli.indexer, "build", fail)
    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    err = capsys.readouterr().err
    assert "could not build index: cannot scan protected directory" in err
    assert "Traceback" not in err


def test_build_rechecks_output_existence_under_its_publication_lock(
        mini_tree, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from kernel_atlas import kernelsrc

    output = tmp_path / "study.db"

    @contextmanager
    def racing_output_lock(path):
        assert path == output
        path.write_bytes(b"published by another build")
        yield

    monkeypatch.setattr(kernelsrc, "output_lock", racing_output_lock)
    monkeypatch.setattr(
        cli.indexer, "build",
        lambda *args, **kwargs: pytest.fail("existing output must not be rebuilt"),
    )

    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(mini_tree), "--output", str(output),
            "--quiet",
        ])

    assert output.read_bytes() == b"published by another build"
    assert "index already exists" in capsys.readouterr().err


def test_managed_build_holds_source_then_output_locks_through_publication(
        mini_tree, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from kernel_atlas import indexer, kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    output = tmp_path / "study.db"
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    state = {"source": False, "output": False}
    events = []

    @contextmanager
    def source_lock(version):
        assert version == "6.12.104"
        assert not state["output"]
        state["source"] = True
        events.append("source+")
        try:
            yield
        finally:
            events.append("source-")
            state["source"] = False

    @contextmanager
    def output_lock(path):
        assert state["source"]
        assert path == output
        state["output"] = True
        events.append("output+")
        try:
            yield
        finally:
            events.append("output-")
            state["output"] = False

    def ensure_source(version, **kwargs):
        assert state == {"source": True, "output": True}
        events.append("source-ready")
        return mini_tree

    def build(tree, out, version, **kwargs):
        assert state == {"source": True, "output": True}
        events.append("published")
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(kernelsrc, "output_lock", output_lock)
    monkeypatch.setattr(kernelsrc, "ensure_source", ensure_source)
    monkeypatch.setattr(cli.indexer, "build", build)

    assert cli.main([
        "build", "lts", "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert events == [
        "source+", "output+", "source-ready", "published", "output-", "source-",
    ]


def test_custom_build_of_a_managed_cache_path_also_holds_its_source_lock(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil
    from contextlib import contextmanager

    from kernel_atlas import indexer, kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    output = tmp_path / "study.db"
    locked = False

    @contextmanager
    def source_lock(version):
        nonlocal locked
        assert version == "6.12.104"
        locked = True
        try:
            yield
        finally:
            locked = False

    def build(tree, out, version, **kwargs):
        assert locked
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(cli.indexer, "build", build)

    assert cli.main([
        "build", "local-study", "--src", str(managed),
        "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert not locked


def test_custom_build_symlink_alias_locks_the_canonical_managed_tree(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil
    from contextlib import contextmanager

    from kernel_atlas import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    alias = home / "kernels" / "linux-study"
    try:
        alias.symlink_to(managed, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    seen = []

    @contextmanager
    def source_lock(version):
        seen.append(version)
        yield

    def build(tree, out, version, **kwargs):
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "study.db"
    assert cli.main([
        "build", "--src", str(alias), "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert seen == ["6.12.104"]


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


def test_output_containment_checks_the_symlink_entry_not_its_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output = source / "atlas.db"
    try:
        output.symlink_to(tmp_path / "outside.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert cli._path_inside(output, source)


def test_custom_build_output_is_not_blocked_by_the_managed_index(
        mini_tree, tmp_path, monkeypatch, capsys):
    from kernel_atlas import indexer, kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    _fake_index(home, "6.12.104")
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release("longterm", "6.12.104", source_url, None),
    )
    seen = {}

    def fake_source(version, **kwargs):
        seen["source_url"] = kwargs["source_url"]
        return mini_tree

    def fake_build(tree, out, version, **kwargs):
        seen["metadata_source"] = kwargs["source"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "ensure_source", fake_source)
    monkeypatch.setattr(cli.indexer, "build", fake_build)
    custom = tmp_path / "custom.db"
    assert cli.main(["build", "lts", "--output", str(custom), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert custom.read_bytes() == b"index"
    assert seen == {
        "source_url": source_url,
        "metadata_source": str(mini_tree),
    }
    assert f"{cli.PROG} --db {custom} info mm" in out
    assert f"{cli.PROG} --db {custom} siblings mm/page_alloc.c" in out


def test_modified_managed_cache_is_recorded_as_local_source(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil

    from kernel_atlas import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    kernelsrc._write_source_identity(
        "6.12.104", managed, source_url, authoritative=True)
    (managed / "README.local").write_text("study edit\n")
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    monkeypatch.setattr(kernelsrc, "ensure_source", lambda *a, **kw: managed)
    seen = {}

    def build(tree, out, version, **kwargs):
        seen.update(kwargs)
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "modified.db"
    assert cli.main([
        "build", "6.12.104", "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert seen["source"] == str(managed)
    assert seen["managed_tree_identity"] is None


def test_managed_source_change_during_build_prevents_publication(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil

    from kernel_atlas import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    kernelsrc._write_source_identity(
        "6.12.104", managed, source_url, authoritative=True)
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    monkeypatch.setattr(kernelsrc, "ensure_source", lambda *a, **kw: managed)

    def build(tree, out, version, **kwargs):
        (tree / "README.changed").write_text("changed during build\n")
        kwargs["pre_publish"]()
        out.write_bytes(b"must not publish")
        return indexer.BuildStats()

    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "changed.db"
    with pytest.raises(SystemExit):
        cli.main([
            "build", "6.12.104", "--output", str(output), "--quiet",
        ])

    assert not output.exists()
    assert "managed source changed while the index was built" in (
        capsys.readouterr().err)


def test_stale_recorded_source_is_not_replaced_by_same_version_tree(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = home / "kernels" / "linux-6.12.104"
    managed.mkdir(parents=True)
    (managed / "MAINTAINERS").write_text("different snapshot\n")
    assert cli.find_source_tree({
        "tree_path": str(tmp_path / "missing-original"),
        "kernel_version": "6.12.104",
        "index_stem": "6.12.104",
    }) is None


def test_source_commands_reject_parent_paths_from_an_untrusted_index(
        mini_index, mini_tree, tmp_path, capsys):
    import shutil

    tree = tmp_path / "tree"
    shutil.copytree(mini_tree, tree)
    copied = tmp_path / "unsafe.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    conn.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    conn.execute(
        "UPDATE files SET path='../outside.c' WHERE path='net/ipv4/tcp.c'")
    conn.commit()
    conn.close()

    for command in ("info", "path", "show"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command, "tcp_sendmsg"])
        assert "unsafe path" in capsys.readouterr().err


def test_source_commands_reject_symlinks_escaping_the_recorded_tree(
        mini_index, mini_tree, tmp_path, capsys):
    import shutil

    tree = tmp_path / "tree"
    shutil.copytree(mini_tree, tree)
    outside = tmp_path / "outside.c"
    outside.write_text("secret\n")
    escape = tree / "escape.c"
    try:
        escape.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    copied = tmp_path / "escaping-link.db"
    shutil.copy(mini_index, copied)
    conn = sqlite3.connect(copied)
    conn.execute("UPDATE meta SET value=? WHERE key='tree_path'", (str(tree),))
    conn.execute("UPDATE files SET path='escape.c' WHERE path='net/ipv4/tcp.c'")
    conn.commit()
    conn.close()

    for command in ("info", "path", "show"):
        with pytest.raises(SystemExit):
            cli.main(["--db", str(copied), command, "tcp_sendmsg"])
        assert "escapes the recorded source tree" in capsys.readouterr().err


def test_downloaded_build_rejects_output_inside_managed_source_before_fetch(
        tmp_path, monkeypatch, capsys):
    from kernel_atlas import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104",
            "https://cdn.kernel.org/example/linux-6.12.104.tar.xz", None),
    )
    monkeypatch.setattr(
        kernelsrc, "ensure_source",
        lambda *a, **kw: pytest.fail("unsafe output should fail before download"),
    )
    out = home / "kernels" / "linux-6.12.104" / "atlas.db"
    with pytest.raises(SystemExit):
        cli.main(["build", "lts", "--output", str(out), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
