"""CLI handlers for backtraces, call graphs, and subsystem relationships."""

from __future__ import annotations

import csv
import re
import sys

from .. import query, render
from ..queries import relationships
from ..presentation.terminal import Console


_FRAME_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_0-9]+)*\s*\+\s*0x")


_CLONE_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_0-9]+)+")


_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _frames_from_text(text: str) -> list[str]:
    """Pull symbol names out of an oops / ftrace / gdb style backtrace."""
    frames: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Kernel oops section delimiters are metadata, not frames.  Once bare
        # uppercase identifiers are accepted, <TASK>/</TASK> must be excluded
        # explicitly rather than accidentally becoming symbol names.
        if re.fullmatch(r"</?[A-Za-z_][A-Za-z0-9_]*>", line):
            continue
        m = _FRAME_RE.findall(line)
        if m:
            frames.extend(m)
            continue
        clone = _CLONE_RE.fullmatch(line)
        if clone:
            frames.append(clone.group(1))
            continue
        # 'tcp_sendmsg' or '#3  0x... in tcp_sendmsg (...)' or a bare name per line
        if " in " in line:
            tail = line.split(" in ", 1)[1]
            cand = _IDENT_RE.findall(tail)
            if cand:
                frames.append(cand[0])
                continue
        cand = _IDENT_RE.findall(line)
        if len(cand) == 1:
            frames.append(cand[0])
    return frames


def cmd_trace(args, support):
    conn, meta = support.open_index(args)
    if args.frames:
        text = "\n".join(args.frames)
    else:
        if sys.stdin.isatty():
            support._die(
                "paste a backtrace on stdin, or pass frame names as arguments")
        text = sys.stdin.read()
    frames = support._frames_from_text(text)
    if not frames:
        support._die("could not find any symbol names in that input")

    results = []
    version = support.index_version(meta)
    picked = frames if args.limit == 0 else frames[:args.limit]
    for name in picked:
        resolution = query.resolve_symbol(conn, name)
        target = resolution.target
        if (target is None or target.kind != "symbol"
                or target.symbol_kind not in ("function", "syscall")):
            results.append({"frame": name, "found": False, "index": version})
            continue
        subsystem = query.subsystem_for_target(conn, target)
        area = query.describe_area(target.path)
        subsystem_name = subsystem["name"] if subsystem else None
        specific = (subsystem_name
                    if subsystem_name not in query.CATCH_ALL else None)
        results.append({
            "frame": name,
            "found": True,
            "symbol_kind": target.symbol_kind,
            "path": target.path,
            "line": target.line,
            "subsystem": subsystem_name,
            "status": subsystem["status"] if subsystem else None,
            "area": area[0] if area else None,
            "label": specific or (area[0] if area else subsystem_name) or "?",
            "ambiguous": sum(
                candidate.symbol_kind in ("function", "syscall")
                for candidate in resolution.candidates),
            "index": version,
        })

    if args.format == "json":
        sys.stdout.write(render.render_json(results))
        return

    console = Console(args.color)
    console.heading(
        f"Backtrace across {len(results)} frame"
        f"{'s' if len(results) != 1 else ''} ({support._linux(meta)})")
    console.blank()
    table_rows = []
    row_tones = []
    for index, row in enumerate(results):
        if not row["found"]:
            table_rows.append((f"#{index}", row["frame"], "not in index", "-"))
            row_tones.append("muted")
            continue
        location = f"{row['path']}:{row['line']}"
        subsystem = row["label"]
        ambiguity = (f"  (+{row['ambiguous']} more defs)"
                     if row["ambiguous"] else "")
        table_rows.append((f"#{index}", row["frame"], location,
                           subsystem + ambiguity))
        row_tones.append(None)
    console.table(("FRAME", "FUNCTION", "LOCATION / STATUS", "SUBSYSTEM / AREA"),
                  table_rows, tones={0: "muted", 1: "success", 3: "accent"},
                  row_tones=row_tones)

    counts: dict[str, int] = {}
    for row in results:
        if row["found"]:
            key = row["area"] or row["subsystem"] or "?"
            counts[key] = counts.get(key, 0) + 1
    if counts:
        console.blank()
        console.heading("Areas touched")
        console.table(("AREA", "FRAMES"), [
            (name, f"{count:,}")
            for name, count in sorted(counts.items(), key=lambda item: -item[1])
        ], align_right=(1,), tones={0: "accent"})


