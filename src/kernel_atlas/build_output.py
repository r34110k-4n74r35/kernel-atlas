"""Terminal presentation for builds, independent of indexing and storage."""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .render_format import paint

_COLOR_MODE = ContextVar("kernel_atlas_build_color", default="auto")
_TONES = {"info": "36", "success": "32", "warning": "33", "error": "31"}


@contextmanager
def color_mode(choice: str):
    """Scope a CLI color preference without changing subsequent library calls."""
    token = _COLOR_MODE.set(choice)
    try:
        yield
    finally:
        _COLOR_MODE.reset(token)


def color_enabled(stream, choice: str | None = None) -> bool:
    choice = _COLOR_MODE.get() if choice is None else choice
    if choice == "never":
        return False
    if choice == "always":
        return True
    return (stream.isatty() and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb")


def clean(value) -> str:
    """Keep paths and other user-controlled text from injecting terminal codes."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def _character_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    """Count terminal cells in plain text, including wide and combining glyphs."""
    return sum(_character_width(char) for char in text)


def wrap_text(text: str, width: int, *, subsequent_indent: str = "") -> list[str]:
    """Wrap plain text by display cells, preserving words and internal spaces.

    Long words split only when necessary. Indentation gives way on very narrow
    terminals; a two-cell glyph becomes ``?`` only when the entire row is one
    cell wide. Callers apply ANSI styles after wrapping.
    """
    width = max(1, width)
    lines = []
    line = ""
    used = 0
    spaces = ""

    def flush():
        nonlocal line, used
        if line.strip():
            lines.append(line.rstrip())
        line = subsequent_indent
        used = display_width(line)

    for token in re.findall(r" +|[^ ]+", text):
        if token.startswith(" "):
            spaces = token
            continue
        if line.strip() and used + len(spaces) + display_width(token) > width:
            flush()
            spaces = ""
        if spaces and (line.strip() or not lines):
            line += spaces
            used += len(spaces)
        spaces = ""
        for char in token:
            cells = _character_width(char)
            if cells > width:
                char, cells = "?", 1
            if used + cells > width:
                if line.strip():
                    flush()
                # Leave room for the next glyph, even if leading whitespace
                # contains wide spaces rather than ordinary indentation.
                if used + cells > width:
                    trimmed = []
                    used = 0
                    for space in line:
                        space_width = _character_width(space)
                        if used + space_width > width - cells:
                            break
                        trimmed.append(space)
                        used += space_width
                    line = "".join(trimmed)
            line += char
            used += cells
    if line.strip():
        lines.append(line.rstrip())
    return lines


def terminal_width(stream) -> int:
    if not stream.isatty():
        return 100
    try:
        columns = os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        columns = 0
    if columns <= 0:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    return max(1, columns - 1)


def _field(label, value, *, stream, tone: str | None = None,
           emphasize: bool = False):
    label, value = clean(label), clean(value)
    color = color_enabled(stream)
    prefix = f"  {label}{' ' * max(0, 14 - display_width(label))}  "
    prefix_width = display_width(prefix)
    width = terminal_width(stream)
    if stream.isatty() and width < prefix_width + 12:
        for line in wrap_text(f"{label}: {value}", width):
            print(paint(line, _TONES.get(tone, ""), color and bool(tone)), file=stream)
        return
    values = (wrap_text(value, max(1, width - prefix_width))
              if stream.isatty() else [value]) or [""]
    for n, line in enumerate(values):
        left = prefix if n == 0 else " " * prefix_width
        left = paint(left, _TONES.get(tone, ""), color and bool(tone))
        print(left + paint(line, "1", color and emphasize), file=stream)


def note(label, text, *, tone: str = "info", quiet: bool = False, stream=None):
    if not quiet:
        _field(label, text, stream=stream if stream is not None else sys.stderr,
               tone=tone)


def _heading(text, *, stream, tone: str = "info"):
    # Wrap before styling so ANSI sequences never count toward terminal width.
    lines = (wrap_text(clean(text), terminal_width(stream))
             if stream.isatty() else [clean(text)])
    for line in lines:
        print(paint(line, "1;" + _TONES[tone], color_enabled(stream)), file=stream)


def _display_path(path) -> str:
    """Shorten local paths for scanning; suggested commands retain full paths."""
    path = Path(path)
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def header(version: str, source, output, *, calls: bool, workers: int | None,
           quiet: bool = False):
    if quiet:
        return
    stream = sys.stderr
    _heading(f"Kernel Atlas | Linux {version}", stream=stream)
    for label, value in (("Source", _display_path(source)), ("Index", _display_path(output)),
                         ("Call graph", "enabled" if calls else "disabled"),
                         ("Workers", workers if workers is not None else "automatic")):
        _field(label, value, stream=stream)
    print(file=stream)


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{size:,} B"
        value /= 1024
    raise AssertionError("unreachable")


def _elapsed(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def summary(version: str, output, stats, *, size: int, calls: bool, query_cmd: str):
    stream = sys.stdout
    print(file=stream)
    _heading(f"Built index for Linux {version}", stream=stream, tone="success")
    for label, value in (("Index", _display_path(output)), ("Size", _size(size)),
                         ("Build time", _elapsed(stats.seconds))):
        _field(label, value, stream=stream, emphasize=True)

    print(file=stream)
    _heading("Contents", stream=stream)
    for label, value in (("Directories", stats.dirs), ("Files", stats.files),
                         ("Parsed C/H", stats.parsed), ("Symbols", stats.symbols),
                         ("Subsystems", stats.subsystems)):
        _field(label, f"{value:,}", stream=stream, emphasize=True)
    if stats.symlinks:
        _field("Symlinks", f"{stats.symlinks:,} recorded", stream=stream)

    if calls:
        print(file=stream)
        _heading("Call graph", stream=stream)
        for label, value in (("Records", stats.calls),
                             ("Occurrences", stats.call_occurrences),
                             ("Resolved", stats.calls_resolved),
                             ("Ambiguous", stats.calls_ambiguous),
                             ("Macro", stats.calls_macro),
                             ("Indirect", stats.calls_indirect),
                             ("Unresolved", stats.calls_unresolved)):
            _field(label, f"{value:,}", stream=stream, emphasize=True)

    if stats.skipped or stats.failed:
        print(file=stream)
        _heading("Parsing notes", stream=stream, tone="warning")
        _field("Skipped", f"{stats.skipped:,} inputs ({stats.oversize:,} oversized)",
               stream=stream, tone="warning")
        _field("Failed", f"{stats.failed:,} inputs", stream=stream,
               tone="error" if stats.failed else None)

    print(file=stream)
    _heading("Try next", stream=stream)
    # Keep commands intact for copying, including long shell-quoted paths.
    for suffix in ("info mm", "siblings mm/page_alloc.c"):
        print("  " + paint(clean(f"{query_cmd} {suffix}"), "36", color_enabled(stream)),
              file=stream)
