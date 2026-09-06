"""Application index files stay in the project, including SQLite sidecars."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kernel_atlas.storage import db


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
