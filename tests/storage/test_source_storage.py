"""Source acquisition must never generate data beyond the project boundary."""

from __future__ import annotations

import io

import pytest

from kernel_atlas.storage import config, kernelsrc


@pytest.fixture
def storage_boundary(tmp_path, monkeypatch):
    # Both sides of this simulated boundary stay in pytest's local directory.
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(config, "project_root", lambda: project)
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(project))
    return project, outside


@pytest.mark.parametrize("via_symlink", [False, True])
def test_download_rejects_external_parent_before_network(
        storage_boundary, monkeypatch, via_symlink):
    project, outside = storage_boundary
    parent = outside / "downloads"
    if via_symlink:
        alias = project / "external"
        alias.symlink_to(outside, target_is_directory=True)
        parent = alias / "downloads"

    def unexpected_request(*args, **kwargs):
        pytest.fail("external destination must be rejected before networking")

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", unexpected_request)
    with pytest.raises(ValueError, match="project"):
        kernelsrc.download("https://example.invalid/linux.tar.xz",
                           parent / "linux.tar.xz", quiet=True)
    assert list(outside.iterdir()) == []


def test_partial_download_cannot_truncate_external_file(storage_boundary):
    _, outside = storage_boundary
    part = outside / "archive.part"
    part.write_bytes(b"keep this partial download")
    with pytest.raises(ValueError, match="project"):
        kernelsrc._open_download_part(part, append=False)
    assert part.read_bytes() == b"keep this partial download"


@pytest.mark.parametrize("via_symlink", [False, True])
def test_extract_rejects_external_directory_before_scratch_creation(
        storage_boundary, via_symlink):
    project, outside = storage_boundary
    into = outside / "extracted"
    if via_symlink:
        into = project / "extracted"
        into.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="project"):
        kernelsrc.extract(project / "missing.tar.xz", into, quiet=True)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("external_source", [False, True])
def test_rename_refuses_to_move_files_across_project_boundary(
        storage_boundary, external_source):
    project, outside = storage_boundary
    source = (outside if external_source else project) / "source"
    destination = (project if external_source else outside) / "destination"
    source.write_text("keep me")
    with pytest.raises(ValueError, match="project"):
        kernelsrc._rename_noreplace(source, destination)
    assert source.read_text() == "keep me"
    assert not destination.exists()


def test_local_rename_moves_alias_without_touching_external_target(
        storage_boundary):
    project, outside = storage_boundary
    target = outside / "target"
    target.write_text("keep me")
    alias = project / "alias"
    alias.symlink_to(target)
    moved = project / "moved"
    kernelsrc._rename_noreplace(alias, moved)
    assert not alias.is_symlink()
    assert moved.is_symlink()
    assert target.read_text() == "keep me"


def test_locks_reject_external_parents(storage_boundary):
    project, outside = storage_boundary
    alias = project / "external"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="project"):
        with kernelsrc._file_lock(alias / "lifecycle.lock"):
            pytest.fail("must not acquire an external lock")
    with pytest.raises(ValueError, match="project"):
        with kernelsrc.output_lock(outside / "index.db"):
            pytest.fail("must not lock an external output")
    assert list(outside.iterdir()) == []
    assert not (project / "indexes").exists()


def _identity():
    return kernelsrc.ManagedSourceIdentity(
        token="a" * 64, device=1, inode=1, digest="b" * 64,
        source="https://example.invalid/linux.tar.xz", authoritative=True,
    )


def test_marker_and_quarantine_reject_external_source_directory(
        storage_boundary, monkeypatch):
    _, outside = storage_boundary
    monkeypatch.setattr(config, "sources_dir", lambda: outside / "kernels")
    with pytest.raises(ValueError, match="project"):
        kernelsrc._store_source_identity("6.12", _identity())
    with pytest.raises(ValueError, match="project"):
        kernelsrc._source_quarantine_base(create=True)
    assert list(outside.iterdir()) == []


def test_marker_replaces_local_symlink_without_modifying_target(
        storage_boundary):
    _, outside = storage_boundary
    target = outside / "target"
    target.write_text("keep me")
    marker = kernelsrc._source_identity_path("6.12")
    marker.parent.mkdir()
    marker.symlink_to(target)
    identity = _identity()
    kernelsrc._store_source_identity("6.12", identity)
    assert not marker.is_symlink()
    assert kernelsrc.source_identity_marker("6.12") == identity
    assert target.read_text() == "keep me"


def test_download_still_publishes_inside_project(storage_boundary, monkeypatch):
    project, outside = storage_boundary

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "7"}

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen",
                        lambda *args, **kwargs: Response(b"archive"))
    destination = project / "kernels" / "linux.tar.xz"
    assert kernelsrc.download("https://example.invalid/linux.tar.xz",
                              destination, quiet=True) == destination
    assert destination.read_bytes() == b"archive"
    assert not destination.with_name(destination.name + ".part").exists()
    assert list(outside.iterdir()) == []
