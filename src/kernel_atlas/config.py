"""Cache locations for downloaded kernel trees and built indexes."""

from __future__ import annotations

import os
from pathlib import Path


def cache_root() -> Path:
    env = os.environ.get("KERNEL_ATLAS_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "kernel-atlas"


def sources_dir() -> Path:
    return cache_root() / "src"


def index_dir() -> Path:
    return cache_root() / "index"


def source_path(version: str) -> Path:
    return sources_dir() / f"linux-{version}"


def index_path(version: str) -> Path:
    return index_dir() / f"{version}.db"


def list_indexes() -> list[Path]:
    d = index_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.db"))
