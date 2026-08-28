from __future__ import annotations

import io
import shutil
import tarfile
import threading
import urllib.error
from pathlib import Path

import pytest

from kernel_atlas import config, kernelsrc


def _make_tree(path: Path, version=("6", "12", "104", "")) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    major, patch, sublevel, extra = version
    (path / "MAINTAINERS").write_text("TEST\nM: A <a@example.com>\nF: *\n")
    (path / "Makefile").write_text(
        f"VERSION = {major}\nPATCHLEVEL = {patch}\nSUBLEVEL = {sublevel}\n"
        f"EXTRAVERSION = {extra}\n",
        encoding="utf-8",
    )
    return path


def test_detect_version_uses_canonical_kernel_org_name(tmp_path):
    tree = _make_tree(tmp_path / "linux", ("7", "2", "0", "-rc1"))
    assert kernelsrc.detect_version(tree) == "7.2-rc1"
    (tree / "Makefile").write_text(
        "VERSION = 7\nPATCHLEVEL = 2\nSUBLEVEL = 0\nEXTRAVERSION =\n"
    )
    assert kernelsrc.detect_version(tree) == "7.2"


def test_detect_version_preserves_historical_2x_zero_sublevel(tmp_path):
    tree = _make_tree(tmp_path / "linux", ("2", "6", "0", ""))
    assert kernelsrc.detect_version(tree) == "2.6.0"


def test_detect_version_rejects_path_like_makefile_fields(tmp_path):
    tree = _make_tree(tmp_path / "linux")
    (tree / "Makefile").write_text(
        "VERSION = /tmp/owned\nPATCHLEVEL = 1\nSUBLEVEL = 0\nEXTRAVERSION =\n"
    )
    assert kernelsrc.detect_version(tree) is None

    (tree / "Makefile").write_text(
        "VERSION = ²\nPATCHLEVEL = 6\nSUBLEVEL = 0\nEXTRAVERSION =\n"
    )
    assert kernelsrc.detect_version(tree) is None


def test_26_checksum_is_looked_up_beside_26_tarball(monkeypatch):
    seen = []
    digest = "a" * 64

    def fake_get(url, timeout=30):
        seen.append(url)
        return f"{digest}  linux-2.6.39.tar.xz\n".encode()

    monkeypatch.setattr(kernelsrc, "_get", fake_get)
    assert kernelsrc._expected_sha256("2.6.39") == digest
    assert seen == ["https://cdn.kernel.org/pub/linux/kernel/v2.6/sha256sums.asc"]


@pytest.mark.parametrize(
    ("version", "series"),
    [
        ("1.2.13", "v1.2"),
        ("2.4.37", "v2.4"),
        ("2.6.39", "v2.6"),
        ("3.0", "v3.x"),
        ("6.12.104", "v6.x"),
    ],
)
def test_tarball_url_uses_kernel_org_archive_series(version, series):
    assert kernelsrc.tarball_url(version) == (
        f"https://cdn.kernel.org/pub/linux/kernel/{series}/linux-{version}.tar.xz"
    )


def test_exact_release_falls_back_to_cdn_when_live_feed_is_malformed(monkeypatch):
    def bad_feed(*args, **kwargs):
        raise ValueError("bad feed")

    monkeypatch.setattr(kernelsrc, "list_releases", bad_feed)
    release = kernelsrc.resolve_version("6.12.104")
    assert release.moniker == "explicit"
    assert release.version == "6.12.104"
    assert release.source == kernelsrc.tarball_url("6.12.104")


def test_normal_download_fails_closed_when_checksum_is_unavailable(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    monkeypatch.setattr(kernelsrc, "_expected_sha256", lambda *a, **kw: None)
    monkeypatch.setattr(
        kernelsrc,
        "download",
        lambda *a, **kw: pytest.fail("download must not start without a checksum"),
    )
    with pytest.raises(RuntimeError, match="refusing an unverified download"):
        kernelsrc.ensure_source("6.12.104", quiet=True, verify=True)


def test_verified_download_rejects_non_https_source_url(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    monkeypatch.setattr(
        kernelsrc,
        "download",
        lambda *a, **kw: pytest.fail("an insecure download must not start"),
    )
    with pytest.raises(RuntimeError, match="non-HTTPS"):
        kernelsrc.ensure_source(
            "6.12.104", quiet=True, verify=True,
            source_url=("http://cdn.kernel.org/pub/linux/kernel/v6.x/"
                        "linux-6.12.104.tar.xz"),
        )


def test_cached_tree_must_match_requested_version(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    tree = _make_tree(config.source_path("9.9"), ("1", "2", "3", ""))
    with pytest.raises(RuntimeError, match="reports Linux 1.2.3, not 9.9"):
        kernelsrc.ensure_source("9.9", quiet=True)
    assert tree.is_dir(), "a mismatched cache must be preserved for the user to inspect"


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


class _Response(io.BytesIO):
    status = 200

    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_download_restarts_after_stale_part_gets_416(monkeypatch, tmp_path):
    dest = tmp_path / "linux.tar.xz"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"stale-complete-file")
    ranges = []

    def fake_urlopen(req, timeout=60):
        ranges.append(req.get_header("Range"))
        if len(ranges) == 1:
            raise urllib.error.HTTPError(req.full_url, 416, "range", {}, None)
        return _Response(b"fresh")

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", fake_urlopen)
    kernelsrc.download("https://example.invalid/linux.tar.xz", dest,
                       quiet=True, retries=2)
    assert ranges == [f"bytes={len(b'stale-complete-file')}-", None]
    assert dest.read_bytes() == b"fresh"
    assert not part.exists()


def test_download_rejects_malformed_content_length(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=60):
        response = _Response(b"payload")
        response.headers["Content-Length"] = "not-a-number"
        return response

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OSError, match="invalid Content-Length"):
        kernelsrc.download(
            "https://example.invalid/linux.tar.xz",
            tmp_path / "linux.tar.xz",
            quiet=True,
            retries=1,
        )
