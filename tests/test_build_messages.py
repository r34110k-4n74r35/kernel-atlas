"""Acquisition messages share build formatting without changing quiet behavior."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from kernel_atlas import config, kernelsrc
from kernel_atlas.build_output import color_mode


@pytest.mark.parametrize("quiet", [False, True])
def test_cached_source_message_respects_build_color_and_quiet(
        tmp_path, monkeypatch, capsys, quiet):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    tree = config.source_path("6.12.104")
    tree.mkdir(parents=True)
    (tree / "MAINTAINERS").write_text("TEST\n", encoding="utf-8")
    (tree / "Makefile").write_text(
        "VERSION = 6\nPATCHLEVEL = 12\nSUBLEVEL = 104\nEXTRAVERSION =\n",
        encoding="utf-8",
    )

    with color_mode("always"):
        assert kernelsrc.ensure_source("6.12.104", quiet=quiet) == tree

    captured = capsys.readouterr()
    assert captured.out == ""
    if quiet:
        assert captured.err == ""
    else:
        assert "Source" in captured.err
        assert f"cached at {tree}" in captured.err
        assert "\x1b[" in captured.err


@pytest.mark.parametrize("quiet", [False, True])
def test_download_retry_is_labeled_and_respects_quiet(
        tmp_path, monkeypatch, capsys, quiet):
    attempts = 0

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "7"}

    def open_response(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("connection dropped")
        return Response(b"payload")

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", open_response)
    monkeypatch.setattr(kernelsrc.time, "sleep", lambda seconds: None)
    destination = tmp_path / "linux.tar.xz"

    with color_mode("never"):
        kernelsrc.download("https://example.invalid/linux.tar.xz", destination,
                           quiet=quiet, retries=2)

    assert destination.read_bytes() == b"payload"
    captured = capsys.readouterr()
    assert captured.out == ""
    if quiet:
        assert captured.err == ""
    else:
        retry = next(line for line in captured.err.splitlines() if "Retry" in line)
        assert "connection dropped" in retry
        assert "resuming (attempt 2/2)" in retry
        assert "\x1b[" not in captured.err


@pytest.mark.parametrize("quiet", [False, True])
def test_fetch_and_checksum_messages_keep_verification_result(
        tmp_path, monkeypatch, capsys, quiet):
    version = "6.12.104"
    archive = tmp_path / "fixture.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        for name, contents in {
            "MAINTAINERS": b"TEST\n",
            "Makefile": b"VERSION = 6\nPATCHLEVEL = 12\nSUBLEVEL = 104\n",
        }.items():
            member = tarfile.TarInfo(f"linux-{version}/{name}")
            member.size = len(contents)
            output.addfile(member, io.BytesIO(contents))
    payload = archive.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(kernelsrc, "_expected_sha256", lambda *a, **kw: digest)
    attempts = 0

    def download(url, destination, quiet=False):
        nonlocal attempts
        attempts += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"corrupted" if attempts == 1 else payload)
        return destination

    monkeypatch.setattr(kernelsrc, "download", download)
    with color_mode("never"):
        tree = kernelsrc.ensure_source(version, quiet=quiet)

    assert kernelsrc.detect_version(tree) == version
    assert attempts == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    if quiet:
        assert captured.err == ""
    else:
        assert "Fetch" in captured.err
        assert kernelsrc.tarball_url(version) in captured.err
        assert "Checksum" in captured.err
        assert "mismatch — discarding and downloading again" in captured.err
        assert "sha256 verified against kernel.org" in captured.err
        assert "\x1b[" not in captured.err
