"""Index and managed-source removal, authorization, and concurrent changes."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.storage import config, db, kernelsrc

from .helpers import _authorize_source, _fake_source


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
    assert "Pinned 6.18.45" in " ".join(out.split())
    assert "Active index 6.18.45" in " ".join(out.split())


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
    from kernel_atlas.commands import lifecycle as cli_lifecycle

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

    from kernel_atlas.storage import kernelsrc

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

    from kernel_atlas.commands import lifecycle as cli_lifecycle

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

    from kernel_atlas.commands import lifecycle as cli_lifecycle

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
