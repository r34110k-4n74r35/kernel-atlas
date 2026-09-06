"""Terminal presentation for builds, independent of indexing and storage."""

from __future__ import annotations

import sys
from pathlib import Path

from .render_format import paint
from .terminal import (
    TONES as _TONES,
    clean as clean,
    color_enabled as color_enabled,
    color_mode as color_mode,
    display_width as display_width,
    format_size as _size,
    terminal_width as terminal_width,
    wrap_text as wrap_text,
)


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
