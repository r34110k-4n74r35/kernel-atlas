"""Small, stderr-only progress displays for long-running build phases."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import threading
import time


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
        self.label = label
        self.total = total
        self.unit = unit
        self.completed = self.initial = initial
        self.detail = detail
        self.quiet = quiet
        self.refresh = refresh
        self.stream = stream if stream is not None else sys.stderr
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
                self.detail = detail
            self._write()

    def _refresh(self):
        while not self._stop.wait(0.1):
            with self._lock:
                self._write()

    def _line(self, now: float, status: str | None = None) -> str:
        elapsed = max(0.0, now - self.started)
        marker = status or ("|/-\\"[int(elapsed * 8) % 4] if self.tty else "started")
        parts = [f"{marker} {self.label}"]
        if self.total is not None and self.total > 0:
            ratio = min(1.0, max(0.0, self.completed / self.total))
            filled = int(ratio * 18)
            parts.append(f"[{'#' * filled}{'-' * (18 - filled)}] {int(ratio * 100):3d}%")
        if self.unit == "bytes":
            count = _bytes(self.completed)
            if self.total is not None:
                count += f"/{_bytes(self.total)}"
            parts.append(count)
        elif self.unit:
            count = f"{self.completed:,}"
            if self.total is not None:
                count += f"/{self.total:,}"
            parts.append(f"{count} {self.unit}")
        parts.append(f"elapsed {_duration(elapsed)}")
        delta = self.completed - self.initial
        if elapsed > 0 and delta > 0 and self.unit:
            rate = delta / elapsed
            parts.append(f"{_bytes(rate)}/s" if self.unit == "bytes"
                         else f"{rate:,.0f} {self.unit}/s")
            if status is None and self.total is not None:
                parts.append(f"ETA {_duration(max(0, self.total - self.completed) / rate)}")
        if self.detail:
            parts.append(" ".join(self.detail.split()))
        # Labels/details may contain a source filename or user-supplied version.
        return "  " + "".join(c if c.isprintable() else "?"
                              for c in " | ".join(parts))

    def _write(self, *, force: bool = False, status: str | None = None):
        now = time.monotonic()
        interval = 0.1 if self.tty else 30.0
        if not force and now - self._last_write < interval:
            return
        self._last_write = now
        line = self._line(now, status)
        if self.tty:
            try:
                # stdout may be redirected while stderr still has a terminal.
                size = os.get_terminal_size(self.stream.fileno())
            except (OSError, ValueError, AttributeError):
                size = os.terminal_size((0, 0))
            if size.columns <= 0 or size.lines <= 0:
                size = shutil.get_terminal_size(fallback=(120, 24))
            width = max(1, size.columns - 1)
            lines = textwrap.wrap(line, width, subsequent_indent="  " if width > 2 else "",
                                  break_on_hyphens=False)[:max(1, size.lines - 1)]
            # Wrap detailed counters instead of hiding rate/ETA on an 80-column
            # terminal. Clear every old row when a shorter update replaces it.
            lines += [""] * max(0, self._rows - len(lines))
            if self._rows > 1:
                self.stream.write(f"\r\x1b[{self._rows - 1}A")
            else:
                self.stream.write("\r")
            self.stream.write("\n\r".join(part + "\x1b[K" for part in lines))
            if status:
                self.stream.write("\n")
            self._rows = len(lines)
        else:
            self.stream.write(line + "\n")
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
