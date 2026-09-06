"""Terminal rendering for detailed C aggregate study reports."""

from __future__ import annotations

import io
import re
from collections.abc import Callable

from .render_format import paint
from .terminal import clean, display_width, wrap_text


def _report_text(value) -> str:
    """Fold source whitespace for a report while escaping terminal controls.

    Member documentation can retain paragraph breaks; declarations and
    summaries may also contain source indentation. Keep ordinary repeated
    spaces (including those in paths), and normalize only whitespace around
    intentional line breaks or tabs before sanitizing the remaining controls.
    """
    return clean(re.sub(r"[ \t\r\n\f\v]+",
                        lambda match: " " if match[0].strip(" ") else match[0],
                        str(value)))


def render_structure(
    detail: dict,
    color: bool,
    max_width: int = 0,
    *,
    paint_fn: Callable[[str, str, bool], str] | None = None,
) -> str:
    """Render a detailed, hierarchical structure study report."""
    colorize = paint if paint_fn is None else paint_fn
    width = max(1, max_width or 100)
    out = io.StringIO()

    def write(text: str, code: str | None = None, *, indent: str = "",
              continuation: str | None = None) -> None:
        lines = wrap_text(indent + _report_text(text), width,
                          subsequent_indent=(indent if continuation is None
                                             else continuation))
        for line in lines:
            out.write((colorize(line, code, color) if code else line) + "\n")

    def heading(text: str, code: str = "1") -> None:
        out.write("\n")
        write(text, code, indent="  ")

    def write_field(label: str, value, *, indent: str = "  ",
                    label_width: int = 14, code: str | None = None) -> None:
        if value is None or value == "" or value == []:
            return
        label, value = clean(label), _report_text(value)
        prefix = indent + label + " " * max(1, label_width + 1 - display_width(label))
        prefix_width = display_width(prefix)
        if width < prefix_width + 12:
            write(label + ": " + value, code, indent=indent,
                  continuation=indent)
            return
        for index, part in enumerate(wrap_text(value, width - prefix_width) or [""]):
            left = prefix if index == 0 else " " * prefix_width
            out.write(colorize(left, "90", color)
                      + (colorize(part, code, color) if code else part) + "\n")

    aggregate_kind = detail.get("kind", "struct")
    if detail.get("is_anonymous"):
        title = f"{detail['name']} (typedef to anonymous {aggregate_kind})"
    else:
        title = detail.get("c_name") or f"{aggregate_kind} {detail['name']}"
    index = detail.get("index")
    if index:
        title += f"  [Linux {index}]"
    write(title, "1;36")

    def field(label: str, value) -> None:
        write_field(label, value)

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
    field(
        "members",
        f"{detail.get('direct_member_count', 0)} direct; "
        f"{detail.get('total_member_count', 0)} including nested",
    )
    field(
        "documented",
        f"{detail.get('documented_member_count', 0)}/"
        f"{detail.get('documentable_member_count', detail.get('total_member_count', 0))} "
        f"named members source-documented "
        f"({coverage:.0%})",
    )
    if semantic:
        field("macro notes", f"{semantic} parser-supplied macro explanation"
              f"{'s' if semantic != 1 else ''}")
    complete = detail.get("parse_complete")
    write_field("parse", "complete" if complete else "partial",
                code="32" if complete else "33")

    if detail.get("summary"):
        heading("Summary")
        write(detail["summary"], indent="    ")
    if detail.get("description"):
        heading("Description / notes")
        for paragraph in detail["description"].split("\n\n"):
            write(paragraph, indent="    ")

    heading("Members (source order)")

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
            member_heading = (
                f"{indent}{member['ordinal'] + 1:>3}. {name}{suffix}  [line {line}]"
            )
            write(member_heading, "33" if depth else "1;33",
                  continuation=indent + "     ")

            def member_field(label: str, value) -> None:
                write_field(label, value, indent=indent + "     ", label_width=12,
                            code="90" if value == "(undocumented)" else None)

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
        heading("Documented but not matched", "1;33")
        for name, description in detail["unmatched_member_docs"].items():
            write(f"@{name}: {description}", indent="    ")
    if detail.get("warnings"):
        heading("Parse warnings", "1;33")
        for warning in detail["warnings"]:
            write("- " + warning, "33", indent="    ", continuation="      ")

    docs = detail.get("related_documentation", [])
    if docs:
        heading("Related Documentation")
        for item in docs:
            write(item["path"], "36", indent="    ")
    links_payload = detail.get("links", {})
    if links_payload:
        heading("Links")
        for key in ("elixir", "ident", "git", "github", "docs"):
            if links_payload.get(key):
                write_field(key, links_payload[key], indent="    ", label_width=8,
                            code="36")
    for limitation in detail.get("layout_limits", []):
        out.write("\n")
        write(f"Note: {limitation}", "90", indent="  ")
    return out.getvalue()
