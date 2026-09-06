"""Rank indexed kernel documentation using explicit, inspectable evidence."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .models import Entry, Target
from .paths import glob_under


@dataclass(frozen=True)
class DocumentationMatch:
    entry: Entry
    reasons: tuple[str, ...]


def documentation_scope(value: str) -> str:
    """Normalize a literal path below Documentation, never a glob or disk path."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("documentation scope must not be empty")
    value = value.strip().rstrip("/")
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(
            "documentation scope must be a relative path below Documentation"
        )
    if parts[0] != "Documentation":
        value = "Documentation/" + value
    return value


def rank_documentation(
    conn: sqlite3.Connection,
    t: Target,
    specific_owners: list,
    limit: int = 30,
    *,
    under: str | None = None,
) -> list[DocumentationMatch]:
    """Rank Documentation files by direct, ownership, and lexical evidence.

    Limits are applied only after all evidence has been combined.  This keeps
    a broad area or incidental secondary MAINTAINERS match from filling the
    result before a specific file's documentation is considered.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("documentation limit must be a non-negative integer")
    scope = documentation_scope(under) if under is not None else "Documentation"
    docs = conn.execute(
        "SELECT id,path,name,size,lines FROM files"
        " WHERE path=? OR path GLOB ? ORDER BY path",
        (scope, glob_under(scope)),
    ).fetchall()
    if not docs:
        return []

    stop = {
        "api",
        "core",
        "doc",
        "docs",
        "driver",
        "drivers",
        "file",
        "files",
        "kernel",
        "linux",
        "main",
        "subsystem",
        "system",
    }

    def tokens(value: str) -> set[str]:
        stem = re.sub(r"\.[^./]+$", "", value)
        return {
            word
            for word in re.findall(r"[a-z0-9]+", stem.lower())
            if len(word) >= 3 and word not in stop
        }

    def lexical(source: set[str], candidate: set[str]) -> int:
        score = 0
        for left in source:
            best = 0
            for right in candidate:
                if left == right:
                    best = max(best, 30 + min(len(left), 12))
                    continue
                common = 0
                for a, b in zip(left, right):
                    if a != b:
                        break
                    common += 1
                if common >= 4:
                    best = max(best, 5 + min(common, 12))
            score += best
        return score

    path = t.path or ""
    parts = [part for part in path.split("/") if part]
    file_id = t.file_id or (t.id if t.kind == "file" else None)
    identity_terms = tokens(parts[-1]) if parts else set()
    if t.kind == "symbol":
        identity_terms.update(tokens(t.name))
    path_terms = tokens("/".join(parts[-3:]))
    semantic_terms = set(path_terms)
    if t.kind == "symbol":
        semantic_terms.update(tokens(t.name))
    elif file_id is not None:
        # Macro-heavy generated headers can contain tens of thousands of names.
        # A bounded set of declaration identities retains useful semantic hints
        # without turning one interactive docs query into hundreds of millions
        # of token comparisons.  The target path and owner evidence remain
        # unbounded and carry the strongest tiers below.
        for row in conn.execute(
            "SELECT name FROM symbols WHERE file_id=?"
            " AND kind IN ('function','syscall','struct','union','enum',"
            "              'typedef','variable')"
            " GROUP BY name"
            " ORDER BY MIN(CASE kind WHEN 'function' THEN 0"
            "  WHEN 'syscall' THEN 0 WHEN 'struct' THEN 1"
            "  WHEN 'union' THEN 1 WHEN 'enum' THEN 1"
            "  WHEN 'typedef' THEN 2 ELSE 3 END),"
            " MAX(is_exported) DESC,LENGTH(name) DESC,name LIMIT 192",
            (file_id,),
        ):
            semantic_terms.update(tokens(row["name"]))

    for owner in specific_owners:
        semantic_terms.update(tokens(owner["name"]))

    owner_rank = {row["id"]: rank for rank, row in enumerate(specific_owners)}
    owner_paths: dict[str, int] = {}
    if owner_rank:
        placeholders = ",".join("?" for _ in owner_rank)
        for row in conn.execute(
            "SELECT f.path,p.subsystem_id FROM files f"
            " JOIN path_subsys p ON p.ref_kind='file' AND p.ref_id=f.id"
            " WHERE f.path GLOB 'Documentation/*'"
            f" AND p.subsystem_id IN ({placeholders})",
            tuple(owner_rank),
        ):
            rank = owner_rank[row["subsystem_id"]]
            owner_paths[row["path"]] = min(owner_paths.get(row["path"], rank), rank)

    direct_exact = (
        path if path.startswith("Documentation/") and t.kind != "dir" else None
    )
    direct_prefix: str | None = None
    if t.kind == "dir" and (not path or path == "Documentation"):
        direct_prefix = "Documentation/"
    elif t.kind == "dir" and path.startswith("Documentation/"):
        direct_prefix = path.rstrip("/") + "/"
    elif path.startswith("Documentation/"):
        direct_prefix = path.rpartition("/")[0] + "/"

    aliases = {
        "arch": "arch",
        "block": "block",
        "drivers": "driver-api",
        "fs": "filesystems",
        "include": "core-api",
        "kernel": "core-api",
        "net": "networking",
        "security": "security",
        "sound": "sound",
        "tools": "tools",
        "virt": "virt",
    }
    area_roots: list[str] = []
    if t.kind == "dir" and parts and parts[0] != "Documentation":
        last = aliases.get(parts[-1], parts[-1])
        top = aliases.get(parts[0], parts[0])
        candidates = [last]
        if len(parts) > 1:
            candidates.append(f"{top}/{parts[-1]}")
        candidates.append(top)
        for area in candidates:
            area = area.strip("/")
            prefix = f"Documentation/{area}/"
            standalone = re.compile(rf"^Documentation/{re.escape(area)}\.[^/]+$")
            if area not in area_roots and any(
                row["path"].startswith(prefix) or standalone.fullmatch(row["path"])
                for row in docs
            ):
                area_roots.append(area)

    studying_code = t.kind in {"symbol", "file"} and not path.startswith(
        "Documentation/"
    )
    ranked: list[tuple[tuple, DocumentationMatch]] = []
    for row in docs:
        doc_path = row["path"]
        doc_terms = tokens(doc_path.removeprefix("Documentation/"))
        path_score = lexical(identity_terms, doc_terms)
        semantic_score = lexical(semantic_terms, doc_terms)
        owner = owner_paths.get(doc_path)
        area = next(
            (
                rank
                for rank, root in enumerate(area_roots)
                if doc_path.startswith(f"Documentation/{root}/")
                or re.fullmatch(rf"Documentation/{re.escape(root)}\.[^/]+", doc_path)
            ),
            None,
        )
        exact = direct_exact == doc_path
        contained = direct_prefix is not None and doc_path.startswith(direct_prefix)

        if exact:
            tier = 0
        elif contained:
            tier = 1
        elif owner is not None and semantic_score:
            tier = 2
        elif path_score:
            tier = 3
        elif owner is not None:
            tier = 4
        elif area is not None:
            tier = 5
        else:
            continue
        stem = row["name"].rsplit(".", 1)[0].lower()
        overview = 0 if stem in {"index", "readme", "overview"} else 1
        depth = doc_path.count("/")
        guide = doc_path.lower().endswith(
            (".rst", ".txt", ".md")
        ) and not doc_path.startswith("Documentation/devicetree/bindings/")
        # Within an evidence tier, code study favors prose over schemas and
        # build scripts. Explicit Documentation targets retain their priority.
        purpose = int(studying_code and not guide)
        reasons = []
        if exact:
            reasons.append("exact documentation target")
        if contained:
            reasons.append("in the target documentation directory")
        if owner is not None:
            reasons.append("claimed by target owner: " + specific_owners[owner]["name"])
        if path_score:
            reasons.append("target name or filename terms match documentation path")
        if semantic_score:
            reasons.append(
                "target, declaration, or owner terms match documentation path"
            )
        if area is not None:
            reasons.append("code-to-documentation area: " + area_roots[area])
        if studying_code and guide:
            reasons.append("prose guide preferred for code study")
        match = DocumentationMatch(
            Entry(
                kind="file",
                name=row["name"],
                path=doc_path,
                size=row["size"],
                lines=row["lines"],
            ),
            tuple(reasons),
        )
        ranked.append(
            (
                (
                    tier,
                    purpose,
                    -semantic_score,
                    owner if owner is not None else 10**6,
                    area if area is not None else 10**6,
                    overview,
                    depth,
                    doc_path,
                ),
                match,
            )
        )

    ranked.sort(key=lambda item: item[0])
    return [match for _, match in (ranked[:limit] if limit else ranked)]
