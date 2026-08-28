"""Discover, download and unpack Linux kernel source trees from kernel.org."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import config

RELEASES_URL = "https://www.kernel.org/releases.json"
CDN = "https://cdn.kernel.org/pub/linux/kernel"
USER_AGENT = "kernel-atlas/0.1 (+https://kernel.org)"

_ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2")

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


class UnverifiedRCWarning(UserWarning):
    """A kernel.org-generated RC snapshot has no published checksum."""


@contextmanager
def _source_lock(version: str):
    """Serialize download/extraction of one version across processes."""
    lock = config.sources_dir() / f".linux-{version}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as fh:
        if os.name == "nt":
            import msvcrt

            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            # LK_LOCK gives up after ten one-second retries.  Kernel downloads
            # routinely take longer, so use the non-blocking operation in an
            # interruptible loop and wait for the other publisher for as long
            # as necessary.
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_releases(timeout: int = 30) -> list[Release]:
    """Live release list from kernel.org, so no version is ever hardcoded."""
    data = json.loads(_get(RELEASES_URL, timeout=timeout))
    if not isinstance(data, dict):
        raise ValueError("kernel.org release feed is not a JSON object")
    records = data.get("releases")
    if not isinstance(records, list):
        raise ValueError("kernel.org release feed has no releases list")
    out: list[Release] = []
    for position, r in enumerate(records):
        if not isinstance(r, dict):
            raise ValueError(
                f"kernel.org release feed entry {position} is not an object")
        moniker = r.get("moniker")
        version = r.get("version")
        source = r.get("source")
        released_record = r.get("released")
        if not isinstance(moniker, str) or not isinstance(version, str):
            raise ValueError(
                f"kernel.org release feed entry {position} has invalid identity")
        if source is not None and not isinstance(source, str):
            raise ValueError(
                f"kernel.org release feed entry {position} has an invalid source")
        if released_record is not None and not isinstance(released_record, dict):
            raise ValueError(
                f"kernel.org release feed entry {position} has invalid release data")
        released = (released_record or {}).get("isodate")
        if released is not None and not isinstance(released, str):
            raise ValueError(
                f"kernel.org release feed entry {position} has an invalid date")
        out.append(
            Release(
                moniker=moniker,
                version=version,
                source=source,
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
    except (OSError, ValueError):
        pass
    if "-rc" in spec:
        # Release candidates are only published as git snapshots, not on the
        # CDN, so a synthesised URL would 404 confusingly.
        raise LookupError(
            f"{spec} is a release candidate no longer offered by kernel.org; "
            f"only current RCs (see 'versions') can be downloaded")
    return Release(moniker="explicit", version=spec, source=tarball_url(spec), released=None)


def tarball_url(version: str) -> str:
    version = config.validate_version(version)
    match = re.match(r"^(\d+)\.(\d+)", version, flags=re.ASCII)
    if match is None:  # Kept defensive in case the version grammar changes.
        raise ValueError(f"kernel version must include a major and minor: {version!r}")
    major, minor = match.groups()
    # kernel.org split the 1.x and 2.x archives by minor series.  The vN.x
    # directories used by modern releases only begin with Linux 3.x.
    series = f"v{major}.{minor}" if major in {"1", "2"} else f"v{major}.x"
    return f"{CDN}/{series}/linux-{version}.tar.xz"


def _archive_name(url: str) -> str:
    name = Path(urllib.parse.urlsplit(url).path).name
    if not name or not any(name.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES):
        raise ValueError(f"unsupported kernel source archive URL: {url}")
    return name


def _archive_stem(name: str) -> str:
    for suffix in _ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    raise ValueError(f"unsupported kernel source archive: {name}")


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
                length = resp.headers.get("Content-Length")
                try:
                    remaining = int(length) if length else 0
                except (TypeError, ValueError) as exc:
                    raise OSError(
                        f"server returned an invalid Content-Length: {length!r}"
                    ) from exc
                if remaining < 0:
                    raise OSError(
                        f"server returned an invalid Content-Length: {length!r}")
                total = remaining + have
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
            # A complete or oversized stale part elicits 416 forever unless it
            # is discarded.  Restart cleanly on the next attempt.
            reset_range = (isinstance(exc, urllib.error.HTTPError)
                           and exc.code == 416 and have > 0)
            if reset_range:
                tmp.unlink(missing_ok=True)
            if attempt == retries:
                raise OSError(f"download failed after {retries} attempts: {exc}")
            if not quiet:
                action = "restarting" if reset_range else "resuming"
                print(f"\n  {exc} — {action} (attempt {attempt + 1}/{retries})",
                      file=sys.stderr)
            if not reset_range:
                time.sleep(min(2 ** attempt, 15))
    raise OSError("unreachable")


def _expected_sha256(version: str, timeout: int = 30, *,
                     source_url: str | None = None) -> str | None:
    """Return the published hash for an archive, or ``None`` if unavailable."""
    version = config.validate_version(version)
    source_url = source_url or tarball_url(version)
    parsed = urllib.parse.urlsplit(source_url)
    # cgit-generated RC snapshots have no published checksum file.  Do not
    # probe a made-up URL on git.kernel.org.
    if parsed.hostname not in {"cdn.kernel.org", "www.kernel.org"}:
        return None
    url = urllib.parse.urljoin(source_url, "sha256sums.asc")
    try:
        text = _get(url, timeout=timeout).decode("utf-8", "replace")
    except OSError:
        return None
    target = _archive_name(source_url)
    for line in text.splitlines():
        parts = line.split()
        if (len(parts) == 2 and parts[1] == target
                and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0])):
            return parts[0].lower()
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _checked_archive(tarball: Path) -> None:
    """Reject archive members that could escape the extraction directory.

    The system ``tar`` fast path does not provide Python's extraction filters,
    so validate names, links, and special files before invoking it.  This also
    gives Python 3.10 the same protection without relying on the newer
    ``filter='data'`` argument.
    """

    def parts_within(value: str, base=(), *, allow_parent: bool) -> tuple[str, ...]:
        if "\\" in value or re.match(r"^[A-Za-z]:", value):
            raise RuntimeError(f"archive contains a non-portable path: {value!r}")
        path = PurePosixPath(value)
        if path.is_absolute():
            raise RuntimeError(f"archive contains an absolute path: {value!r}")
        parts = list(base)
        for part in path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not allow_parent or not parts:
                    raise RuntimeError(f"archive path escapes extraction root: {value!r}")
                parts.pop()
            else:
                parts.append(part)
        return tuple(parts)

    try:
        with tarfile.open(tarball, "r:*") as tf:
            for member in tf.getmembers():
                member_parts = parts_within(member.name, allow_parent=False)
                if member.isdev():
                    raise RuntimeError(
                        f"archive contains a special device: {member.name!r}")
                if member.issym():
                    parts_within(
                        member.linkname, member_parts[:-1], allow_parent=True)
                elif member.islnk():
                    parts_within(member.linkname, allow_parent=True)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"could not validate {tarball.name}: {exc}") from exc


def extract(tarball: Path, into: Path, quiet: bool = False) -> Path:
    """Unpack a kernel tar archive. Uses system tar when present (far faster).

    Extraction happens in a scratch directory that is renamed into place only
    when complete, so an interrupted run can never be mistaken for a full tree.
    """
    into.mkdir(parents=True, exist_ok=True)
    if not quiet:
        print(f"  extracting {tarball.name} ...", file=sys.stderr, flush=True)

    stem = _archive_stem(tarball.name)
    scratch = Path(tempfile.mkdtemp(prefix=f".extracting-{stem}-", dir=into))
    try:
        _checked_archive(tarball)
        if shutil.which("tar"):
            try:
                subprocess.run(["tar", "-xf", str(tarball), "-C", str(scratch)],
                               check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"tar failed: {exc.stderr.decode('utf-8', 'replace')[:400]}")
        else:
            with tarfile.open(tarball, "r:*") as tf:
                tf.extractall(scratch)

        extracted = scratch / stem
        if extracted.is_symlink() or not extracted.is_dir():
            raise RuntimeError(
                f"expected a real {stem}/ directory inside {tarball}")
        final = into / stem
        if final.exists():
            # Another concurrent extraction may already have published the
            # same complete tree.  Never delete a destination here.
            return final
        try:
            extracted.rename(final)
        except OSError:
            # Concurrent directory publication is reported as EEXIST on some
            # platforms and ENOTEMPTY on others.
            if final.is_dir():
                return final
            raise
        return final
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _is_kernel_org_rc_snapshot(version: str, source_url: str) -> bool:
    parsed = urllib.parse.urlsplit(source_url)
    return (
        "-rc" in version
        and parsed.scheme == "https"
        and parsed.hostname == "git.kernel.org"
        and Path(parsed.path).name == f"linux-{version}.tar.gz"
    )


def ensure_source(version: str, keep_tarball: bool = False, quiet: bool = False,
                  verify: bool = True, source_url: str | None = None) -> Path:
    """Return a local kernel tree, serializing same-version acquisition."""
    version = config.validate_version(version)
    with _source_lock(version):
        return _ensure_source_locked(
            version, keep_tarball=keep_tarball, quiet=quiet, verify=verify,
            source_url=source_url)


def _ensure_source_locked(version: str, keep_tarball: bool = False,
                          quiet: bool = False, verify: bool = True,
                          source_url: str | None = None) -> Path:
    """Implementation of :func:`ensure_source` while its version lock is held."""
    tree = config.source_path(version)
    if (tree / "MAINTAINERS").is_file() and (tree / "Makefile").is_file():
        actual_version = detect_version(tree)
        if actual_version != version:
            raise RuntimeError(
                f"cached source at {tree} reports Linux "
                f"{actual_version or 'unknown'}, not {version}; move or remove it"
            )
        if not quiet:
            print(f"  source cached at {tree}", file=sys.stderr)
        return tree

    url = source_url or tarball_url(version)
    if verify and urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise RuntimeError(
            f"refusing to verify kernel source over a non-HTTPS URL: {url}")
    try:
        archive_name = _archive_name(url)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if _archive_stem(archive_name) != f"linux-{version}":
        raise RuntimeError(
            f"source archive {archive_name!r} does not match Linux {version}"
        )
    tarball = config.sources_dir() / archive_name
    expect = (_expected_sha256(version, source_url=url) if verify else None)
    if verify and expect is None:
        if _is_kernel_org_rc_snapshot(version, url):
            warnings.warn(
                f"Linux {version} is a kernel.org-generated RC snapshot with no "
                "published checksum; proceeding with its HTTPS source archive",
                UnverifiedRCWarning,
                stacklevel=2,
            )
        else:
            raise RuntimeError(
                f"no published sha256 checksum is available for {archive_name}; "
                "refusing an unverified download (pass --no-verify to override)"
            )

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
                f"sha256 mismatch for {archive_name} "
                f"(expected {expect[:16]}…, got {actual[:16]}…)"
            )
        if not quiet:
            print("  checksum mismatch — discarding and downloading again",
                  file=sys.stderr)

    if tree.is_symlink() or tree.is_file():
        tree.unlink()
    elif tree.exists():
        shutil.rmtree(tree)
    out = extract(tarball, config.sources_dir(), quiet=quiet)
    if out != tree:
        raise RuntimeError(f"archive extracted to unexpected directory {out}")
    actual_version = detect_version(tree)
    if actual_version != version:
        raise RuntimeError(
            f"downloaded source reports Linux {actual_version or 'unknown'}, "
            f"not {version}"
        )
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
    if (re.fullmatch(r"[0-9]+", fields["VERSION"]) is None
            or re.fullmatch(r"[0-9]+", fields["PATCHLEVEL"]) is None):
        return None
    sublevel = fields.get("SUBLEVEL", "")
    if sublevel and re.fullmatch(r"[0-9]+", sublevel) is None:
        return None
    try:
        major = int(fields["VERSION"])
    except ValueError:
        return None
    v = f"{fields['VERSION']}.{fields['PATCHLEVEL']}"
    # Modern release/tag names omit the Makefile's conventional .0 (3.0,
    # 7.2), while historical 2.x archives include it (notably 2.6.0).
    if sublevel and (sublevel != "0" or major <= 2):
        v += f".{sublevel}"
    v += fields.get("EXTRAVERSION", "")
    try:
        return config.validate_version(v)
    except ValueError:
        return None
