"""Discover, download and unpack Linux kernel source trees from kernel.org."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

RELEASES_URL = "https://www.kernel.org/releases.json"
CDN = "https://cdn.kernel.org/pub/linux/kernel"
USER_AGENT = "kernel-atlas/0.1 (+https://kernel.org)"

# Monikers that make sense as a `build` target, best-first for a learner.
PREFERRED_MONIKERS = ("longterm", "stable", "mainline")


@dataclass
class Release:
    moniker: str
    version: str
    source: str | None
    released: str | None

    @property
    def is_lts(self) -> bool:
        return self.moniker == "longterm"


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_releases(timeout: int = 30) -> list[Release]:
    """Live release list from kernel.org, so no version is ever hardcoded."""
    data = json.loads(_get(RELEASES_URL, timeout=timeout))
    out: list[Release] = []
    for r in data.get("releases", []):
        released = (r.get("released") or {}).get("isodate")
        out.append(
            Release(
                moniker=r.get("moniker", "?"),
                version=r.get("version", "?"),
                source=r.get("source"),
                released=released,
            )
        )
    return out


def resolve_version(spec: str, timeout: int = 30) -> Release:
    """Turn 'lts' / 'stable' / 'mainline' / 'latest' / '6.12.104' into a Release."""
    spec = (spec or "lts").strip()
    aliases = {"lts": "longterm", "longterm": "longterm", "stable": "stable",
               "mainline": "mainline", "latest": "mainline"}

    if spec.lower() in aliases:
        want = aliases[spec.lower()]
        for rel in list_releases(timeout):
            if rel.moniker == want and rel.source:
                return rel
        raise LookupError(f"kernel.org has no current {want} release with a tarball")

    if not re.fullmatch(r"\d+(\.\d+){1,3}(-rc\d+)?", spec):
        raise ValueError(
            f"{spec!r} is not a kernel version or alias "
            f"(try: lts, stable, mainline, or e.g. 6.12.104)"
        )

    # Explicit version: reuse kernel.org metadata when it matches a current
    # release, otherwise synthesise the canonical CDN URL for older versions.
    try:
        for rel in list_releases(timeout):
            if rel.version == spec and rel.source:
                return rel
    except OSError:
        pass
    if "-rc" in spec:
        # Release candidates are only published as git snapshots, not on the
        # CDN, so a synthesised URL would 404 confusingly.
        raise LookupError(
            f"{spec} is a release candidate no longer offered by kernel.org; "
            f"only current RCs (see 'versions') can be downloaded")
    return Release(moniker="explicit", version=spec, source=tarball_url(spec), released=None)


def tarball_url(version: str) -> str:
    major = version.split(".", 1)[0]
    series = "v2.6" if version.startswith("2.6.") else f"v{major}.x"
    return f"{CDN}/{series}/linux-{version}.tar.xz"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


def download(url: str, dest: Path, quiet: bool = False, retries: int = 5) -> Path:
    """Download with resume. A dropped connection mid-transfer is common on a
    147MB tarball and must not be mistaken for a completed download."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    for attempt in range(1, retries + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resuming = resp.status == 206
                if have and not resuming:
                    have = 0
                    tmp.unlink(missing_ok=True)
                total = int(resp.headers.get("Content-Length") or 0) + have
                got = have
                tty = sys.stderr.isatty()
                step = 0
                with open(tmp, "ab" if have else "wb") as fh:
                    while chunk := resp.read(1 << 20):
                        fh.write(chunk)
                        got += len(chunk)
                        if quiet or not total:
                            continue
                        pct = got * 100 // total
                        if tty:
                            print(f"\r  downloading {_human(got)}/{_human(total)}"
                                  f" ({pct}%)", end="", file=sys.stderr, flush=True)
                        elif pct >= step + 20:
                            step = pct - pct % 20
                            print(f"  downloading {pct}% "
                                  f"({_human(got)}/{_human(total)})",
                                  file=sys.stderr, flush=True)
            if total and got < total:
                raise OSError(f"connection closed after {got} of {total} bytes")
            if not quiet:
                print(file=sys.stderr)
            tmp.replace(dest)
            return dest
        except (OSError, http.client.HTTPException) as exc:
            if attempt == retries:
                raise OSError(f"download failed after {retries} attempts: {exc}")
            if not quiet:
                print(f"\n  {exc} — resuming (attempt {attempt + 1}/{retries})",
                      file=sys.stderr)
            time.sleep(min(2 ** attempt, 15))
    raise OSError("unreachable")


