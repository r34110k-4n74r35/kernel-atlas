"""Minimal source trees for index-construction tests."""

from pathlib import Path


def _tree(root: Path, maintainers: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Makefile").write_text(
        "VERSION = 9\nPATCHLEVEL = 9\nSUBLEVEL = 0\nEXTRAVERSION =\n"
    )
    (root / "MAINTAINERS").write_text(
        maintainers or "TEST\nM: A <a@example.com>\nF: *\n",
        encoding="utf-8",
    )
    return root
