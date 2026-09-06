"""Literal indexed-path predicates shared by browsing and documentation queries."""

from __future__ import annotations

_GLOB_ESCAPES = str.maketrans({"[": "[[]", "*": "[*]", "?": "[?]"})


def parent_path(path: str) -> str:
    """Directory containing a file path; '' for a top-level file or the root."""
    return path.rpartition("/")[0] if path else ""


def like_escape(value: str) -> str:
    """Escape SQLite LIKE metacharacters for case-insensitive name searches."""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_under(path: str) -> str:
    """Legacy LIKE prefix; use glob_under for case-sensitive indexed paths."""
    return like_escape(path) + "/%" if path else "%"


def glob_under(path: str) -> str:
    """Case-sensitive SQLite GLOB for descendants of a literal directory.

    Bracket expressions quote GLOB's metacharacters, so filenames containing
    ``[``, ``*``, or ``?`` cannot broaden the scope. Unlike LIKE, GLOB respects
    the Linux path namespace without changing connection-wide search behavior.
    """
    return path.translate(_GLOB_ESCAPES) + "/*" if path else "*"
