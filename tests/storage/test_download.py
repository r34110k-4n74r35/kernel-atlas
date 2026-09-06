"""Download verification, resumable transfers, and partial-file safety."""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from kernel_atlas import kernelsrc


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


class _Response(io.BytesIO):
    status = 200

    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@pytest.mark.parametrize("known_length", [True, False])
def test_download_progress_with_and_without_content_length(
        monkeypatch, tmp_path, capsys, known_length):
    def open_response(*args, **kwargs):
        response = _Response(b"payload")
        if not known_length:
            response.headers.clear()
        return response

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", open_response)
    dest = tmp_path / "linux.tar.xz"
    kernelsrc.download("https://example.invalid/linux.tar.xz", dest, retries=1)
    assert dest.read_bytes() == b"payload"
    output = capsys.readouterr()
    assert output.out == ""
    assert "done Downloading source" in output.err
    assert ("100%" in output.err) == known_length
    assert "7.0 B" in output.err
    assert "\r" not in output.err


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


def test_download_rejects_a_symlink_part_without_touching_its_target(
        monkeypatch, tmp_path):
    dest = tmp_path / "linux.tar.xz"
    part = dest.with_name(dest.name + ".part")
    victim = tmp_path / "personal-notes"
    victim.write_bytes(b"do not append")
    try:
        part.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(
        kernelsrc.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("unsafe part must fail before HTTP"),
    )

    with pytest.raises(OSError, match="unsafe partial download"):
        kernelsrc.download(
            "https://example.invalid/linux.tar.xz", dest,
            quiet=True, retries=1)

    assert victim.read_bytes() == b"do not append"
    assert part.is_symlink()


def test_download_rejects_a_hard_link_part_without_touching_its_target(
        monkeypatch, tmp_path):
    dest = tmp_path / "linux.tar.xz"
    part = dest.with_name(dest.name + ".part")
    victim = tmp_path / "personal-notes"
    victim.write_bytes(b"do not append")
    try:
        part.hardlink_to(victim)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    monkeypatch.setattr(
        kernelsrc.urllib.request, "urlopen",
        lambda *args, **kwargs: pytest.fail("unsafe part must fail before HTTP"),
    )

    with pytest.raises(OSError, match="unsafe partial download"):
        kernelsrc.download(
            "https://example.invalid/linux.tar.xz", dest,
            quiet=True, retries=1)

    assert victim.read_bytes() == b"do not append"


def test_download_part_rejects_a_regular_file_swap_before_open(
        monkeypatch, tmp_path):
    part = tmp_path / "linux.tar.xz.part"
    part.write_bytes(b"partial")
    original_part = tmp_path / "original-part"
    victim = tmp_path / "personal-notes"
    victim.write_bytes(b"keep me")
    original_open = kernelsrc.os.open
    raced = False

    def swapping_open(path, flags, mode=0o777):
        nonlocal raced
        if Path(path) == part and not raced:
            part.rename(original_part)
            victim.rename(part)
            raced = True
        return original_open(path, flags, mode)

    monkeypatch.setattr(kernelsrc.os, "open", swapping_open)
    with pytest.raises(OSError, match="unsafe partial download"):
        kernelsrc._open_download_part(part, append=False)

    assert part.read_bytes() == b"keep me"
    assert original_part.read_bytes() == b"partial"