def cmd_calls(args, support):
    conn, meta = support.open_index(args)
    support._reject_symbol_size_sort(args, query.SYMBOL_KINDS)
    if meta.get("has_calls") != "1":
        advice = support._call_graph_rebuild_advice(args, meta)
        support._die(
            f"this index ({support.index_version(meta)}) has no call graph — "
            f"{advice}")
    target_spec = support._normalize_target_spec(meta, args.target)
    resolution = (support.resolve_or_die(conn, target_spec)
                  if ":" in target_spec
                  else query.resolve_symbol(conn, target_spec))
    if resolution.target is None:
        support._die(resolution.note)
    support._require_exact_line_qualifier(conn, target_spec)
    target = resolution.target
    if (target.kind != "symbol"
            or target.symbol_kind not in ("function", "syscall")):
        support._die(f"{target.display} is not a function or syscall")
    callable_alternatives = [
        candidate for candidate in resolution.candidates
        if candidate.symbol_kind in ("function", "syscall")
    ]
    if callable_alternatives:
        candidates = [target, *callable_alternatives]
        same_file = len({candidate.path for candidate in candidates}) \
            < len(candidates)
        qualifier = "path:line" if same_file else "path:symbol"
        examples = ", ".join(
            f"{candidate.path}:{candidate.line}" if same_file
            else candidate.display
            for candidate in candidates[:3])
        support._die(
            f"{len(callable_alternatives) + 1} callable definitions are named "
            f"{target.name!r}; qualify the target as {qualifier}"
            + (f" (for example: {examples})" if examples else ""))
    if support._split_list(args.kinds):
        selected_kinds = support.kinds_from_args(args, None)
        invalid = [kind for kind in selected_kinds
                   if kind not in ("function", "syscall")]
        if not selected_kinds or invalid:
            support._die(
                "calls only lists function and syscall identities; use "
                "--kinds function,syscall")

    narrowing = bool(
        args.grep or args.static_only or args.no_static or args.exported
        or support._split_list(args.kinds))
    fetch = 0 if narrowing or args.sort != "name" else args.limit
    explicit_columns = support._split_list(args.columns)
    want_subsystem = (
        args.format != "names"
        and (args.with_subsystem or "subsystem" in explicit_columns))
    default_columns = ("kind", "name", "path", "line", "occurrences",
                       "resolution") + (
                           ("subsystem",) if want_subsystem else ())

    if args.callers:
        entries = support._post_filter(
            query.callers(conn, target.id, limit=fetch), args)
        query.sort_entries(entries, args.sort)
        if args.limit:
            entries = entries[:args.limit]
        if want_subsystem:
            query.annotate_subsystems(conn, entries)
        support.emit(
            entries, args, {"function"}, want_subsystem,
            f"Functions that call {target.display}  [{support._linux(meta)}]\n",
            index=support.index_version(meta), default_columns=default_columns)
        return

    entries = query.callee_entries(conn, target.id, limit=fetch)
    entries = support._post_filter(entries, args)
    query.sort_entries(entries, args.sort)
    if args.limit:
        entries = entries[:args.limit]
    if want_subsystem:
        query.annotate_subsystems(conn, entries)
    support.emit(
        entries, args, {"function"}, want_subsystem,
        f"Functions called by {target.display}  [{support._linux(meta)}]\n",
        index=support.index_version(meta),
        default_columns=default_columns)


