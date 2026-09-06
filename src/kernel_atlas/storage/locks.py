"""Reentrant process locks for managed sources, index outputs, and pins.

Lock identities and their filesystem checks are shared by build, selection,
and removal so all lifecycle operations serialize on the same entries.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import config


_LOCK_STATE = threading.local()


def _open_regular_lock(path: Path):
    """Open/create a lock leaf without ever writing through a symlink."""
    path = config.require_project_path(path, follow_leaf=False)
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
    lock = config.require_project_path(lock, follow_leaf=False)
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


@contextmanager
def output_lock(path: Path):
    """Serialize one index leaf and every existing symlink alias to it.

    A symlink publication replaces the alias leaf, rather than its target.  An
    alias operation therefore takes both locks: the lexical lock bridges that
    replacement transition, while the target lock makes operations through the
    alias converge with operations on the real index.  Rechecking after all
    locks are held closes races between cooperating lifecycle commands.
    """
    path = config.require_project_path(path, follow_leaf=False)
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
