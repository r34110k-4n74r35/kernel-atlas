"""Where kernel trees and built indexes live.

Everything is kept inside the project directory rather than a hidden cache, so
the kernel source you are studying sits right next to the tool and can be opened
in an editor or grepped directly:

    kernel-atlas/
      kernels/linux-6.18.45/   <- the actual kernel source, browsable
      indexes/6.18.45.db
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCES_DIRNAME = "kernels"
INDEX_DIRNAME = "indexes"


def project_root() -> Path | None:
    """The checkout this package was imported from, if there is one."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    return None


def data_root() -> Path:
    if env := os.environ.get("KERNEL_ATLAS_HOME"):
        return Path(env).expanduser()
    root = project_root()
    if root is not None:
        return root
    # Installed as a plain package with no checkout to sit beside.
    return Path.home() / ".kernel-atlas"


def sources_dir() -> Path:
    return data_root() / SOURCES_DIRNAME


def index_dir() -> Path:
    return data_root() / INDEX_DIRNAME


def source_path(version: str) -> Path:
    return sources_dir() / f"linux-{version}"


def index_path(version: str) -> Path:
    return index_dir() / f"{version}.db"


def list_indexes() -> list[Path]:
    d = index_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.db"))


def default_version_file() -> Path:
    return index_dir() / ".default-version"


def get_default_version() -> str | None:
    """The version pinned with `ka use`, or None if nothing is pinned."""
    try:
        value = default_version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def set_default_version(version: str) -> None:
    f = default_version_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(version + "\n", encoding="utf-8")


def clear_default_version() -> None:
    default_version_file().unlink(missing_ok=True)


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
