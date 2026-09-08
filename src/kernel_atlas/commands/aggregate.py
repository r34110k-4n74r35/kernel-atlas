"""CLI handler for detailed C struct and union study reports."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from ..queries import links
from ..queries import query
from ..queries.type_relationships import structure_relationships
from ..presentation import render
from ..presentation.terminal import Console, terminal_width


def cmd_struct(args, support):
    """Explain an indexed C struct/union and every retained member."""
    conn, meta = support.open_index(args)
    requested = (args.target or "").strip()
    kind_hint = None
    target_text = requested
    for aggregate_kind in ("struct", "union"):
        prefix = aggregate_kind + " "
        if target_text.startswith(prefix):
            kind_hint = aggregate_kind
            target_text = target_text[len(prefix):].strip()
            break
    spec = support._normalize_target_spec(meta, target_text)
    lookup_spec = f"{kind_hint} {spec}" if kind_hint else spec
    resolution = query.resolve_structure(conn, lookup_spec)
    if resolution.target is None:
        kinds = set(filter(None, meta.get("kinds", "").split(",")))
        needed = {kind_hint} if kind_hint else {"struct", "union"}
        suffix = ("; rebuild the index with struct/union symbols enabled"
                  if not (needed & kinds) else "")
        support._die(
            (resolution.note
             or f"could not resolve aggregate {requested!r}") + suffix)

    support._require_exact_line_qualifier(conn, spec)

    if resolution.candidates and not args.all:
        candidates = [resolution.target, *resolution.candidates]
        selectors = [query.structure_selector(conn, candidate)
                     for candidate in candidates]
        exact = [selector for selector in selectors if selector is not None]
        examples = ", ".join(shlex.quote(selector) for selector in exact[:5])
        more = (f", and {len(exact) - 5} more" if len(exact) > 5 else "")
        if len(exact) != len(candidates):
            example_note = (f"; available exact selectors: {examples}{more}"
                            if examples else "")
            support._die(
                f"{len(candidates)} aggregate definitions match "
                f"{requested!r}; at least one cannot be isolated by "
                f"path:name or path:line{example_note}; pass --all")
        forms = {"path:line" if selector.rpartition(":")[2].isdigit()
                 else "path:name" for selector in exact}
        qualifier = " or ".join(sorted(forms))
        support._die(
            f"{len(candidates)} aggregate definitions match {requested!r}; "
            f"use {qualifier} for one exact identity "
            f"(for example: {examples}{more}), or pass --all")

    targets = ([resolution.target, *resolution.candidates]
               if args.all else [resolution.target])
    definitions = []
    source_root = support.find_source_tree(meta)
    for target in targets:
        detail = query.structure_detail(conn, target)
        composition = query.all_subsystems(
            conn, "file", target.file_id or target.id)
        subsystems = [row for row in composition
                      if row["name"] not in query.CATCH_ALL]
        catch_all = next((
            row for row in composition
            if row["name"] in query.CATCH_ALL and bool(row["is_primary"])
        ), None)
        unmatched = not any(bool(row["is_primary"]) for row in composition)
        unclassified = None
        if catch_all is not None or unmatched:
            unclassified = {
                "is_primary": (bool(catch_all["is_primary"])
                               if catch_all is not None else False),
                "unmatched": unmatched,
                "match_score": (catch_all["score"]
                                if catch_all is not None else None),
                "match_rank": (catch_all["rank"]
                               if catch_all is not None else None),
                "maintainers_section": (catch_all["name"]
                                        if catch_all is not None else None),
            }
        area = query.describe_area(target.path)
        related = (query.documentation_for(conn, target, limit=args.max_docs)
                   if args.max_docs else [])
        recorded_root = meta.get("tree_path")
        display_root = (source_root if source_root is not None else
                        Path(recorded_root).expanduser()
                        if recorded_root else None)
        source = (support.source_member(display_root, target.path)
                  if display_root is not None else None)
        detail.update({
            "index": support.index_version(meta),
            "area": ({"name": area[0], "description": area[1]}
                     if area else None),
            "subsystems": [support._subsystem_payload(row)
                           for row in subsystems],
            "unclassified_ownership": unclassified,
            "links": support._links_for(meta, target),
            "related_documentation": [
                {
                    "path": entry.path,
                    "name": entry.name,
                    "lines": entry.lines,
                    "size": entry.size,
                    "links": links.links(
                        support.index_version(meta), entry.path,
                        source=meta.get("source")),
                }
                for entry in related
            ],
            "source_path": str(source) if source is not None else None,
            "source_exists": ((source.exists() or source.is_symlink())
                              if source is not None else None),
            "layout_limits": [
                "All preprocessor alternatives are source possibilities, not "
                "members proven to coexist in one configured kernel.",
                "Byte offsets, padding, alignment, and sizeof require a "
                "concrete configuration, architecture, compiler ABI, and "
                "expanded macros.",
            ],
        })
        if getattr(args, "relations", False) or getattr(args, "used_by", False):
            detail["type_relationships"] = structure_relationships(
                conn, target, outgoing=getattr(args, "relations", False),
                incoming=True, limit=getattr(args, "max_relations", 100),
            )
        definitions.append(detail)

    payload = {
        "query": args.target,
        "index": support.index_version(meta),
        "n_definitions": len(definitions),
        "definitions": definitions,
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return

    color = render.use_color(args.color)
    width = terminal_width(sys.stdout)
    for index, detail in enumerate(definitions):
        if index:
            print()
        sys.stdout.write(render.render_structure(detail, color, width))
        if "type_relationships" in detail:
            _render_relationships(detail["type_relationships"], Console(args.color))
    target_spec = shlex.quote(
        f"{definitions[0]['path']}:{definitions[0]['line']}")
    prefix = support._command_prefix(args, meta)
    next_lines = [
        f"{prefix} show {target_spec}",
        f"{prefix} docs {target_spec}",
    ]
    if definitions[0]["links"]:
        next_lines.append(f"{prefix} web {target_spec}")
    Console(args.color).commands(next_lines)


def _render_relationships(relationships: dict, console: Console) -> None:
    for direction, title in (("outgoing", "Types referenced by members"),
                             ("incoming", "Declarations using this type")):
        if not relationships[direction + "_requested"]:
            continue
        console.blank()
        console.heading(title)
        relations = relationships[direction]
        if not relations:
            console.text("No explicit indexed declaration relationships found.", tone="muted")
            continue
        rows = []
        for relation in relations:
            owner = relation["owner"]
            identity = owner["name"]
            if relation["member"]:
                identity += "." + relation["member"]
            location = f"{owner['path']}:{relation['line']}"
            candidates = "; ".join(
                f"{candidate['path']}:{candidate['line']} ({candidate['visibility'].replace('_', ' ')})"
                for candidate in relation["candidates"]
            ) or "No indexed definition"
            if relation["candidates_truncated"]:
                candidates += (f"; showing {len(relation['candidates'])} of "
                               f"{relation['candidates_total']} candidates")
            spelling = ((relation["kind"] + " ") if relation["kind"] else "") + relation["name"]
            rows.append([
                relation["role"].replace("_", " "), f"{identity} — {location}",
                spelling, f"{relation['resolution']}: {candidates}",
            ])
        console.table(["Relationship", "Declaration", "Type", "Candidate definitions"],
                      rows, tones={0: "info", 2: "accent", 3: "muted"})
        if relationships[direction + "_truncated"]:
            console.note(f"Showing the first {relationships['limit']} {direction} relationships; "
                         "increase --max-relations to see more.")
        if any(relation["candidates_truncated"] for relation in relations):
            console.note(f"Candidate definitions are limited to {relationships['candidate_limit']} "
                         "per relationship; ambiguity and matching use the full candidate set.")
    for limitation in relationships["limits"]:
        console.note(limitation, tone="info")
