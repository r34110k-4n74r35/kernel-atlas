"""Managed-tree identity, provenance, and recoverable source removal.

This module owns local source authentication and filesystem publication
primitives. It does not acquire releases or depend on the acquisition facade.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config


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


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing any destination directory entry.

    ``os.rename`` has the required no-replace contract on Windows, but POSIX
    permits it to replace an empty directory.  Linux and macOS expose explicit
    atomic flags.  Unknown platforms fail closed instead of approximating the
    ownership boundary with a check-then-rename race.
    """
    source = config.require_project_path(source, follow_leaf=False)
    destination = config.require_project_path(destination, follow_leaf=False)
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
    marker = config.require_project_path(
        _source_identity_path(version), follow_leaf=False)
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
    base = config.require_project_path(
        config.sources_dir() / ".kernel-atlas-removing", follow_leaf=False)
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


def _restore_unexpected_quarantine(
        quarantine: Path, source: Path,
        rename: Callable[[Path, Path], None]) -> Path:
    """Best-effort restoration after an entry swap; never replace a new leaf."""
    try:
        rename(quarantine, source)
    except OSError:
        return quarantine
    return source


def prepare_source_removal(
        version: str, expected: ManagedSourceIdentity, *,
        rename: Callable[[Path, Path], None] = _rename_noreplace) \
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

    source = config.require_project_path(
        config.source_path(version), follow_leaf=False)
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
            rename(source, quarantine)
        except FileExistsError as exc:
            raise RuntimeError(
                f"source-removal quarantine {quarantine} appeared while "
                "removal was starting; nothing was deleted") from exc
        moved_info = _entry_info(quarantine)
        if not _identity_matches_entry(current, moved_info):
            recovery = _restore_unexpected_quarantine(quarantine, source, rename)
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


def source_identity_marker(version: str) -> ManagedSourceIdentity | None:
    """Return marker metadata without treating it as current-tree proof."""
    return _read_source_identity_marker(version)


def clear_source_identity(version: str, token: str) -> None:
    """Remove the marker only when it still carries the expected nonce."""
    marker = config.require_project_path(
        _source_identity_path(version), follow_leaf=False)
    identity = _read_source_identity_marker(version)
    if identity is not None and secrets.compare_digest(identity.token, token):
        marker.unlink(missing_ok=True)


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
