"""CLI diagnostics, listing formats, columns, and symbol filters."""

from __future__ import annotations

import re
import sys

from ..queries import query
from ..presentation import render
from ..presentation import terminal
from ..queries.models import Entry


PROG = "kernel-atlas"


def _die(msg: str, code: int = 1):
    terminal.Console(stream=sys.stderr).text(f"{PROG}: {msg}", tone="error", indent=0)
    raise SystemExit(code)


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,\s]+", value) if p.strip()]


def pick_columns(args, kinds_listed: set[str], with_subsystem: bool) -> list[str]:
    if getattr(args, "columns", None) is not None:
        cols = _split_list(args.columns)
        if not cols:
            _die("--columns must name at least one column")
        bad = [c for c in cols if c not in render.COLUMNS]
        if bad:
            _die(f"unknown column(s): {', '.join(bad)}"
                 f" — valid: {', '.join(render.COLUMNS)}")
        if with_subsystem and "subsystem" not in cols:
            cols.append("subsystem")
        return cols
    only_dirs = kinds_listed <= {"dir"}
    only_files = kinds_listed <= {"file"}
    only_syms = kinds_listed and not (kinds_listed & {"dir", "file"})
    if only_dirs:
        cols = ["kind", "name", "subdirs", "files"]
    elif only_files:
        cols = ["kind", "name", "lines", "size", "symbols"]
    elif only_syms:
        cols = ["kind", "name", "line", "span", "flags", "signature"]
    else:
        cols = ["kind", "name", "path", "line"]
    if with_subsystem and "subsystem" not in cols:
        cols.insert(2 if len(cols) > 2 else len(cols), "subsystem")
    return cols


_COLUMN_OUTPUT_FORMATS = {"table", "json", "csv"}


def _validate_listing_output(args) -> None:
    """Reject column controls when the selected format has a fixed shape."""
    if args.format in _COLUMN_OUTPUT_FORMATS:
        return
    if getattr(args, "columns", None) is not None:
        _die(f"--columns does not apply to --format {args.format}")
    if getattr(args, "with_subsystem", False):
        _die(f"--with-subsystem does not apply to --format {args.format}")


def _listing_has_columns(args) -> bool:
    """Whether the selected listing renderer can expose chosen columns."""
    return args.format in _COLUMN_OUTPUT_FORMATS


def emit(entries: list[Entry], args, kinds_listed: set[str], with_subsystem: bool,
         header: str = "", index: str | None = None,
         default_columns: tuple[str, ...] | None = None):
    _validate_listing_output(args)
    fmt = args.format
    machine = fmt in ("json", "csv", "names", "plain")
    color = render.use_color(args.color)
    explicit_columns = getattr(args, "columns", None) is not None
    cols = pick_columns(args, kinds_listed, with_subsystem)
    if not explicit_columns and default_columns:
        cols = list(default_columns)
    if not machine and header:
        terminal.Console(args.color).heading(header)
    if fmt == "json":
        rows = [render.entry_dict(e, cols if explicit_columns else None)
                for e in entries]
        if index:
            for row in rows:
                row["index"] = index
        sys.stdout.write(render.render_json(rows))
        return
    text = render.render(entries, cols, fmt, color, render.term_width())
    sys.stdout.write(text)
    if not machine:
        n = len(entries)
        console = terminal.Console(args.color)
        console.blank()
        console.text(f"{n} result{'s' if n != 1 else ''}", tone="muted", indent=0)


def _entry_is_target(e: Entry, t: query.Target) -> bool:
    if e.ref_id is not None:
        if t.kind == "symbol":
            return e.kind == t.symbol_kind and e.ref_id == t.id
        return e.kind == t.kind and e.ref_id == t.id
    return (e.path == t.path and e.name == t.name
            and (t.kind != "symbol"
                 or (e.kind == t.symbol_kind and e.line == t.line)))


def kinds_from_args(args, target) -> tuple[str, ...]:
    raw = _split_list(getattr(args, "kinds", None))
    if not raw:
        return query.default_kinds(target) if target else ("dir", "file")
    out: list[str] = []
    for k in raw:
        k = k.lower()
        if k == "all":
            return query.ALL_KINDS
        if k in ("symbol", "symbols"):
            out.extend(query.SYMBOL_KINDS)
        elif k in ("path", "paths"):
            out.extend(query.PATH_KINDS)
        elif k in ("func", "fn", "functions"):
            out.extend(("function", "syscall"))
        elif k in ("type", "types"):
            out.extend(("struct", "union", "enum", "typedef"))
        elif k in query.ALL_KINDS:
            out.append(k)
        elif k.endswith("s") and k[:-1] in query.ALL_KINDS:
            out.append(k[:-1])
        else:
            _die(f"unknown kind {k!r} (valid: {', '.join(query.ALL_KINDS)}, "
                 f"or all/symbols/paths/functions/types)")
    return tuple(dict.fromkeys(out))


def symbol_filter_kinds(args, kinds: tuple[str, ...]) -> tuple[str, ...]:
    """Make linkage filters explicit instead of silently ignoring path rows."""
    enabled = []
    if getattr(args, "exported", False):
        enabled.append("--exported")
    if getattr(args, "static_only", False):
        enabled.append("--static-only")
    if getattr(args, "no_static", False):
        enabled.append("--no-static")
    if not enabled:
        return kinds
    symbols = tuple(k for k in kinds if k in query.SYMBOL_KINDS)
    if not symbols:
        _die(f"{'/'.join(enabled)} only applies to symbols; choose a symbol kind")
    return symbols


def _static_mode(args) -> str:
    if getattr(args, "static_only", False):
        return "only"
    if getattr(args, "no_static", False):
        return "exclude"
    return "any"


def _checked_grep(pattern: str | None) -> str | None:
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            _die(f"--grep {pattern!r} is not a valid regex: {exc}")
    return pattern


def _post_filter(entries, args):
    """Apply --grep and the static filters to already-fetched entries."""
    pattern = _checked_grep(getattr(args, "grep", None))
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        entries = [e for e in entries if rx.search(e.name)]
    if getattr(args, "static_only", False):
        entries = [e for e in entries if e.is_static is True]
    elif getattr(args, "no_static", False):
        entries = [e for e in entries if e.is_static is not True]
    if getattr(args, "exported", False):
        entries = [e for e in entries if e.is_exported]
    if _split_list(getattr(args, "kinds", None)):
        allowed = set(k for k in kinds_from_args(args, None)
                      if k in query.SYMBOL_KINDS)
        entries = [e for e in entries if e.kind in allowed]
    return entries


def _reject_symbol_size_sort(args, kinds) -> None:
    if (args.sort == "size" and kinds
            and all(kind in query.SYMBOL_KINDS for kind in kinds)):
        _die("--sort size does not apply to symbols; use --sort lines for "
             "definition span")
