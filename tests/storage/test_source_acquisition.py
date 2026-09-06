"""Managed cache identity, safe archive extraction, and publication races."""

from __future__ import annotations

import io
import shutil
import tarfile

import pytest

from kernel_atlas import config, kernelsrc

from .helpers import _make_tree


def test_cached_tree_must_match_requested_version(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    tree = _make_tree(config.source_path("9.9"), ("1", "2", "3", ""))
    with pytest.raises(RuntimeError, match="reports Linux 1.2.3, not 9.9"):
        kernelsrc.ensure_source("9.9", quiet=True)
    assert tree.is_dir(), "a mismatched cache must be preserved for the user to inspect"


def test_unrecognized_existing_source_destination_is_preserved(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    tree = config.source_path("9.9")
    tree.mkdir(parents=True)
    notes = tree / "unfinished-research.txt"
    notes.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        kernelsrc, "download",
        lambda *args, **kwargs: pytest.fail("must refuse before downloading"),
    )

    with pytest.raises(RuntimeError, match="already exists.*move or remove"):
        kernelsrc.ensure_source("9.9", quiet=True, verify=False)

    assert notes.read_text(encoding="utf-8") == "keep me\n"


def test_broken_source_destination_symlink_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    tree = config.source_path("9.9")
    tree.parent.mkdir(parents=True)
    try:
        tree.symlink_to(tmp_path / "missing-personal-tree", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(RuntimeError, match="already exists"):
        kernelsrc.ensure_source("9.9", quiet=True, verify=False)

    assert tree.is_symlink()


def test_release_candidate_git_snapshot_warns_and_extracts_tar_gz(
        monkeypatch, tmp_path):
    version = "7.3-rc1"
    source_tree = _make_tree(tmp_path / f"linux-{version}", ("7", "3", "0", "-rc1"))
    archive = tmp_path / f"linux-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source_tree, arcname=source_tree.name)

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))

    def fake_download(url, dest, quiet=False):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, dest)
        return dest

    monkeypatch.setattr(kernelsrc, "download", fake_download)
    url = f"https://git.kernel.org/torvalds/t/linux-{version}.tar.gz"
    with pytest.warns(kernelsrc.UnverifiedRCWarning, match="no published checksum"):
        tree = kernelsrc.ensure_source(
            version, quiet=True, verify=True, source_url=url)
    assert tree == config.source_path(version)
    assert kernelsrc.detect_version(tree) == version
    assert (tree / "MAINTAINERS").is_file()


@pytest.mark.parametrize(
    "member_name",
    ["../escaped.txt", "/absolute.txt", r"..\escaped.txt"],
)
def test_extract_rejects_archive_member_outside_destination(tmp_path, member_name):
    archive = tmp_path / "linux-9.9.tar.gz"
    payload = b"must not escape"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo(member_name)
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    destination = tmp_path / "sources"
    with pytest.raises(RuntimeError, match="archive (path|contains)"):
        kernelsrc.extract(archive, destination, quiet=True)
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize("use_system_tar", [False, True])
@pytest.mark.parametrize(
    ("member_type", "linkname", "error"),
    [
        (tarfile.SYMTYPE, "../../outside", "escapes extraction root"),
        (tarfile.LNKTYPE, "../outside", "escapes extraction root"),
        (tarfile.CHRTYPE, "", "special device"),
        (tarfile.FIFOTYPE, "", "special device"),
    ],
)
def test_extract_rejects_unsafe_links_and_special_files_before_backend(
        monkeypatch, tmp_path, use_system_tar, member_type, linkname, error):
    archive = tmp_path / "linux-9.9.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("linux-9.9/unsafe")
        member.type = member_type
        member.linkname = linkname
        tf.addfile(member)

    def backend_must_not_run(*args, **kwargs):
        pytest.fail("archive validation must fail before extraction")

    monkeypatch.setattr(
        kernelsrc.shutil,
        "which",
        lambda command: "/usr/bin/tar" if use_system_tar else None,
    )
    monkeypatch.setattr(kernelsrc.subprocess, "run", backend_must_not_run)
    monkeypatch.setattr(tarfile.TarFile, "extractall", backend_must_not_run)
    with pytest.raises(RuntimeError, match=error):
        kernelsrc.extract(archive, tmp_path / "sources", quiet=True)


def test_extract_rejects_symlink_as_the_top_level_tree(monkeypatch, tmp_path):
    archive = tmp_path / "linux-9.9.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        other = tarfile.TarInfo("other")
        other.type = tarfile.DIRTYPE
        tf.addfile(other)
        root_link = tarfile.TarInfo("linux-9.9")
        root_link.type = tarfile.SYMTYPE
        root_link.linkname = "other"
        tf.addfile(root_link)

    # Exercise the Python backend explicitly; the post-extraction publication
    # check is shared with the system-tar fast path.
    monkeypatch.setattr(kernelsrc.shutil, "which", lambda command: None)
    destination = tmp_path / "sources"
    with pytest.raises(RuntimeError, match="real linux-9.9/ directory"):
        kernelsrc.extract(archive, destination, quiet=True)
    assert not (destination / "linux-9.9").exists()


def test_acquisition_does_not_claim_a_destination_published_during_extract(
        monkeypatch, tmp_path):
    source = _make_tree(
        tmp_path / "source" / "linux-9.9", ("9", "9", "0", ""))
    archive = tmp_path / "linux-9.9.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname=source.name)
    destination = tmp_path / "kernels"
    original_extractall = tarfile.TarFile.extractall

    def race_publication(handle, path, *args, **kwargs):
        original_extractall(handle, path, *args, **kwargs)
        replacement = destination / "linux-9.9"
        replacement.mkdir(parents=True)
        (replacement / "personal-notes").write_text("keep me\n")

    monkeypatch.setattr(kernelsrc.shutil, "which", lambda command: None)
    monkeypatch.setattr(tarfile.TarFile, "extractall", race_publication)

    with pytest.raises(RuntimeError, match="appeared during extraction"):
        kernelsrc.extract(
            archive, destination, quiet=True, require_new=True)

    assert (destination / "linux-9.9" / "personal-notes").read_text() == (
        "keep me\n")


def test_acquisition_does_not_replace_an_empty_directory_racing_publication(
        monkeypatch, tmp_path):
    source = _make_tree(
        tmp_path / "source" / "linux-9.9", ("9", "9", "0", ""))
    archive = tmp_path / "linux-9.9.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname=source.name)
    destination = tmp_path / "kernels"
    final = destination / "linux-9.9"
    original = kernelsrc._rename_noreplace

    def race(source_path, destination_path):
        final.mkdir()
        original(source_path, destination_path)

    monkeypatch.setattr(kernelsrc.shutil, "which", lambda command: None)
    monkeypatch.setattr(kernelsrc, "_rename_noreplace", race)

    with pytest.raises(RuntimeError, match="appeared during extraction"):
        kernelsrc.extract(
            archive, destination, quiet=True, require_new=True)

    assert final.is_dir()
    assert list(final.iterdir()) == []
