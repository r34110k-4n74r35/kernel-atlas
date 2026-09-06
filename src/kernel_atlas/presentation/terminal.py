"""Shared terminal color, cell widths, and human-readable CLI presentation."""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar

_COLOR_MODE = ContextVar("kernel_atlas_color", default="auto")
TONES = {"info": "36", "success": "32", "warning": "33", "error": "31",
         "muted": "90", "accent": "35", "bold": "1"}


def paint(text: str, code: str, on: bool) -> str:
    """Apply one ANSI style without changing undecorated output."""
    return f"\033[{code}m{text}\033[0m" if on and text else text


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


def _pad(text: str, width: int, right: bool = False) -> str:
    spaces = " " * max(0, width - display_width(text))
    return spaces + text if right else text + spaces


def table_text(headers, rows, *, color: bool = False, width: int = 0,
               align_right=(), tones=None, row_tones=None, markers=None,
               cell_codes=None) -> str:
    """Align and wrap human-readable tables without dropping field contents.

    If columns cannot fit legibly, render each row as labelled fields. Width
    zero leaves redirected reports unwrapped. Machine formats bypass this.
    """
    headers = [clean(h) for h in headers]
    rows = [[clean(v) for v in row] for row in rows]
    if not headers:
        return ""
    widths = [max([display_width(h)] + [display_width(r[i]) for r in rows])
              for i, h in enumerate(headers)]
    minimum = [w if i in align_right else min(w, max(8, display_width(headers[i])))
               for i, w in enumerate(widths)]
    overhead = 2 + 2 * (len(headers) - 1)
    stacked = width and sum(minimum) + overhead > width
    if width and not stacked:
        while sum(widths) + overhead > width:
            available = [i for i, w in enumerate(widths) if w > minimum[i]]
            if not available:
                break
            widest = max(available, key=lambda i: widths[i])
            widths[widest] -= 1
    tones = tones or {}
    lines = []

    def code(row, col):
        if cell_codes is not None and cell_codes[row][col]:
            return cell_codes[row][col]
        tone = row_tones[row] if row_tones is not None else None
        return TONES.get(tone or tones.get(col), "")

    if stacked:
        for n, row in enumerate(rows):
            if n:
                lines.append("")
            for i, value in enumerate(row):
                prefix = "> " if markers and markers[n] and i == 0 else "  "
                for line in wrap_text(f"{prefix}{headers[i]}: {value}", width,
                                      subsequent_indent="    "):
                    lines.append(paint(line, code(n, i), color and bool(code(n, i))))
        if not rows:
            lines.extend(paint(line, "1;36", color)
                         for line in wrap_text("  ".join(headers), width))
    else:
        head = "  " + "  ".join(_pad(h, widths[i], i in align_right)
                                  for i, h in enumerate(headers))
        lines.append(paint(head.rstrip(), "1;36", color))
        for n, row in enumerate(rows):
            cells = [wrap_text(v, max(1, widths[i])) or [""]
                     for i, v in enumerate(row)]
            for part in range(max(map(len, cells))):
                segments = []
                last_value = max((i for i, values in enumerate(cells)
                                  if part < len(values) and values[part].strip()),
                                 default=0)
                for i, values in enumerate(cells[:last_value + 1]):
                    value = values[part] if part < len(values) else ""
                    value = _pad(value, widths[i], i in align_right)
                    if i == last_value:
                        value = value.rstrip()
                    segments.append(paint(value, code(n, i), color and bool(code(n, i))))
                marker = (paint("> ", "1;33", color)
                          if markers and markers[n] and part == 0 else "  ")
                lines.append((marker + "  ".join(segments)).rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


class Console:
    """A small presentation layer for one output stream, with no side effects."""

    def __init__(self, color: str | None = None, stream=None):
        self.stream = sys.stdout if stream is None else stream
        self.color = color_enabled(self.stream, color)
        self.width = terminal_width(self.stream) if self.stream.isatty() else 0

    def _paint(self, text, tone=None, *, bold=False):
        code = TONES.get(tone, "")
        if bold:
            code = "1;" + code if code else "1"
        return paint(text, code, self.color and bool(code))

    def blank(self):
        print(file=self.stream)

    def heading(self, text, tone="info"):
        for paragraph in str(text).split("\n"):
            lines = (wrap_text(clean(paragraph), self.width)
                     if self.width else [clean(paragraph)])
            for line in lines or [""]:
                print(self._paint(line, tone, bold=True), file=self.stream)

    def text(self, text, tone=None, indent=2, wrap=True):
        # Explicit newlines represent paragraphs; other control characters are
        # escaped for human output. Raw source, paths, and URLs bypass this.
        for paragraph in str(text).split("\n"):
            line = " " * indent + clean(paragraph)
            lines = (wrap_text(line, self.width, subsequent_indent=" " * indent)
                     if wrap and self.width else [line])
            for value in lines or [""]:
                print(self._paint(value, tone), file=self.stream)

    def field(self, label, value, tone=None):
        label, value = clean(label), clean(value)
        prefix = "  " + _pad(label, 14) + "  "
        used = display_width(prefix)
        if self.width and self.width < used + 12:
            self.text(f"{label}: {value}", tone=tone)
            return
        values = wrap_text(value, self.width - used) if self.width else [value]
        for n, line in enumerate(values or [""]):
            left = prefix if n == 0 else " " * used
            print(self._paint(left, "muted") + self._paint(line, tone), file=self.stream)

    def note(self, text, tone="warning"):
        label = {"warning": "Warning", "error": "Error", "success": "Done"}.get(tone, "Note")
        self.text(f"{label}: {text}", tone=tone)

    def table(self, headers, rows, align_right=(), tones=None, row_tones=None):
        self.stream.write(table_text(headers, rows, color=self.color, width=self.width,
                                     align_right=align_right, tones=tones,
                                     row_tones=row_tones))

    def commands(self, commands):
        commands = list(commands)
        if not commands:
            return
        self.blank()
        for n, command in enumerate(commands):
            prefix = "  Next:  " if n == 0 else "         "
            print(self._paint(prefix, "muted") + self._paint(clean(command), "info"),
                  file=self.stream)


def format_size(size: int) -> str:
    """Express byte counts in readable binary units without rounding small files to zero."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{size:,} B"
        value /= 1024
    raise AssertionError("unreachable")
