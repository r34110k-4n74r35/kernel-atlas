"""Application index files stay in the project, including SQLite sidecars."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kernel_atlas import config, db, indexer


@pytest.fixture
def storage_roots(tmp_path, monkeypatch):
    # Both locations are real project-local test fixtures.  Only the first is
    # the simulated checkout, so rejection tests never write outside the repo.
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.setattr(config, "project_root", lambda: project)
    return project, outside


@pytest.mark.parametrize("operation", ["create", "read", "write"])
def test_database_rejects_external_path_before_creating_files(storage_roots, operation):
    _, outside = storage_roots
    path = outside / "new-directory" / "index.db"

    with pytest.raises(ValueError, match="project"):
        if operation == "create":
            db.create(path)
        else:
            db.connect(path, readonly=operation == "read")

    assert not path.parent.exists()


@pytest.mark.parametrize("readonly", [True, False])
def test_connect_rejects_database_alias_to_external_file(storage_roots, readonly):
    project, outside = storage_roots
    target = outside / "index.db"
    target.write_bytes(b"external database")
    alias = project / "alias.db"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="project"):
        db.connect(alias, readonly=readonly)

    assert target.read_bytes() == b"external database"
    assert sorted(path.name for path in outside.iterdir()) == ["index.db"]


@pytest.mark.parametrize("readonly", [True, False])
def test_connect_rejects_hardlinked_database(storage_roots, readonly):
    project, outside = storage_roots
    target = outside / "index.db"
    target.write_bytes(b"external database")
    alias = project / "alias.db"
    alias.hardlink_to(target)

    with pytest.raises(ValueError, match="multiple hard links"):
        db.connect(alias, readonly=readonly)

    assert target.read_bytes() == b"external database"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("operation", ["create", "read", "write"])
def test_database_rejects_external_sidecar_alias(
        storage_roots, suffix, alias_kind, operation):
    project, outside = storage_roots
    path = project / "index.db"
    path.write_bytes(b"original database")
    external = outside / "sidecar"
    external.write_bytes(b"original sidecar")
    sidecar = Path(f"{path}{suffix}")
    if alias_kind == "symlink":
        sidecar.symlink_to(external)
    else:
        sidecar.hardlink_to(external)

    with pytest.raises(ValueError, match="project|multiple hard links"):
        if operation == "create":
            db.create(path)
        else:
            db.connect(path, readonly=operation == "read")

    assert path.read_bytes() == b"original database"
    assert external.read_bytes() == b"original sidecar"


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_create_replaces_local_leaf_alias_without_changing_target(
        storage_roots, alias_kind):
    project, outside = storage_roots
    target = outside / "keep.db"
    target.write_bytes(b"keep this file")
    path = project / "new.db"
    if alias_kind == "symlink":
        path.symlink_to(target)
    else:
        path.hardlink_to(target)

    conn = db.create(path)
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    conn.close()

    assert target.read_bytes() == b"keep this file"
    assert not path.is_symlink()
    assert path.stat().st_nlink == 1


def test_connect_supports_local_database_alias(storage_roots):
    project, _ = storage_roots
    path = project / "index.db"
    conn = db.create(path)
    conn.close()
    alias = project / "alias.db"
    alias.symlink_to(path)

    conn = db.connect(alias)
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    conn.close()


def test_local_readonly_wal_connection_still_sees_committed_rows(storage_roots):
    project, _ = storage_roots
    path = project / "index.db"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE example (value TEXT)")
        writer.execute("INSERT INTO example VALUES ('committed')")
        writer.commit()

        reader = db.connect(path)
        try:
            assert reader.execute("SELECT value FROM example").fetchone()[0] == "committed"
        finally:
            reader.close()
    finally:
        writer.close()


@pytest.mark.parametrize("redirect_parent", [False, True])
def test_build_rejects_external_output_before_creating_files(
        storage_roots, mini_tree, redirect_parent):
    project, outside = storage_roots
    if redirect_parent:
        parent = project / "redirected"
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent = outside
    out = parent / "new-directory" / "index.db"

    with pytest.raises(ValueError, match="project"):
        indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True)

    assert not (outside / "new-directory").exists()


def test_build_replaces_local_leaf_alias_without_changing_target(storage_roots, mini_tree):
    project, outside = storage_roots
    target = outside / "keep.db"
    target.write_bytes(b"keep this file")
    out = project / "index.db"
    out.symlink_to(target)

    stats = indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True)

    assert stats.files > 0
    assert target.read_bytes() == b"keep this file"
    assert not out.is_symlink()
    assert not list(project.glob("*.building"))


def test_build_rechecks_parent_before_publication(storage_roots, mini_tree):
    project, outside = storage_roots
    parent = project / "output"
    parent.mkdir()
    out = parent / "index.db"
    parked = project / "parked"
    outside_scratch = []

    def redirect_parent():
        # Keep the open database inside the simulated project, but redirect
        # future path operations and plant a same-name file outside it.
        scratch = next(parent.glob("*.building"))
        sentinel = outside / scratch.name
        sentinel.write_bytes(b"keep external scratch")
        outside_scratch.append(sentinel)
        parent.rename(parked)
        parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="project"):
        indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True,
                      pre_publish=redirect_parent)

    assert not (outside / "index.db").exists()
    assert outside_scratch[0].read_bytes() == b"keep external scratch"
    assert len(list(parked.glob("*.building"))) == 1
