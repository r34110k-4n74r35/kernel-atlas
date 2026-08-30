"""Discover, download and unpack Linux kernel source trees from kernel.org."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import __version__, config

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


@dataclass(frozen=True)
class ManagedSourceIdentity:
    """Persistent proof that a managed tree is the one the tool published."""

    token: str
    device: int
    inode: int
    digest: str
    source: str
    authoritative: bool
    removing: bool = False


@dataclass(frozen=True)
class ManagedSourceRemoval:
    """An identity-bound source tree isolated from its conventional path."""

    identity: ManagedSourceIdentity
    quarantine: Path
    already_absent: bool = False


_LOCK_STATE = threading.local()


def _open_regular_lock(path: Path):
    """Open/create a lock leaf without ever writing through a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        leaf = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (leaf.st_dev, leaf.st_ino)):
            raise OSError(f"refusing unsafe lifecycle lock path {path}")
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _file_lock(lock: Path):
    """Take one re-entrant, cross-process exclusive lifecycle lock."""
    lock = lock.parent.resolve() / lock.name
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = _LOCK_STATE.held = {}
    key = str(lock)
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    with _open_regular_lock(lock) as fh:
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
                    if (exc.errno not in {
                            errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                            and getattr(exc, "winerror", None) not in {33, 36}):
                        raise
                    time.sleep(0.1)
            try:
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def source_lock(version: str):
    """Serialize every use or mutation of one managed source tree."""
    version = config.validate_version(version)
    lock = config.sources_dir() / f".linux-{version}.lock"
    with _file_lock(lock):
        yield


# Compatibility for callers and tests which used the historical private name.
_source_lock = source_lock


@contextmanager
def output_lock(path: Path):
    """Serialize one index leaf and every existing symlink alias to it.

    A symlink publication replaces the alias leaf, rather than its target.  An
    alias operation therefore takes both locks: the lexical lock bridges that
    replacement transition, while the target lock makes operations through the
    alias converge with operations on the real index.  Rechecking after all
    locks are held closes races between cooperating lifecycle commands.
    """
    path = Path(path).expanduser()
    for _ in range(16):
        before = _output_lock_paths(path)
        with ExitStack() as locks:
            for lock in before:
                locks.enter_context(_file_lock(lock))
            if _output_lock_paths(path) != before:
                continue
            yield
            return
    raise OSError(f"index output kept changing while acquiring its lock: {path}")


def _output_lock_paths(path: Path) -> tuple[Path, ...]:
    """Stable, deterministically ordered lock leaves for an output spelling."""
    lexical = path.parent.resolve() / path.name
    identities = {lexical}
    try:
        if lexical.is_symlink():
            identities.add(lexical.resolve(strict=False))
    except OSError:
        # The post-acquisition comparison makes a transient resolution failure
        # conservative: an operation can proceed only if it sees the same set.
        identities.add(lexical)
    # Keep locks in the application's own registry.  An alias may point at a
    # missing path under an arbitrary parent; merely locking that alias must not
    # create the target's directories or write beside it.
    registry = config.index_dir() / ".lifecycle-locks"
    locks = {
        registry / (
            "output-"
            + hashlib.sha256(os.fsencode(str(identity))).hexdigest()
            + ".lock"
        )
        for identity in identities
    }
    return tuple(sorted(locks, key=lambda item: os.fsencode(str(item))))


@contextmanager
def pin_lock():
    """Serialize reads followed by writes of the default-version pin."""
    lock = config.index_dir() / ".default-version.lock"
    with _file_lock(lock):
        yield


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing any destination directory entry.

    ``os.rename`` has the required no-replace contract on Windows, but POSIX
    permits it to replace an empty directory.  Linux and macOS expose explicit
    atomic flags.  Unknown platforms fail closed instead of approximating the
    ownership boundary with a check-then-rename race.
    """
    source = Path(source)
    destination = Path(destination)
    if os.name == "nt":
        os.rename(source, destination)
        return

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    old = os.fsencode(source)
    new = os.fsencode(destination)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable on this Linux libc",
                str(destination),
            )
        renameat2.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, old, -100, new, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable on this macOS release",
                str(destination),
            )
        renamex_np.argtypes = (
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
        )
        renamex_np.restype = ctypes.c_int
        result = renamex_np(old, new, 0x00000004)  # RENAME_EXCL
    else:
        raise OSError(
            errno.ENOTSUP,
            "this platform has no supported atomic no-replace rename",
            str(destination),
        )
    if result != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(destination))


def managed_source_version(path: Path) -> str | None:
    """Return the canonical managed source version containing *path*.

    Resolve aliases before looking at their spelling.  Thus
    ``kernels/linux-study -> kernels/linux-7.2`` locks ``7.2``, the same lock
    used by removal of the real tree.  A lexical fallback retains the safe
    behaviour for a conventional managed leaf which itself points elsewhere:
    removal can only unlink that leaf, but must still serialize with its users.
    """
    supplied = Path(path).expanduser()
    root = config.sources_dir().resolve()
    try:
        resolved = supplied.resolve()
        relative = resolved.relative_to(root)
        first = relative.parts[0] if relative.parts else ""
        if first.startswith("linux-"):
            version = config.validate_version(first[len("linux-"):])
            managed_root = config.source_path(version).resolve()
            resolved.relative_to(managed_root)
            return version
    except (IndexError, OSError, ValueError):
        pass

    try:
        leaf = supplied.parent.resolve() / supplied.name
        if leaf.parent == root and leaf.name.startswith("linux-"):
            return config.validate_version(leaf.name[len("linux-"):])
    except (OSError, ValueError):
        pass
    return None


def _source_identity_path(version: str) -> Path:
    version = config.validate_version(version)
    return config.sources_dir() / f".linux-{version}.source.json"


def _tree_digest(tree: Path) -> str:
    """Hash one tree without following links, detecting concurrent mutation."""
    tree = Path(tree)
    root_info = tree.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode):
        raise OSError(f"managed source is not a real directory: {tree}")
    digest = hashlib.sha256()

    def stable(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns)

    def add_path(kind: bytes, relative: str, mode: int) -> None:
        encoded = relative.encode("utf-8", "surrogateescape")
        digest.update(kind + b"\0" + encoded + b"\0")
        digest.update(f"{stat.S_IMODE(mode):o}".encode("ascii") + b"\0")

    def visit(directory: Path, prefix: str,
              expected: os.stat_result | None = None) -> None:
        before = directory.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(before.st_mode)
                or (expected is not None
                    and (before.st_dev, before.st_ino)
                    != (expected.st_dev, expected.st_ino))):
            raise OSError(
                f"managed source directory changed while hashing {directory}")
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            info = entry.stat(follow_symlinks=False)
            entry_path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                add_path(b"d", relative, info.st_mode)
                visit(entry_path, relative, info)
            elif stat.S_ISREG(info.st_mode):
                add_path(b"f", relative, info.st_mode)
                digest.update(str(info.st_size).encode("ascii") + b"\0")
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(entry_path, flags)
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (info.st_dev, info.st_ino)):
                    os.close(fd)
                    raise OSError(
                        f"managed source file changed while hashing {entry_path}")
                with os.fdopen(fd, "rb") as stream:
                    while chunk := stream.read(1 << 20):
                        digest.update(chunk)
                digest.update(b"\0")
                if stable(info) != stable(entry_path.stat(follow_symlinks=False)):
                    raise OSError(
                        f"managed source changed while hashing {entry_path}")
            elif stat.S_ISLNK(info.st_mode):
                add_path(b"l", relative, info.st_mode)
                digest.update(os.readlink(entry_path).encode(
                    "utf-8", "surrogateescape") + b"\0")
                if stable(info) != stable(entry_path.stat(follow_symlinks=False)):
                    raise OSError(
                        f"managed source changed while hashing {entry_path}")
            else:
                raise OSError(
                    f"managed source contains a special file: {entry_path}")
        if stable(before) != stable(directory.stat(follow_symlinks=False)):
            raise OSError(
                f"managed source changed while hashing {directory}")

    visit(tree, "", root_info)
    if stable(root_info) != stable(tree.stat(follow_symlinks=False)):
        raise OSError(f"managed source changed while hashing {tree}")
    return digest.hexdigest()


def _write_source_identity(version: str, tree: Path, source: str, *,
                           authoritative: bool) -> ManagedSourceIdentity:
    """Record ownership and pristine content after atomic extraction."""
    info = tree.stat(follow_symlinks=False)
    identity = ManagedSourceIdentity(
        token=secrets.token_hex(32), device=info.st_dev, inode=info.st_ino,
        digest=_tree_digest(tree), source=source,
        authoritative=bool(authoritative),
    )
    _store_source_identity(version, identity)
    return identity


def _store_source_identity(version: str,
                           identity: ManagedSourceIdentity) -> None:
    """Atomically persist an already validated source identity."""
    marker = _source_identity_path(version)
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".tmp", dir=marker.parent)
    temporary = Path(temporary_name)
    payload = {
        "format": 1, "version": version, "token": identity.token,
        "device": identity.device, "inode": identity.inode,
        "digest": identity.digest, "source": identity.source,
        "authoritative": identity.authoritative,
        "removing": identity.removing,
    }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_source_identity_marker(version: str) \
        -> ManagedSourceIdentity | None:
    """Read and validate the marker without making claims about the tree."""
    version = config.validate_version(version)
    marker = _source_identity_path(version)
    try:
        with _open_regular_existing(marker) as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return None
        payload = json.loads(raw.decode("utf-8"))
        identity = ManagedSourceIdentity(
            token=payload["token"], device=payload["device"],
            inode=payload["inode"], digest=payload["digest"],
            source=payload["source"],
            authoritative=payload["authoritative"],
            removing=payload.get("removing", False),
        )
        if (payload.get("format") != 1 or payload.get("version") != version
                or re.fullmatch(r"[0-9a-f]{64}", identity.token) is None
                or re.fullmatch(r"[0-9a-f]{64}", identity.digest) is None
                or type(identity.device) is not int or identity.device < 0
                or type(identity.inode) is not int or identity.inode < 0
                or not isinstance(identity.source, str) or not identity.source
                or type(identity.authoritative) is not bool
                or type(identity.removing) is not bool):
            return None
        return identity
    except (KeyError, OSError, TypeError, ValueError, UnicodeDecodeError):
        return None


def managed_source_identity(version: str, tree: Path, *,
                            verify_content: bool = True,
                            allow_removing: bool = False) \
        -> ManagedSourceIdentity | None:
    """Read a safe identity marker and verify it still names this tree."""
    version = config.validate_version(version)
    try:
        identity = _read_source_identity_marker(version)
        if identity is None or (identity.removing and not allow_removing):
            return None
        info = Path(tree).stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino)
                != (identity.device, identity.inode)):
            return None
        if (verify_content and not identity.removing
                and _tree_digest(Path(tree)) != identity.digest):
            return None
        return identity
    except (OSError, ValueError):
        return None


def _open_regular_existing(path: Path):
    """Open a small sidecar without following or trusting another file."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        leaf = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (leaf.st_dev, leaf.st_ino)):
            raise OSError(f"refusing unsafe source identity marker {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _entry_info(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity_matches_entry(identity: ManagedSourceIdentity,
                            info: os.stat_result | None) -> bool:
    return bool(
        info is not None and stat.S_ISDIR(info.st_mode)
        and (info.st_dev, info.st_ino) == (identity.device, identity.inode)
    )


def _same_source_identity(left: ManagedSourceIdentity,
                          right: ManagedSourceIdentity) -> bool:
    return (
        secrets.compare_digest(left.token, right.token)
        and left.device == right.device and left.inode == right.inode
        and secrets.compare_digest(left.digest, right.digest)
        and left.source == right.source
        and left.authoritative == right.authoritative
    )


def _source_quarantine_base(*, create: bool) -> Path:
    base = config.sources_dir() / ".kernel-atlas-removing"
    if create:
        base.parent.mkdir(parents=True, exist_ok=True)
        try:
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = _entry_info(base)
    if info is None:
        return base
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(
            f"unsafe source-removal quarantine {base}; expected a real directory")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(
            f"unsafe source-removal quarantine permissions at {base}; "
            "expected mode 0700")
    return base


def source_quarantine_path(identity: ManagedSourceIdentity) -> Path:
    """The private, nonce-derived deletion path for one managed source."""
    if re.fullmatch(r"[0-9a-f]{64}", identity.token) is None:
        raise ValueError("invalid managed source identity token")
    return (_source_quarantine_base(create=False)
            / f"source-{identity.token}")


def _restore_unexpected_quarantine(quarantine: Path, source: Path) -> Path:
    """Best-effort restoration after an entry swap; never replace a new leaf."""
    try:
        _rename_noreplace(quarantine, source)
    except OSError:
        return quarantine
    return source


def prepare_source_removal(version: str,
                           expected: ManagedSourceIdentity) \
        -> ManagedSourceRemoval | None:
    """Move the authenticated source to a private quarantine before deletion.

    The conventional ``kernels/linux-V`` leaf is never passed to ``rmtree``.
    A crash after the atomic move is recoverable from the persistent nonce, and
    anything subsequently created at the conventional path remains untouched.
    """
    version = config.validate_version(version)
    current = _read_source_identity_marker(version)
    if current is None or not _same_source_identity(current, expected):
        return None

    source = config.source_path(version)
    base = _source_quarantine_base(create=False)
    quarantine = base / f"source-{current.token}"
    source_info = _entry_info(source)
    quarantine_info = _entry_info(quarantine)

    if current.removing:
        if quarantine_info is not None:
            if not _identity_matches_entry(current, quarantine_info):
                raise RuntimeError(
                    f"source-removal quarantine {quarantine} no longer contains "
                    "the tree recorded by the index; inspect it manually")
            return ManagedSourceRemoval(current, quarantine)
        if _identity_matches_entry(current, source_info):
            raise RuntimeError(
                f"in-progress source {source} was moved out of its quarantine; "
                "the index and source were kept for manual recovery")
        # The old owned root is gone.  A different entry at the conventional
        # spelling was created later and must remain completely untouched.
        return ManagedSourceRemoval(current, quarantine, already_absent=True)

    candidate = source
    candidate_info = source_info
    if quarantine_info is not None:
        # Crash recovery: the atomic move completed before the marker update.
        if not _identity_matches_entry(current, quarantine_info):
            raise RuntimeError(
                f"source-removal quarantine {quarantine} is occupied by an "
                "unrelated entry; source and index kept")
        candidate = quarantine
        candidate_info = quarantine_info
    elif source_info is None:
        return ManagedSourceRemoval(current, quarantine, already_absent=True)

    if not _identity_matches_entry(current, candidate_info):
        raise RuntimeError(
            "the current source entry is not the pristine tool-owned source "
            f"for Linux {version} recorded by this index")
    if (_tree_digest(candidate) != current.digest
            or detect_version(candidate) != version
            or not (candidate / "MAINTAINERS").is_file()):
        raise RuntimeError(
            f"the current Linux {version} tree is not the pristine tool-owned "
            "source recorded by this index")

    if candidate == source:
        _source_quarantine_base(create=True)
        try:
            _rename_noreplace(source, quarantine)
        except FileExistsError as exc:
            raise RuntimeError(
                f"source-removal quarantine {quarantine} appeared while "
                "removal was starting; nothing was deleted") from exc
        moved_info = _entry_info(quarantine)
        if not _identity_matches_entry(current, moved_info):
            recovery = _restore_unexpected_quarantine(quarantine, source)
            raise RuntimeError(
                "the source entry changed while it was being quarantined; "
                f"nothing was deleted and the moved entry is at {recovery}")
        # Rehash after the atomic move so an in-place writer cannot slip a
        # modified tree through the earlier validation window.
        if (_tree_digest(quarantine) != current.digest
                or detect_version(quarantine) != version):
            raise RuntimeError(
                f"source changed while entering quarantine {quarantine}; "
                "nothing was deleted, inspect that directory manually")

    removing = ManagedSourceIdentity(
        token=current.token, device=current.device, inode=current.inode,
        digest=current.digest, source=current.source,
        authoritative=current.authoritative, removing=True,
    )
    _store_source_identity(version, removing)
    return ManagedSourceRemoval(removing, quarantine)


def begin_source_removal(version: str,
                         expected: ManagedSourceIdentity) \
        -> ManagedSourceIdentity | None:
    """Compatibility view of :func:`prepare_source_removal`."""
    removal = prepare_source_removal(version, expected)
    return removal.identity if removal is not None else None


def source_identity_marker(version: str) -> ManagedSourceIdentity | None:
    """Return marker metadata without treating it as current-tree proof."""
    return _read_source_identity_marker(version)


def clear_source_identity(version: str, token: str) -> None:
    """Remove the marker only when it still carries the expected nonce."""
    marker = _source_identity_path(version)
    identity = _read_source_identity_marker(version)
    if identity is not None and secrets.compare_digest(identity.token, token):
        marker.unlink(missing_ok=True)


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


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


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


def _regular_part_size(path: Path) -> int:
    """Compatibility size view of :func:`_regular_part_info`."""
    info = _regular_part_info(path)
    return info.st_size if info is not None else 0


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
    current = _regular_part_info(path)
    if not _same_part(current, expected):
        raise OSError(
            f"partial download changed before cleanup: {path}; kept for safety")
    path.unlink()


_EXPECTED_PART_UNSET = object()


def _open_download_part(path: Path, *, append: bool,
                        expected=_EXPECTED_PART_UNSET):
    """Open a verified regular part without truncating through a link."""
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
                total = remaining + have
                got = have
                tty = sys.stderr.isatty()
                step = 0
                with _open_download_part(
                        tmp, append=bool(have), expected=part_info) as fh:
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


def extract(tarball: Path, into: Path, quiet: bool = False, *,
            require_new: bool = False) -> Path:
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
        if not quiet:
            print(f"  source cached at {tree}", file=sys.stderr)
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
    _write_source_identity(
        version, tree, url, authoritative=authoritative)
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
