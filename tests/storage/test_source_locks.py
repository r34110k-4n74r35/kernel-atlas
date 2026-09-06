"""Source, output, and lifecycle lock serialization and file safety."""

from __future__ import annotations

import threading

import pytest

from kernel_atlas import kernelsrc


def test_source_lock_serializes_same_version_acquisition(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with kernelsrc._source_lock("9.9"):
            first_entered.set()
            assert release_first.wait(2)

    def second():
        with kernelsrc._source_lock("9.9"):
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    assert first_entered.wait(2)
    two.start()
    assert not second_entered.wait(0.05)
    release_first.set()
    one.join(2)
    two.join(2)
    assert second_entered.is_set()


def test_source_lock_is_reentrant_for_ensure_source_callers(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    with kernelsrc.source_lock("9.9"):
        with kernelsrc.source_lock("9.9"):
            assert True


def test_output_lock_serializes_publication_for_the_same_path(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    output = tmp_path / "indexes" / "study.db"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with kernelsrc.output_lock(output):
            first_entered.set()
            assert release_first.wait(2)

    def second():
        with kernelsrc.output_lock(output):
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    assert first_entered.wait(2)
    two.start()
    assert not second_entered.wait(0.05)
    release_first.set()
    one.join(2)
    two.join(2)
    assert second_entered.is_set()


def test_output_lock_serializes_an_existing_symlink_alias(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    target = tmp_path / "real.db"
    target.write_bytes(b"index")
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    entered = threading.Event()

    def through_alias():
        with kernelsrc.output_lock(alias):
            entered.set()

    with kernelsrc.output_lock(target):
        worker = threading.Thread(target=through_alias)
        worker.start()
        assert not entered.wait(0.05)
    worker.join(2)
    assert entered.is_set()


def test_output_lock_for_dangling_alias_does_not_create_target_parents(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    missing_parent = tmp_path / "personal" / "missing"
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(missing_parent / "real.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with kernelsrc.output_lock(alias):
        assert True

    assert not missing_parent.exists()


def test_lifecycle_lock_rejects_a_symlink_without_writing_its_target(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    output = tmp_path / "study.db"
    lock = kernelsrc._output_lock_paths(output)[0]
    lock.parent.mkdir(parents=True)
    victim = tmp_path / "personal-notes"
    victim.write_bytes(b"")
    try:
        lock.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(OSError):
        with kernelsrc.output_lock(output):
            pytest.fail("unsafe lock must not be acquired")
    assert victim.read_bytes() == b""


def test_lifecycle_lock_rejects_a_hard_link_without_writing_its_target(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    output = tmp_path / "study.db"
    lock = kernelsrc._output_lock_paths(output)[0]
    lock.parent.mkdir(parents=True)
    victim = tmp_path / "personal-notes"
    victim.write_bytes(b"")
    try:
        lock.hardlink_to(victim)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(OSError, match="unsafe lifecycle lock"):
        with kernelsrc.output_lock(output):
            pytest.fail("unsafe lock must not be acquired")
    assert victim.read_bytes() == b""