def _expected_sha256(version: str, timeout: int = 30) -> str | None:
    """sha256sums.asc lives beside the tarballs; missing/unreadable is not fatal."""
    major = version.split(".", 1)[0]
    url = f"{CDN}/v{major}.x/sha256sums.asc"
    try:
        text = _get(url, timeout=timeout).decode("utf-8", "replace")
    except OSError:
        return None
    target = f"linux-{version}.tar.xz"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == target:
            return parts[0]
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def extract(tarball: Path, into: Path, quiet: bool = False) -> Path:
    """Unpack linux-X.Y.tar.xz. Uses system tar when present (far faster).

    Extraction happens in a scratch directory that is renamed into place only
    when complete, so an interrupted run can never be mistaken for a full tree.
    """
    into.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print(f"  extracting {tarball.name} ...", file=sys.stderr, flush=True)

    stem = tarball.name.removesuffix(".tar.xz")
    scratch = into / f".extracting-{stem}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()
    try:
        if shutil.which("tar"):
            try:
                subprocess.run(["tar", "-xf", str(tarball), "-C", str(scratch)],
                               check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"tar failed: {exc.stderr.decode('utf-8', 'replace')[:400]}")
        else:
            with tarfile.open(tarball, "r:xz") as tf:
                tf.extractall(scratch, filter="data")

        extracted = scratch / stem
        if not extracted.is_dir():
            raise RuntimeError(f"expected {stem}/ inside {tarball}")
        final = into / stem
        if final.exists():
            shutil.rmtree(final)
        extracted.rename(final)
        return final
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def ensure_source(version: str, keep_tarball: bool = False, quiet: bool = False,
                  verify: bool = True) -> Path:
    """Return a local kernel tree for `version`, downloading it if needed."""
    tree = config.source_path(version)
    if (tree / "MAINTAINERS").is_file() and (tree / "Makefile").is_file():
        if not quiet:
            print(f"  source cached at {tree}", file=sys.stderr)
        return tree

    url = tarball_url(version)
    tarball = config.sources_dir() / f"linux-{version}.tar.xz"
    expect = _expected_sha256(version) if verify else None

    for attempt in (1, 2):
        if not tarball.is_file():
            if not quiet:
                print(f"  fetching {url}", file=sys.stderr)
            download(url, tarball, quiet=quiet)
        if expect is None:
            break
        actual = _sha256(tarball)
        if actual == expect:
            if not quiet:
                print("  sha256 verified against kernel.org", file=sys.stderr)
            break
        tarball.unlink(missing_ok=True)
        tarball.with_name(tarball.name + ".part").unlink(missing_ok=True)
        if attempt == 2:
            raise RuntimeError(
                f"sha256 mismatch for linux-{version}.tar.xz "
                f"(expected {expect[:16]}…, got {actual[:16]}…)"
            )
        if not quiet:
            print("  checksum mismatch — discarding and downloading again",
                  file=sys.stderr)

    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    out = extract(tarball, config.sources_dir(), quiet=quiet)
    if out != tree:
        out.rename(tree)
    if not keep_tarball:
        tarball.unlink(missing_ok=True)
    return tree


def detect_version(tree: Path) -> str | None:
    """Read VERSION/PATCHLEVEL/SUBLEVEL/EXTRAVERSION out of the top Makefile."""
    mk = tree / "Makefile"
    if not mk.is_file():
        return None
    fields: dict[str, str] = {}
    with open(mk, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
            if len(fields) == 4:
                break
    if "VERSION" not in fields or "PATCHLEVEL" not in fields:
        return None
    v = f"{fields['VERSION']}.{fields['PATCHLEVEL']}"
    if fields.get("SUBLEVEL"):
        v += f".{fields['SUBLEVEL']}"
    return v + fields.get("EXTRAVERSION", "")
