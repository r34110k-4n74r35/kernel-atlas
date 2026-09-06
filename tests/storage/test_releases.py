"""Release discovery, version detection, URLs, and checksum lookup."""

from __future__ import annotations

import http.client

import pytest

from kernel_atlas import __version__, kernelsrc

from .helpers import _make_tree


def test_user_agent_reports_package_version_and_project_homepage():
    assert kernelsrc.USER_AGENT == (
        f"kernel-atlas/{__version__} "
        "(+https://github.com/r34110k-4n74r35/kernel-atlas)"
    )


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


def test_get_normalizes_a_truncated_http_response(monkeypatch):
    def truncated(*args, **kwargs):
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", truncated)
    with pytest.raises(OSError, match="incomplete HTTP response"):
        kernelsrc._get("https://example.invalid/releases.json")


def test_explicit_release_falls_back_after_a_truncated_live_feed(monkeypatch):
    def truncated(*args, **kwargs):
        raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(kernelsrc.urllib.request, "urlopen", truncated)
    release = kernelsrc.resolve_version("6.12.104")
    assert release.moniker == "explicit"
    assert release.source == kernelsrc.tarball_url("6.12.104")
