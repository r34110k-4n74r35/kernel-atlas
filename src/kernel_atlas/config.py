"""Where kernel trees and built indexes live.

Study data stays inside the source checkout. ``KERNEL_ATLAS_HOME`` can select
a subdirectory within that checkout; it cannot redirect application data to
another location. Python and third-party caches retain their normal behavior.

    kernel-atlas/
      kernels/linux-6.18.45/   <- the actual kernel source, browsable
      indexes/6.18.45.db
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

SOURCES_DIRNAME = "kernels"
INDEX_DIRNAME = "indexes"

# A version is also used as a filename.  Keep this deliberately a little more
# permissive than kernel.org's release syntax so local/vendor trees such as
# ``6.6.12-acme+debug`` remain usable, while refusing path separators, drive
# prefixes, whitespace and dot-files.
_SAFE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")


def validate_version(version: str) -> str:
    """Return a safe version identifier, or raise :class:`ValueError`.

    Versions become filenames under ``indexes/`` and ``kernels/``.  Validating
    at that boundary prevents a purported version such as ``../../notes`` or
    ``/tmp/data`` from escaping the data directory.
    """
    if not isinstance(version, str):
        raise ValueError("kernel version must be a string")
    value = version.strip()
    if value != version or not _SAFE_VERSION_RE.fullmatch(value):
        raise ValueError(
            f"unsafe kernel version {version!r}; expected a single filename-safe "
            "identifier"
        )
    return value


def project_root() -> Path | None:
    """The checkout this package was imported from, if there is one."""
    package_dir = Path(__file__).resolve().parent
    for parent in package_dir.parents:
        candidate = parent / "src" / "kernel_atlas"
        if not (parent / "pyproject.toml").is_file() or not candidate.is_dir():
            continue
        # Merely being installed in ``some-project/.venv`` must not make that
        # unrelated src-layout project our checkout.  The candidate package
        # itself has to be the one from which this module was imported.
        try:
            if candidate.resolve() == package_dir:
                return parent
        except OSError:
            continue
    return None


def require_project_path(path: Path | str, *, follow_leaf: bool = True) -> Path:
    """Check application storage without changing Python or tool cache policy.

    Resolve parent links before checking containment. Atomic replacement and
    unlink may use ``follow_leaf=False``: they change the local directory entry
    without writing to a leaf symlink's target. These checks assume directories
    are not being maliciously replaced by another process during an operation.
    """
    root = project_root()
    if root is None:
        raise ValueError(
            "application storage requires a source checkout; install this "
            "project in editable mode (python -m pip install -e .)")
    path = Path(path).expanduser()
    try:
        lexical = Path(os.path.abspath(path.parent.resolve() / path.name))
        resolved = lexical.resolve() if follow_leaf else lexical
        resolved.relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValueError(
            f"path {path} is outside the project or cannot be safely resolved; "
            f"choose a location inside {root}") from exc
    return lexical


def data_root() -> Path:
    root = project_root()
    # require_project_path fails before any write when no checkout is present.
    return require_project_path(
        os.environ.get("KERNEL_ATLAS_HOME") or root or Path.cwd())


def sources_dir() -> Path:
    return require_project_path(data_root() / SOURCES_DIRNAME)


def index_dir() -> Path:
    return require_project_path(data_root() / INDEX_DIRNAME)


def source_path(version: str) -> Path:
    return sources_dir() / f"linux-{validate_version(version)}"


def index_path(version: str) -> Path:
    return index_dir() / f"{validate_version(version)}.db"


def list_indexes() -> list[Path]:
    d = index_dir()
    if not d.is_dir():
        return []
    # Keep corrupt regular SQLite files visible for diagnosis, but do not let a
    # directory or dangling alias poison default selection.  An exact dangling
    # alias can still be named explicitly to ``remove`` it.
    return sorted(path for path in d.glob("*.db") if path.is_file())


def default_version_file() -> Path:
    return index_dir() / ".default-version"


def get_default_version() -> str | None:
    """The version pinned with `ka use`, or None if no pin file exists.

    A present pin is configuration, not a hint: malformed contents and read
    failures must remain visible to callers instead of silently changing which
    index is selected.
    """
    pin = default_version_file()
    try:
        value = pin.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise ValueError(
            f"default version pin {pin} is not valid UTF-8"
        ) from None
    if not value:
        raise ValueError(f"default version pin {pin} is empty")
    try:
        return validate_version(value)
    except ValueError:
        # Do not include the untrusted contents in the error: the pin may have
        # been replaced by a symlink to an unrelated file.  Validation still
        # prevents it from redirecting index lookup outside ``indexes/``.
        raise ValueError(
            f"default version pin {pin} does not contain a safe version"
        ) from None


def set_default_version(version: str) -> None:
    """Atomically pin ``version`` without following a hostile leaf symlink."""
    version = validate_version(version)
    f = require_project_path(default_version_file(), follow_leaf=False)
    f.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{f.name}.", suffix=".tmp", dir=f.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(version + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Replacing the directory entry, rather than opening ``f`` for writing,
        # replaces a leaf symlink itself and makes concurrent readers see either
        # the old complete pin or the new complete pin.
        temporary.replace(f)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def clear_default_version() -> None:
    require_project_path(default_version_file(), follow_leaf=False).unlink(
        missing_ok=True)


def tree_for(version: str, recorded: str | None = None) -> Path | None:
    """Find the source tree for a version.

    Prefers the current layout so an index still works after the data directory
    has been moved, and only then falls back to the path recorded at build time.
    """
    local = source_path(version)
    if (local / "MAINTAINERS").is_file():
        return local
    if recorded:
        p = Path(recorded).expanduser()
        if (p / "MAINTAINERS").is_file():
            return p
    return None
