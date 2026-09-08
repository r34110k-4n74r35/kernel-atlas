"""Bounded literal evidence search over documentation retained in the index."""

from __future__ import annotations

from bisect import bisect_right
import json
import re
import sqlite3

from .documentation import documentation_scope
from .paths import glob_under

MAX_RESULTS = 5000
SNIPPET_LENGTH = 240


def _pattern(text, mentions):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("search text must not be empty")
    if len(text) > 1024:
        raise ValueError("search text must contain at most 1024 characters")
    if mentions:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", text):
            raise ValueError("--mentions requires a C identifier")
        return re.compile(r"(?<![A-Za-z_0-9])" + re.escape(text) + r"(?![A-Za-z_0-9])")
    return re.compile(re.escape(text), re.IGNORECASE)


def _snippet(text, start, end):
    # Center excerpts on the match, retaining enough context without dumping a
    # whole long paragraph into terminal or machine output.
    left = max(0, start - 70)
    right = min(len(text), max(end, left + SNIPPET_LENGTH))
    excerpt = " ".join(text[left:right].split())
    truncated = len(excerpt) > SNIPPET_LENGTH
    excerpt = excerpt[:SNIPPET_LENGTH]
    return ("…" if left else "") + excerpt + ("…" if right < len(text) or truncated else "")


def _headings(content):
    """Index Markdown ATX and common reST/setext headings in one bounded document."""
    offsets, headings = [], []
    previous = ""
    offset = previous_offset = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        markdown = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        if markdown:
            offsets.append(offset)
            headings.append(markdown[1])
        elif (previous.strip() and len(stripped) >= 3
              and len(set(stripped)) == 1 and stripped[0] in "=-~^\"'`:+#*"
              and len(stripped) >= len(previous.strip())):
            offsets.append(previous_offset)
            headings.append(previous.strip())
        previous, previous_offset = line, offset
        offset += len(line)
    return offsets, headings


def _document_hits(row, pattern):
    content = row["content"]
    offsets, headings = _headings(content)
    prior_offset, line, prior_line = 0, 1, None
    for match in pattern.finditer(content):
        line += content.count("\n", prior_offset, match.start())
        prior_offset = match.start()
        if line == prior_line:
            continue
        prior_line = line
        heading_index = bisect_right(offsets, match.start()) - 1
        yield {
            "path": row["path"], "line": line, "line_kind": "match",
            "source_kind": "documentation",
            "heading": headings[heading_index] if heading_index >= 0 else None,
            "snippet": _snippet(content, match.start(), match.end()),
        }


def _contract_hits(row, pattern):
    sections = [("function", row["name"]), ("summary", row["summary"]),
                ("description", row["description"]), ("context", row["context"]),
                ("returns", row["returns"])]
    for name, description in json.loads(row["parameters"]).items():
        sections.append((f"parameter @{name}", f"@{name}: {description}"))
    for section, text in sections:
        match = pattern.search(text or "")
        if match:
            yield {
                "path": row["path"], "line": row["line"],
                "line_kind": "documentation_block", "source_kind": "kernel-doc",
                "symbol": row["name"], "symbol_id": row["symbol_id"],
                "heading": row["name"], "section": section,
                "snippet": _snippet(text, match.start(), match.end()),
            }


def search(conn: sqlite3.Connection, text: str, *, mentions=False, limit=30,
           under=None, scope=None, include_documents=True, include_contracts=True) -> dict:
    """Find literal excerpts, streaming one stored document or contract at a time.

    Scope is a literal indexed file/directory, while ``under`` is explicitly a
    Documentation subtree. Ownership and symbol resolution never create hits.
    A zero limit means the documented safety ceiling rather than unbounded RAM.
    """
    pattern = _pattern(text, mentions)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("search limit must be a non-negative integer")
    effective_limit = min(limit or MAX_RESULTS, MAX_RESULTS)
    clauses, params = [], []
    for prefix in (documentation_scope(under) if under else None, scope):
        if prefix:
            clauses.append("(f.path=? OR f.path GLOB ?)")
            params.extend((prefix, glob_under(prefix)))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    matches = []
    # Keep query iteration bounded in Python even when --limit 0 is requested.
    # Matching uses Unicode-aware literal expressions; SQL owns literal path
    # filtering, so neither phrase syntax nor percent/underscore paths become SQL.
    sources = []
    if include_documents:
        sources.append((
            "SELECT f.path,d.file_id AS identity FROM document_text d "
            "JOIN files f ON f.id=d.file_id" + where + " ORDER BY f.path",
            _document_hits, "SELECT content FROM document_text WHERE file_id=?",
        ))
    if include_contracts:
        sources.append((
            "SELECT f.path,d.symbol_id AS identity FROM function_docs d "
            "JOIN symbols s ON s.id=d.symbol_id JOIN files f ON f.id=s.file_id"
            + where + " ORDER BY f.path,d.line,s.id", _contract_hits,
            "SELECT d.*,s.name FROM function_docs d JOIN symbols s ON s.id=d.symbol_id "
            "WHERE d.symbol_id=?",
        ))
    truncated = False
    for sql, hits, load_sql in sources:
        for identity in conn.execute(sql, params):
            # Sort lightweight identities, then fetch only one body. A query
            # must not materialize the complete documentation corpus to order it.
            stored = conn.execute(load_sql, (identity["identity"],)).fetchone()
            row = {**dict(stored), "path": identity["path"]}
            for hit in hits(row, pattern):
                if len(matches) == effective_limit:
                    truncated = True
                    break
                matches.append(hit)
            if truncated:
                break
        if truncated:
            break
    return {
        "query": text, "mode": "mentions" if mentions else "search",
        "matching": "case-sensitive identifier" if mentions else "case-insensitive literal",
        "scope": scope or None, "under": documentation_scope(under) if under else None,
        "matches": matches, "limit": effective_limit, "truncated": truncated,
        "truncation_reasons": ["result_limit"] if truncated else [],
        "note": (
            "Textual mentions do not establish a unique symbol identity. "
            if mentions else "Matches are textual evidence, independent of ownership. "
        ) + "Kernel-doc locations identify the comment block; documentation locations identify the matching line.",
    }
