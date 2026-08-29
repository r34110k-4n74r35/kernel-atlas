"""Output formatting: table / plain / json / csv / tree, with chosen columns."""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections import Counter

from .query import Entry
from .render_format import paint
from .structure_render import render_structure as _render_structure

COLUMNS = ("kind", "name", "path", "dir", "line", "span", "lines", "size",
           "symbols", "subdirs", "files", "flags", "subsystem", "signature",
           "resolution")

DEFAULT_COLUMNS = {
    "dir": ("kind", "name", "subdirs", "files"),
    "file": ("kind", "name", "lines", "size", "symbols"),
    "symbol": ("kind", "name", "line", "span", "flags", "signature"),
}

_KIND_COLOR = {
    "dir": "34", "file": "36", "function": "32", "syscall": "35",
    "struct": "33", "union": "33", "enum": "33", "typedef": "33",
    "macro": "31", "variable": "37", "prototype": "90",
}


def use_color(choice: str = "auto") -> bool:
    if choice == "never":
        return False
    if choice == "always":
        return True
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def human_size(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n}B"
    for unit in ("K", "M", "G"):
        n /= 1024
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}"
    return str(n)


def _flags(e: Entry) -> str:
    out = []
    if e.is_exported:
        out.append("EXPORT")
    if e.is_static:
        out.append("static")
    if e.is_inline:
        out.append("inline")
    return " ".join(out) if out else "-"


def cell(e: Entry, col: str) -> str:
    if col == "kind":
        return e.kind
    if col == "name":
        return e.name + ("/" if e.kind == "dir" else "")
    if col == "path":
        return e.path
    if col == "dir":
        return e.path.rsplit("/", 1)[0] if "/" in e.path else "."
    if col == "line":
        return str(e.line) if e.line else "-"
    if col == "span":
        s = e.span
        return f"{s}L" if s else "-"
    if col == "lines":
        if e.kind == "dir":
            return "-"
        return str(e.lines) if e.lines else ("-" if e.lines is None else "0")
    if col == "size":
        return human_size(e.size)
    if col == "symbols":
        if e.kind == "dir":
            return f"{e.n_subdirs or 0}d {e.n_files or 0}f"
        return str(e.n_symbols) if e.n_symbols is not None else "-"
    if col == "subdirs":
        return str(e.n_subdirs) if e.n_subdirs is not None else "-"
    if col == "files":
        return str(e.n_files) if e.n_files is not None else "-"
    if col == "flags":
        return _flags(e)
    if col == "subsystem":
        return e.subsystem or "-"
    if col == "signature":
        return e.signature or "-"
    if col == "resolution":
        return e.resolution or "-"
    return ""


_NUMERIC = {"line", "span", "lines", "size", "subdirs", "files", "symbols"}


def render_table(entries: list[Entry], columns, color: bool,
                 max_width: int = 0) -> str:
    columns = [c for c in columns]
    if not entries:
        return ""
    rows = [[cell(e, c) for c in columns] for e in entries]
    headers = [c.upper() for c in columns]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))

    # Let the last column soak up remaining terminal width instead of wrapping.
    if max_width:
        fixed = sum(widths[:-1]) + 2 * (len(columns) - 1)
        widths[-1] = max(12, min(widths[-1], max_width - fixed))

    out = io.StringIO()
    head = "  ".join(
        h.ljust(widths[i]) if columns[i] not in _NUMERIC else h.rjust(widths[i])
        for i, h in enumerate(headers))
    out.write(paint("  " + head.rstrip(), "1;90", color) + "\n")

    for e, row in zip(entries, rows):
        cells = []
        for i, v in enumerate(row):
            if len(v) > widths[i]:
                v = v[:widths[i] - 1] + "…"
            v = v.rjust(widths[i]) if columns[i] in _NUMERIC else v.ljust(widths[i])
            if columns[i] in ("kind", "name"):
                v = paint(v, _KIND_COLOR.get(e.kind, "0"), color)
            cells.append(v)
        line = "  ".join(cells).rstrip()
        line = (paint("> ", "1;33", color) if e.is_target else "  ") + line
        out.write(line + "\n")
    return out.getvalue()


def render_plain(entries: list[Entry]) -> str:
    out = []
    for e in entries:
        if e.kind in ("dir", "file"):
            out.append(e.path + ("/" if e.kind == "dir" else ""))
        elif e.line is not None:
            out.append(f"{e.path}:{e.line}:{e.name}")
        else:
            out.append(f"{e.path}:{e.name}")
    return "\n".join(out) + ("\n" if out else "")


