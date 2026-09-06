"""Discover, download and unpack Linux kernel source trees from kernel.org."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import __version__, config
from .presentation.build import note
from .presentation.progress import Progress
from .storage import managed_source as _managed_source
from .storage.locks import (
    _file_lock as _file_lock,
    _output_lock_paths as _output_lock_paths,
    output_lock as output_lock,
    pin_lock as pin_lock,
    source_lock as source_lock,
)
from .storage.managed_source import (
    ManagedSourceIdentity as ManagedSourceIdentity,
    ManagedSourceRemoval as ManagedSourceRemoval,
    _rename_noreplace as _rename_noreplace,
    _source_identity_path as _source_identity_path,
    _source_quarantine_base as _source_quarantine_base,
    _store_source_identity as _store_source_identity,
    _write_source_identity as _write_source_identity,
    clear_source_identity as clear_source_identity,
    detect_version as detect_version,
    managed_source_identity as managed_source_identity,
    managed_source_version as managed_source_version,
    source_identity_marker as source_identity_marker,
    source_quarantine_path as source_quarantine_path,
)

RELEASES_URL = "https://www.kernel.org/releases.json"
CDN = "https://cdn.kernel.org/pub/linux/kernel"
USER_AGENT = (
    f"kernel-atlas/{__version__} "
    "(+https://github.com/r34110k-4n74r35/kernel-atlas)"
)

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


# Compatibility for callers which used the historical private lock name.
_source_lock = source_lock


def prepare_source_removal(version: str,
                           expected: ManagedSourceIdentity) \
        -> ManagedSourceRemoval | None:
    """Authenticate and quarantine a managed source before removal.

    Share extraction's publication primitive so callers can consistently
    instrument or fault-test both filesystem transitions.
    """
    return _managed_source.prepare_source_removal(
        version, expected, rename=_rename_noreplace)


def begin_source_removal(version: str,
                         expected: ManagedSourceIdentity) \
        -> ManagedSourceIdentity | None:
    """Compatibility view of :func:`prepare_source_removal`."""
    removal = prepare_source_removal(version, expected)
    return removal.identity if removal is not None else None


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except http.client.HTTPException as exc:
        # Callers already treat OSError as a recoverable network failure.  A
        # truncated HTTP body is the same class of failure, not a programmer
        # error which should escape as a traceback.
        raise OSError(f"incomplete HTTP response from {url}: {exc}") from exc


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


def _regular_part_info(path: Path) -> os.stat_result | None:
    """Return a resumable part's identity, rejecting links/special files."""
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError(
            f"refusing unsafe partial download path {path}; remove it manually")
    return info


def _same_part(left: os.stat_result | None,
               right: os.stat_result | None) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.st_dev, left.st_ino, left.st_mode, left.st_nlink,
        left.st_size, left.st_mtime_ns,
    ) == (
        right.st_dev, right.st_ino, right.st_mode, right.st_nlink,
        right.st_size, right.st_mtime_ns,
    )


def _unlink_download_part(path: Path, expected: os.stat_result) -> None:
    """Unlink only the same regular partial file observed by the caller."""
    path = config.require_project_path(path, follow_leaf=False)
    current = _regular_part_info(path)
    if not _same_part(current, expected):
        raise OSError(
            f"partial download changed before cleanup: {path}; kept for safety")
    path.unlink()


_EXPECTED_PART_UNSET = object()


