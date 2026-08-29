"""Terminal rendering for detailed C aggregate study reports."""

from __future__ import annotations

import io
import textwrap
from collections.abc import Callable

from .render_format import paint


def render_structure(
    detail: dict,
    color: bool,
    max_width: int = 0,
    *,
    paint_fn: Callable[[str, str, bool], str] | None = None,
) -> str:
    """Render a detailed, hierarchical structure study report."""
    colorize = paint if paint_fn is None else paint_fn
    width = max(72, max_width or 100)
    out = io.StringIO()
    aggregate_kind = detail.get("kind", "struct")
    if detail.get("is_anonymous"):
        title = f"{detail['name']} (typedef to anonymous {aggregate_kind})"
    else:
        title = detail.get("c_name") or f"{aggregate_kind} {detail['name']}"
    index = detail.get("index")
    if index:
        title += f"  [Linux {index}]"
    out.write(colorize(title, "1;36", color) + "\n")

    def field(label: str, value) -> None:
        if value is None or value == "" or value == []:
            return
        out.write(f"  {label:<14} {value}\n")

    span = f"{detail['path']}:{detail['line']}"
    if detail.get("end_line") and detail["end_line"] != detail.get("line"):
        span += f"-{detail['end_line']}"
    field("defined in", span)
    field("signature", detail.get("signature"))
    field("aliases", ", ".join(detail.get("aliases", [])))
    source_path = detail.get("source_path")
    if source_path:
        field(
            "source",
            source_path
            if detail.get("source_exists") is not False
            else f"{source_path} (missing)",
        )
    area = detail.get("area")
    field("area", area.get("name") if isinstance(area, dict) else area)
    subsystems = detail.get("subsystems", [])
    field(
        "subsystems",
        ", ".join(
            row["name"] + (" (primary)" if row.get("is_primary") else "")
            for row in subsystems
        ),
    )
    unclassified = detail.get("unclassified_ownership")
    if unclassified:
        if unclassified.get("unmatched"):
            field("ownership", "no primary MAINTAINERS match")
        elif unclassified.get("maintainers_section"):
            field(
                "ownership",
                "primary only through "
                + unclassified["maintainers_section"]
                + " catch-all",
            )
    field("conditions", " -> ".join(detail.get("conditions", [])))
    coverage = detail.get("documentation_coverage", 0.0)
    semantic = detail.get("semantic_description_count", 0)
    semantic_note = (
        f"; {semantic} parser-supplied macro explanation"
        f"{'s' if semantic != 1 else ''}"
        if semantic
        else ""
    )
    field(
        "members",
        f"{detail.get('direct_member_count', 0)} direct; "
        f"{detail.get('total_member_count', 0)} including nested; "
        f"{detail.get('documented_member_count', 0)}/"
        f"{detail.get('documentable_member_count', detail.get('total_member_count', 0))} "
        f"named members source-documented "
        f"({coverage:.0%}){semantic_note}",
    )
    field("parse", "complete" if detail.get("parse_complete") else "partial")

    if detail.get("summary"):
        out.write("\n" + colorize("  Summary", "1", color) + "\n")
        out.write(
            textwrap.fill(
                detail["summary"],
                width=width,
                initial_indent="    ",
                subsequent_indent="    ",
            )
            + "\n"
        )
    if detail.get("description"):
        out.write(
            "\n" + colorize("  Description / notes", "1", color) + "\n"
        )
        for paragraph in detail["description"].split("\n\n"):
            out.write(
                textwrap.fill(
                    paragraph,
                    width=width,
                    initial_indent="    ",
                    subsequent_indent="    ",
                )
                + "\n"
            )

    out.write(
        "\n" + colorize("  Members (source order)", "1", color) + "\n"
    )

    def walk(members: list[dict], depth: int = 0) -> None:
        for member in members:
            indent = "    " + "  " * depth
            name = member["name"] or "<anonymous>"
            shape: list[str] = []
            if member.get("kind") not in {"field", "function_pointer"}:
                shape.append(member["kind"])
            if member.get("kind") == "function_pointer":
                shape.append("callback")
            if member.get("bit_width") is not None:
                shape.append(f"bitfield:{member['bit_width']}")
            if member.get("array_dimensions"):
                dims = "".join(
                    f"[{value}]" for value in member["array_dimensions"]
                )
                shape.append(f"array{dims}")
            if member.get("visibility") != "unspecified":
                shape.append(member["visibility"])
            suffix = f"  ({', '.join(shape)})" if shape else ""
            line = str(member["line"])
            if member.get("end_line") != member.get("line"):
                line += f"-{member['end_line']}"
            heading = (
                f"{indent}{member['ordinal'] + 1:>3}. {name}{suffix}  [line {line}]"
            )
            out.write(
                colorize(heading, "33" if depth else "1;33", color) + "\n"
            )

            def member_field(label: str, value) -> None:
                if value is None or value == "" or value == []:
                    return
                prefix = indent + "     " + f"{label:<12} "
                out.write(
                    textwrap.fill(
                        str(value),
                        width=width,
                        initial_indent=prefix,
                        subsequent_indent=" " * len(prefix),
                    )
                    + "\n"
                )

            member_field("type", member.get("type"))
            member_field("declaration", member.get("declaration"))
            if member.get("generated_by"):
                member_field("generated by", member["generated_by"])
            if member.get("conditions"):
                member_field("conditions", " -> ".join(member["conditions"]))
            if member.get("referenced_kind"):
                member_field(
                    "references",
                    f"{member['referenced_kind']} {member['referenced_name']}",
                )
            description = member.get("description")
            if description:
                source = member.get("description_source") or "source"
                member_field("description", f"{description} [{source}]")
            else:
                member_field("description", "(undocumented)")
            walk(member.get("children", []), depth + 1)

    walk(detail.get("members", []))

    if detail.get("unmatched_member_docs"):
        out.write(
            "\n"
            + colorize("  Documented but not matched", "1;33", color)
            + "\n"
        )
        for name, description in detail["unmatched_member_docs"].items():
            out.write(
                textwrap.fill(
                    f"@{name}: {description}",
                    width=width,
                    initial_indent="    ",
                    subsequent_indent="    ",
                )
                + "\n"
            )
    if detail.get("warnings"):
        out.write("\n" + colorize("  Parse warnings", "1;33", color) + "\n")
        for warning in detail["warnings"]:
            out.write(f"    - {warning}\n")

    docs = detail.get("related_documentation", [])
    if docs:
        out.write(
            "\n" + colorize("  Related Documentation", "1", color) + "\n"
        )
        for item in docs:
            out.write(f"    {item['path']}\n")
    links_payload = detail.get("links", {})
    if links_payload:
        out.write("\n" + colorize("  Links", "1", color) + "\n")
        for key in ("elixir", "ident", "git", "github", "docs"):
            if links_payload.get(key):
                out.write(f"    {key:<8} {links_payload[key]}\n")
    for limitation in detail.get("layout_limits", []):
        out.write(colorize(f"\n  Note: {limitation}\n", "90", color))
    return out.getvalue()