def render_names(entries: list[Entry]) -> str:
    return "\n".join(e.name for e in entries) + ("\n" if entries else "")


def entry_dict(e: Entry, columns=None) -> dict:
    d = {
        "kind": e.kind, "name": e.name, "path": e.path, "line": e.line,
        "end_line": e.end_line, "span": e.span, "signature": e.signature,
        "size": e.size, "lines": e.lines, "n_files": e.n_files,
        "n_subdirs": e.n_subdirs, "n_symbols": e.n_symbols,
        "is_static": e.is_static, "is_inline": e.is_inline,
        "is_exported": e.is_exported, "subsystem": e.subsystem,
        "resolution": e.resolution,
    }
    if e.is_target:
        d["is_target"] = True
    if columns is None:
        return {k: v for k, v in d.items() if v is not None}

    selected = {}
    for col in columns:
        if col == "dir":
            value = e.path.rsplit("/", 1)[0] if "/" in e.path else "."
        elif col == "span":
            value = e.span
        elif col == "symbols":
            value = e.n_symbols
        elif col == "subdirs":
            value = e.n_subdirs
        elif col == "files":
            value = e.n_files
        elif col == "flags":
            value = _flags(e)
        else:
            value = d.get(col)
        # Explicit columns are a schema request.  Keep a requested key even
        # when this particular row has no value for it so heterogeneous JSON
        # listings remain straightforward to consume.
        selected[col] = value
    if e.is_target:
        selected["is_target"] = True
    return selected


def render_json(payload) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_structure(detail: dict, color: bool, max_width: int = 0) -> str:
    """Compatibility facade for detailed aggregate study reports."""
    return _render_structure(detail, color, max_width, paint_fn=paint)


def render_csv(entries: list[Entry], columns) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for e in entries:
        w.writerow([cell(e, c) for c in columns])
    return buf.getvalue()


def render_tree(entries: list[Entry], color: bool) -> str:
    """Nested view of the paths present in `entries`.

    Symbols hang beneath their file as leaves in a list.  A list is important:
    named aggregate typedefs commonly produce two symbols with the same name
    and source line, and neither may overwrite the other.
    """
    root: dict = {}
    for e in entries:
        node = root
        parts = [p for p in e.path.split("/") if p]
        if not parts:
            continue
        if e.kind in ("dir", "file"):
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault(parts[-1], {})["__entry__"] = e
        else:
            for part in parts:
                node = node.setdefault(part, {})
            node.setdefault("__leaves__", []).append(e)

    out = io.StringIO()

    def walk(node: dict, prefix: str) -> None:
        # Dict insertion order reflects the already-sorted input.  Re-sorting
        # here used to silently undo ``--sort line`` and ``--sort lines``.
        children = [("node", key, child) for key, child in node.items()
                    if key not in ("__entry__", "__leaves__")]
        leaves = node.get("__leaves__", [])
        counts = Counter((e.name, e.line) for e in leaves)
        leaf_seen: Counter = Counter()
        items = children + [("leaf", "", e) for e in leaves]
        for i, (item_kind, key, value) in enumerate(items):
            last = i == len(items) - 1
            if item_kind == "leaf":
                e = value
                label = f"{e.name}:{e.line}" if e.line else e.name
                if counts[(e.name, e.line)] > 1:
                    label += f" [{e.kind}]"
                    leaf_seen[(e.name, e.line, e.kind)] += 1
                    if leaf_seen[(e.name, e.line, e.kind)] > 1:
                        label += f" #{leaf_seen[(e.name, e.line, e.kind)]}"
                label = paint(label, _KIND_COLOR.get(e.kind, "0"), color)
                out.write(f"{prefix}{'└── ' if last else '├── '}{label}\n")
                continue

            child = value
            e = child.get("__entry__")
            label = key + ("/" if e is not None and e.kind == "dir" else "")
            if e is not None:
                label = paint(label, _KIND_COLOR.get(e.kind, "0"), color)
            out.write(f"{prefix}{'└── ' if last else '├── '}{label}\n")
            walk(child, prefix + ("    " if last else "│   "))

    walk(root, "")
    return out.getvalue()


def render(entries: list[Entry], columns, fmt: str, color: bool,
           max_width: int = 0) -> str:
    if fmt == "json":
        return render_json([entry_dict(e, columns) for e in entries])
    if fmt == "plain":
        return render_plain(entries)
    if fmt == "names":
        return render_names(entries)
    if fmt == "csv":
        return render_csv(entries, columns)
    if fmt == "tree":
        return render_tree(entries, color)
    return render_table(entries, columns, color, max_width)


def term_width(default: int = 120) -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default