def cmd_relationships(args, support):
    """Show ownership overlap and conservative direct-call flow."""
    if args.via == "ownership":
        if (args.direction != "both" or args.include_internal
                or args.min_calls != 1):
            support._die(
                "--direction, --include-internal and --min-calls apply only "
                "to call relationships")
    elif args.via == "calls" and args.min_shared != 1:
        support._die("--min-shared applies only to ownership relationships")
    conn, meta = support.open_index(args)
    subsystem, note = support._relationship_subsystem(
        conn, meta, args.target)
    version = support.index_version(meta)

    overlaps = []
    if args.via in {"all", "ownership"}:
        overlaps = relationships.ownership_overlaps(
            conn, subsystem["id"], min_files=args.min_shared,
            limit=args.limit)

    has_calls = meta.get("has_calls") == "1"
    if args.via == "calls" and not has_calls:
        advice = support._call_graph_rebuild_advice(args, meta)
        support._die(f"this index ({version}) has no call graph — {advice}")
    flows = []
    coverage = None
    if args.via in {"all", "calls"} and has_calls:
        flows = relationships.call_flows(
            conn, subsystem["id"], direction=args.direction,
            include_internal=args.include_internal, min_edges=args.min_calls,
            limit=args.limit)
        coverage = relationships.call_resolution_coverage(
            conn, subsystem["id"])

    summary = {
        "name": subsystem["name"],
        "status": subsystem["status"],
        "claimed_files": subsystem["n_files"],
        "primary_files": subsystem["n_primary_files"],
    }
    payload = {
        "subsystem": summary,
        "resolved_from": note,
        "index": version,
        "call_graph_available": has_calls,
        "ownership_overlaps": [row.as_dict() for row in overlaps],
        "call_flows": [row.as_dict() for row in flows],
        "outgoing_call_resolution": coverage,
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return

    if args.format == "csv":
        fields = (
            "relationship", "direction", "selected_subsystem", "subsystem",
            "source_subsystem", "target_subsystem", "unclassified", "edges",
            "shared_files", "selected_files", "other_files",
            "selected_coverage", "other_coverage", "jaccard",
            "callers", "callees", "source_files", "target_files", "internal",
            "total_calls", "resolved_calls", "same_file", "included_source",
            "unique_global", "ambiguous", "macro", "indirect", "unresolved",
            "index",
        )
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        for row in overlaps:
            writer.writerow({
                "relationship": "ownership",
                "direction": "overlap",
                "selected_subsystem": subsystem["name"],
                "subsystem": row.subsystem,
                "shared_files": row.shared_files,
                "selected_files": row.selected_files,
                "other_files": row.other_files,
                "selected_coverage": row.selected_coverage,
                "other_coverage": row.other_coverage,
                "jaccard": row.jaccard,
                "index": version,
            })
        for row in flows:
            other = row.subsystem or ""
            source_subsystem = (
                subsystem["name"] if row.direction == "outgoing" else other)
            target_subsystem = (
                other if row.direction == "outgoing" else subsystem["name"])
            writer.writerow({
                "relationship": "call",
                "direction": row.direction,
                "selected_subsystem": subsystem["name"],
                "subsystem": other,
                "source_subsystem": source_subsystem,
                "target_subsystem": target_subsystem,
                "unclassified": row.unclassified,
                "edges": row.edges,
                "callers": row.callers,
                "callees": row.callees,
                "source_files": row.source_files,
                "target_files": row.target_files,
                "internal": row.internal,
                "index": version,
            })
        if coverage is not None:
            writer.writerow({
                "relationship": "call_resolution",
                "direction": "outgoing",
                "selected_subsystem": subsystem["name"],
                "total_calls": coverage["total"],
                "resolved_calls": coverage["resolved"],
                "same_file": coverage["same_file"],
                "included_source": coverage["included_source"],
                "unique_global": coverage["unique_global"],
                "ambiguous": coverage["ambiguous"],
                "macro": coverage["macro"],
                "indirect": coverage["indirect"],
                "unresolved": coverage["unresolved"],
                "index": version,
            })
        return

    console = Console(args.color)
    console.heading(
        f"{subsystem['name']} relationships  [{support._linux(meta)}]",
        tone="accent")
    if note:
        console.note(note)
    console.field("Files", f"{subsystem['n_files']:,} claimed, "
                  f"{subsystem['n_primary_files']:,} primary")

    if args.via in {"all", "ownership"}:
        console.blank()
        console.heading("Ownership overlap")
        if overlaps:
            console.table(("SHARED", "THIS", "OTHER", "JACCARD", "SUBSYSTEM"), [
                (f"{row.shared_files:,}", f"{row.selected_coverage:.1%}",
                 f"{row.other_coverage:.1%}", f"{row.jaccard:.1%}", row.subsystem)
                for row in overlaps
            ], align_right=(0, 1, 2, 3), tones={4: "accent"})
        else:
            console.text("no overlap at this threshold", tone="muted")

    if args.via in {"all", "calls"}:
        console.blank()
        console.heading("Direct C invocation flow")
        if not has_calls:
            advice = support._call_graph_rebuild_advice(args, meta)
            console.text(f"unavailable — {advice}", tone="warning", wrap=False)
        elif flows:
            table_rows = []
            for row in flows:
                label = ("unclassified (MAINTAINERS catch-all)"
                         if row.unclassified else (row.subsystem or "?"))
                if row.internal:
                    label += " (internal)"
                table_rows.append((row.direction, f"{row.edges:,}",
                                   f"{row.callers:,}", f"{row.callees:,}", label))
            console.table(("DIRECTION", "EDGES", "CALLERS", "CALLEES", "SUBSYSTEM"),
                          table_rows, align_right=(1, 2, 3),
                          tones={0: "info", 4: "accent"})
        else:
            console.text("no resolved cross-subsystem calls at this threshold",
                         tone="muted")
        if coverage is not None:
            excluded = (
                coverage["ambiguous"] + coverage["macro"]
                + coverage["indirect"] + coverage["unresolved"])
            console.blank()
            console.heading("Outgoing resolution")
            console.field("Resolved", f"{coverage['resolved']:,}/"
                          f"{coverage['total']:,} edges", tone="success")
            console.field("Other outcomes", f"{excluded:,} records",
                          tone="warning" if excluded else "muted")
            console.text("Ambiguous, macro, indirect, and unresolved records "
                         "remain coverage evidence.", tone="muted")
