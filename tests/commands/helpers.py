"""Synthetic managed indexes and source identities for CLI lifecycle tests."""

from __future__ import annotations

import sqlite3

from kernel_atlas.storage import db, kernelsrc


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
