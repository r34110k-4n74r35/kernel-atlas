"""Index selection, connection lifetime, and safe removal authorization."""

from __future__ import annotations

import re
import sqlite3
import stat
import sys
from pathlib import Path

from .. import config, db
from ..presentation import terminal
from .output import PROG, _die


def _version_key(path: Path) -> tuple:
    """Sort by the leading numeric release, including vendor/local suffixes.

    For one numeric base, final releases sort above deterministic vendor/local
    suffixes, which sort above release candidates.  The suffixes themselves do
    not have a universal version scheme, but they must not make Linux 6.6 sort
    below every plain numeric version such as Linux 6.1.
    """
    stem = path.stem
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", stem)
    if not m:
        return (0, (), 0, 0, stem)
    numbers = tuple(int(p) for p in m.group(1).split("."))
    suffix = m.group(2)
    rc = re.fullmatch(r"-rc(\d+)", suffix)
    phase = 2 if not suffix else (0 if rc else 1)
    # For the same numeric release: final > local/vendor > rc10 > rc2.
    return (1, numbers, phase, int(rc.group(1)) if rc else 0, suffix, stem)


def version_prefix_match(stem: str, spec: str) -> bool:
    """True if `spec` is `stem` or a prefix of it at a version-component boundary.

    ``6.18`` matches ``6.18.45``; ``6.1`` does not. ``next`` matches
    ``next-20260101``. String ``startswith`` would treat ``6.1`` as a prefix of
    ``6.18.45``, which is how you accidentally pin the wrong LTS.
    """
    if not spec or not stem:
        return False
    if stem == spec:
        return True
    return stem.startswith(spec + ".") or stem.startswith(spec + "-")


def _index_version_key(path: Path) -> tuple:
    """Sort usable indexes by recorded version, ahead of corrupt aliases."""
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        version = db.validate_schema(conn).get("kernel_version")
        version = config.validate_version(version)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return (0, _version_key(path))
    finally:
        if conn is not None:
            conn.close()
    return (1, _version_key(Path(f"{version}.db")))