def _open_download_part(path: Path, *, append: bool,
                        expected=_EXPECTED_PART_UNSET):
    """Open a verified regular part without truncating through a link."""
    path = config.require_project_path(path, follow_leaf=False)
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        before = None
    if before is not None and (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise OSError(
            f"refusing unsafe partial download path {path}; remove it manually")
    if expected is not _EXPECTED_PART_UNSET and not _same_part(before, expected):
        raise OSError(
            f"partial download changed before it was opened: {path}; "
            "kept for safety")

    flags = os.O_WRONLY
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        leaf = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (leaf.st_dev, leaf.st_ino)
                or (before is not None
                    and (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino))):
            raise OSError(
                f"refusing unsafe partial download path {path}; "
                "remove it manually")
        if append:
            os.lseek(fd, 0, os.SEEK_END)
        else:
            os.ftruncate(fd, 0)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def download(url: str, dest: Path, quiet: bool = False, retries: int = 5) -> Path:
    """Download with resume. A dropped connection mid-transfer is common on a
    147MB tarball and must not be mistaken for a completed download."""
    dest = config.require_project_path(dest, follow_leaf=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    for attempt in range(1, retries + 1):
        part_info = _regular_part_info(tmp)
        have = part_info.st_size if part_info is not None else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resuming = resp.status == 206
                if have and not resuming:
                    have = 0
                    assert part_info is not None
                    _unlink_download_part(tmp, part_info)
                    part_info = None
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
                total = remaining + have if length is not None else None
                got = have
                with Progress("Downloading source", total=total, unit="bytes",
                              initial=have, quiet=quiet,
                              detail=f"attempt {attempt}/{retries}") as progress:
                    with _open_download_part(
                            tmp, append=bool(have), expected=part_info) as fh:
                        while chunk := resp.read(1 << 20):
                            fh.write(chunk)
                            got += len(chunk)
                            progress.update(got)
                    if total is not None and got < total:
                        raise OSError(f"connection closed after {got} of {total} bytes")
            _rename_noreplace(tmp, dest)
            return dest
        except (OSError, http.client.HTTPException) as exc:
            # A complete or oversized stale part elicits 416 forever unless it
            # is discarded.  Restart cleanly on the next attempt.
            reset_range = (isinstance(exc, urllib.error.HTTPError)
                           and exc.code == 416 and have > 0)
            if reset_range:
                assert part_info is not None
                _unlink_download_part(tmp, part_info)
            if attempt == retries:
                raise OSError(f"download failed after {retries} attempts: {exc}")
            action = "restarting" if reset_range else "resuming"
            note("Retry", f"{exc} — {action} (attempt {attempt + 1}/{retries})",
                 tone="warning", quiet=quiet)
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


def extract(tarball: Path, into: Path, quiet: bool = False, *,
            require_new: bool = False) -> Path:
    """Unpack a kernel tar archive. Uses system tar when present (far faster).

    Extraction happens in a scratch directory that is renamed into place only
    when complete, so an interrupted run can never be mistaken for a full tree.
    """
    into = config.require_project_path(into)
    into.mkdir(parents=True, exist_ok=True)
    stem = _archive_stem(tarball.name)
    scratch = Path(tempfile.mkdtemp(prefix=f".extracting-{stem}-", dir=into))
    try:
        with Progress("Checking archive", detail=tarball.name, quiet=quiet):
            _checked_archive(tarball)
        with Progress("Extracting source", detail=tarball.name, quiet=quiet):
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
            if final.exists() or final.is_symlink():
                # Another concurrent extraction may already have published the
                # same complete tree.  Never delete a destination here.
                if require_new:
                    raise RuntimeError(
                        f"source destination {final} appeared during extraction; "
                        "it has not been claimed as tool-owned")
                return final
            try:
                _rename_noreplace(extracted, final)
            except FileExistsError as exc:
                if require_new:
                    raise RuntimeError(
                        f"source destination {final} appeared during extraction; "
                        "it has not been claimed as tool-owned") from exc
                return final
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
    with source_lock(version):
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
        note("Source", f"cached at {tree}", quiet=quiet)
        return tree
    if tree.exists() or tree.is_symlink():
        # Atomic extraction never publishes a partial destination.  Therefore
        # an unrecognizable entry here is not proven tool-owned and an ordinary
        # build must not recursively delete it.
        raise RuntimeError(
            f"source destination {tree} already exists but is not a complete "
            f"Linux {version} tree; move or remove it explicitly")

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
    tarball = config.require_project_path(
        config.sources_dir() / archive_name, follow_leaf=False)
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
            note("Fetch", url, quiet=quiet)
            download(url, tarball, quiet=quiet)
        if expect is None:
            break
        with Progress("Verifying archive checksum", quiet=quiet):
            actual = _sha256(tarball)
        if actual == expect:
            note("Checksum", "sha256 verified against kernel.org",
                 tone="success", quiet=quiet)
            break
        config.require_project_path(
            tarball, follow_leaf=False).unlink(missing_ok=True)
        config.require_project_path(
            tarball.with_name(tarball.name + ".part"),
            follow_leaf=False).unlink(missing_ok=True)
        if attempt == 2:
            raise RuntimeError(
                f"sha256 mismatch for {archive_name} "
                f"(expected {expect[:16]}…, got {actual[:16]}…)"
            )
        note("Checksum", "mismatch — discarding and downloading again",
             tone="warning", quiet=quiet)

    out = extract(
        tarball, config.sources_dir(), quiet=quiet, require_new=True)
    if out != tree:
        raise RuntimeError(f"archive extracted to unexpected directory {out}")
    actual_version = detect_version(tree)
    if actual_version != version:
        raise RuntimeError(
            f"downloaded source reports Linux {actual_version or 'unknown'}, "
            f"not {version}"
        )
    # The sidecar is outside the indexed tree.  Its nonce and root identity
    # prove ownership for a future recursive removal; its digest distinguishes
    # this pristine extraction from an edited or replacement tree.
    authoritative = bool(
        verify and (expect is not None or _is_kernel_org_rc_snapshot(version, url)))
    with Progress("Recording source identity", quiet=quiet):
        _write_source_identity(
            version, tree, url, authoritative=authoritative)
    if not keep_tarball:
        config.require_project_path(
            tarball, follow_leaf=False).unlink(missing_ok=True)
    return tree
