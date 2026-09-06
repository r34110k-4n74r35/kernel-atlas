"""Small, stderr-only progress displays for long-running build phases."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

from .build_output import clean, color_enabled, display_width, wrap_text
from .render_format import paint


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours
            else f"{minutes:02d}:{seconds:02d}")


def _bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


class Progress:
    """One phase with measured counts, or a spinner when work is unbounded.

    TTY displays refresh in place. Redirected output has a start/end record
    and, for counted work, an update at most every 30 seconds. No output or
    background thread is created in quiet mode. Disable ``refresh`` around
    process-pool creation: fork-based Python runtimes must not inherit a live
    renderer thread or its lock.
    """

    def __init__(self, label: str, *, total: int | None = None,
                 unit: str | None = None, initial: int = 0,
                 detail: str = "", quiet: bool = False,
                 refresh: bool = True, stream=None):
        self.label = clean(label)
        self.total = total
        self.unit = clean(unit) if unit else None
        self.completed = self.initial = initial
        self.detail = clean(detail)
        self.quiet = quiet
        self.refresh = refresh
        self.stream = stream if stream is not None else sys.stderr
        # The refresh thread does not inherit ContextVars from the CLI thread.
        # Freeze its policy here so the first render and later updates agree.
        self.color = color_enabled(self.stream)
        self.tty = (not quiet and self.stream.isatty()
                    and os.environ.get("TERM") != "dumb")
        self.started = 0.0
        self._last_write = 0.0
        self._rows = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if not self.quiet:
            self.started = time.monotonic()
            self._write(force=True)
            if self.tty and self.refresh:
                self._thread = threading.Thread(
                    target=self._refresh, name="kernel-atlas-progress", daemon=True)
                self._thread.start()
        return self

    def update(self, completed: int | None = None, *, detail: str | None = None,
               total: int | None = None):
        if self.quiet:
            return
        with self._lock:
            if completed is not None:
                self.completed = completed
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = clean(detail)
            self._write()

    def _refresh(self):
        while not self._stop.wait(0.1):
            with self._lock:
                self._write()

    def _parts(self, now: float, status: str | None = None):
        elapsed = max(0.0, now - self.started)
        spinner = "|/-\\"[int(elapsed * 8) % 4]
        marker = status or (f"{spinner} running" if self.tty else "started")
        header = f"  {marker} {self.label}"
        count = ""
        if self.unit == "bytes":
            count = _bytes(self.completed)
            if self.total is not None:
                count += f"/{_bytes(self.total)}"
        elif self.unit:
            count = f"{self.completed:,}"
            if self.total is not None:
                count += f"/{self.total:,}"
            count += f" {self.unit}"
        elif self.total is not None:
            count = f"{self.completed:,}/{self.total:,}"
        metrics = [f"elapsed {_duration(elapsed)}"]
        delta = self.completed - self.initial
        if elapsed > 0 and delta > 0 and self.unit:
            rate = delta / elapsed
            metrics.append(f"{_bytes(rate)}/s" if self.unit == "bytes"
                           else f"{rate:,.0f} {self.unit}/s")
            if status is None and self.total is not None:
                metrics.append(f"ETA {_duration(max(0, self.total - self.completed) / rate)}")
        code = {"done": "1;32", "failed": "1;31", "interrupted": "1;33"}.get(
            status, "1;36")
        return header, count, metrics, code

    def _summary_count(self, count: str) -> str:
        if self.total is not None and self.total > 0:
            ratio = min(1.0, max(0.0, self.completed / self.total))
            return f"{count} ({int(ratio * 100)}%)"
        return count

    def _terminal_lines(self, now: float, status: str | None, width: int):
        header, count, metrics, code = self._parts(now, status)
        rows = [(header, code)]
        if status:
            count = self._summary_count(count)
            rows.append(("    " + "   ".join(([count] if count else []) + metrics), ""))
        else:
            if self.total is not None and self.total > 0:
                ratio = min(1.0, max(0.0, self.completed / self.total))
                # Keep the measured count visible on narrow terminals too.
                bar_width = max(4, min(18, width - display_width(count) - 15))
                filled = int(ratio * bar_width)
                bar = f"[{'#' * filled}{'-' * (bar_width - filled)}] " if width >= 24 else ""
                count = f"{bar}{int(ratio * 100):3d}%" + (f"  {count}" if count else "")
            if count:
                rows.append((f"    {count}", "36"))
            rows.append(("    " + "   ".join(metrics), ""))
        if self.detail:
            rows.append((f"    {self.detail}", "2"))
        # Wrap plain content first: ANSI color sequences have no display width.
        return [paint(part, style, self.color and bool(style))
                for content, style in rows
                for part in wrap_text(content, width,
                                      subsequent_indent="    " if width > 4 else "")]

    def _log_line(self, now: float, status: str | None) -> str:
        header, count, metrics, code = self._parts(now, status)
        count = self._summary_count(count)
        fields = ([count] if count else []) + metrics
        if self.detail:
            fields.append(self.detail)
        return paint(header, code, self.color) + ": " + "; ".join(fields)

    def _write(self, *, force: bool = False, status: str | None = None):
        now = time.monotonic()
        interval = 0.1 if self.tty else 30.0
        if not force and now - self._last_write < interval:
            return
        self._last_write = now
        if self.tty:
            try:
                # stdout may be redirected while stderr still has a terminal.
                size = os.get_terminal_size(self.stream.fileno())
            except (OSError, ValueError, AttributeError):
                size = os.terminal_size((0, 0))
            if size.columns <= 0 or size.lines <= 0:
                size = shutil.get_terminal_size(fallback=(120, 24))
            width = max(1, size.columns - 1)
            lines = self._terminal_lines(now, status, width)[:max(1, size.lines - 1)]
            rendered_rows = len(lines)
            # Erase every previous row when a compact completion replaces the
            # live bar; return to the last new row so phases stay together.
            lines += [""] * max(0, self._rows - len(lines))
            if self._rows > 1:
                self.stream.write(f"\r\x1b[{self._rows - 1}A")
            else:
                self.stream.write("\r")
            self.stream.write("\n\r".join(part + "\x1b[K" for part in lines))
            if len(lines) > rendered_rows:
                self.stream.write(f"\r\x1b[{len(lines) - rendered_rows}A")
            if status:
                self.stream.write("\n")
            self._rows = rendered_rows
        else:
            self.stream.write(self._log_line(now, status) + "\n")
        self.stream.flush()

    def __exit__(self, exc_type, exc, traceback):
        if self.quiet:
            return False
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        status = ("done" if exc_type is None else
                  "interrupted" if issubclass(exc_type, KeyboardInterrupt) else "failed")
        with self._lock:
            self._write(force=True, status=status)
        return False