def _same_path(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def resolve_index_spec(spec: str) -> Path:
    """Turn a version or unique version prefix into an index path, or die."""
    try:
        spec = config.validate_version(spec)
        path = config.index_path(spec)
    except ValueError as exc:
        _die(str(exc))
    if path.is_file() or path.is_symlink():
        return path
    matches = [p for p in config.list_indexes() if version_prefix_match(p.stem, spec)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _die(f"{spec!r} is ambiguous: " + ", ".join(p.stem for p in matches))
    have = ", ".join(p.stem for p in config.list_indexes()) or "none built yet"
    _die(f"no index for {spec!r} (built: {have})")


def _default_version_pin() -> str | None:
    """Read the configured pin with a concise CLI error on corruption."""
    try:
        return config.get_default_version()
    except (OSError, ValueError) as exc:
        _die(f"cannot read the default version pin: {exc}")


def default_index(*, warn: bool = True) -> Path:
    """The index used when neither --db nor -K is given.

    Precedence: the version pinned with `{PROG} use`, then the highest built
    version — which is predictable, unlike file modification times.
    """
    available = config.list_indexes()
    if not available:
        _die(f"no index built yet — run '{PROG} build lts' first")
    pinned = _default_version_pin()
    if pinned:
        path = config.index_path(pinned)
        if path.is_file():
            return path
        if warn:
            terminal.Console(stream=sys.stderr).note(
                f"{PROG}: pinned version {pinned} has no index any more; "
                f"falling back to the highest built version "
                f"(fix with '{PROG} use <version>' or '{PROG} use --clear')")
    return max(available, key=_index_version_key)


def selected_index(args) -> Path:
    """The index `-K` / `--db` / `use` would open, without connecting."""
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    if getattr(args, "kernel", None):
        return resolve_index_spec(args.kernel)
    return default_index()


def index_version(meta: dict) -> str:
    """Kernel version recorded by the build.

    An index filename is only a selection alias.  In particular, a custom
    ``--output indexes/other-name.db`` must not generate links for a kernel
    version that was never indexed.
    """
    stem = meta.get("index_stem") or ""
    kver = meta.get("kernel_version") or ""
    return kver or stem or "?"


def open_index(args) -> tuple[sqlite3.Connection, dict]:
    path = selected_index(args)
    if not path.is_file():
        _die(f"no index at {path} — run '{PROG} build <version>' first")
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        meta = db.validate_schema(conn)
    except (sqlite3.DatabaseError, OSError) as exc:
        if conn is not None:
            conn.close()
        _die(f"{path} is not a usable index ({exc}) — rebuild it with "
             f"'{PROG} build <version> --force'")
    meta["index_stem"] = path.stem
    # Keep the resolved selection identity alongside the persisted metadata.
    # A filename may be a custom alias for another kernel version, so rebuild
    # hints must not derive the publication path from ``kernel_version``.
    meta["index_path"] = str(path.expanduser().resolve())
    _OPEN_INDEXES.append(conn)
    return conn, meta


_OPEN_INDEXES: list[sqlite3.Connection] = []


def _close_indexes() -> None:
    while _OPEN_INDEXES:
        try:
            _OPEN_INDEXES.pop().close()
        except sqlite3.Error:
            pass


def _linux(meta: dict) -> str:
    return f"Linux {index_version(meta)}"


def _unlink_index(path: Path) -> int:
    """Delete one regular index and its sidecars, or an index symlink leaf."""
    path = config.require_project_path(path, follow_leaf=False)

    def inspect(leaf: Path):
        try:
            return leaf.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None

    primary = inspect(path)
    if primary is None:
        raise FileNotFoundError(path)
    if stat.S_ISLNK(primary.st_mode):
        # SQLite sidecars belong beside the symlink target, not beside the
        # alias.  Removing an alias must never guess ownership of adjacent data.
        path.unlink()
        return 0
    if not stat.S_ISREG(primary.st_mode):
        raise OSError(f"refusing to remove non-regular index entry {path}")

    leaves = (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
              path.with_suffix(".db-journal"))
    observed = []
    for leaf in leaves:
        info = inspect(leaf)
        if info is None:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise OSError(f"refusing non-regular SQLite sidecar {leaf}")
        observed.append((leaf, info))

    freed = 0
    for leaf, expected in observed:
        current = inspect(leaf)
        if (current is None
                or (current.st_dev, current.st_ino, current.st_mode)
                != (expected.st_dev, expected.st_ino, expected.st_mode)):
            raise OSError(f"index entry changed while being removed: {leaf}")
        if stat.S_ISREG(current.st_mode):
            freed += current.st_size
        leaf.unlink()
    return freed


def _managed_source_record(path: Path) -> tuple[Path, dict[str, str]] | None:
    """The safely removable managed source identified by an index.

    Selection aliases need not equal the indexed kernel version, and a custom
    ``--src`` tree must never be recursively deleted by ``remove --source``.
    Only return the conventional managed path when the index recorded that
    exact path for its validated metadata version.
    """
    conn = None
    try:
        conn = db.connect(path, readonly=True)
        meta = db.validate_schema(conn)
        version = config.validate_version(meta.get("kernel_version", ""))
        recorded = meta.get("tree_path")
        if not isinstance(recorded, str) or not recorded:
            return None
        recorded_path = Path(recorded).expanduser()
        identity_keys = (
            "managed_tree_id", "managed_tree_device", "managed_tree_inode",
            "managed_tree_digest",
        )
        identity = {key: meta.get(key) for key in identity_keys}
        if any(not isinstance(value, str) or not value
               for value in identity.values()):
            # Custom --src indexes never receive this acquisition nonce, even
            # when their tree happens to use the conventional cache spelling.
            return None
        expected = config.source_path(version)
        return ((expected, identity)
                if _same_path(recorded_path, expected) else None)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _managed_source_recorded_by(path: Path) -> Path | None:
    """Compatibility path-only view of a managed source authorization."""
    record = _managed_source_record(path)
    return record[0] if record is not None else None
