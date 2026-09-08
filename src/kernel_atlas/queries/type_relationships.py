"""Declaration evidence linking aggregates, members and function signatures."""

from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3

from ..parsing.type_references import TypeReference, declaration_references
from .models import Target


CANDIDATE_LIMIT = 100
_OWNER = "s.id,s.file_id,s.name,s.kind,s.start_line,s.end_line,f.path"
_MEMBERS = (
    f"SELECT {_OWNER},m.id AS member_id,m.name AS member_name,"
    "m.declaration,m.start_line AS evidence_line,m.end_line AS evidence_end_line,"
    "m.conditions,m.generated_by FROM type_members m "
    "JOIN symbols s ON s.id=m.symbol_id JOIN files f ON f.id=s.file_id"
)
_FUNCTIONS = (
    f"SELECT {_OWNER},NULL AS member_id,NULL AS member_name,"
    "s.signature AS declaration,s.start_line AS evidence_line,"
    "s.start_line AS evidence_end_line,s.conditions,NULL AS generated_by "
    "FROM symbols s JOIN files f ON f.id=s.file_id"
)


class _Evidence:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.candidates: dict[tuple, dict] = {}
        self.scopes: dict[tuple[int, int, int], set[int]] = {}
        self.owners: dict[int, list] = {}

    def scope(self, file_id: int, start: int, end: int):
        key = file_id, start, end
        if key not in self.scopes:
            if len(self.scopes) >= 1024:
                self.scopes.pop(next(iter(self.scopes)))
            self.scopes[key] = {row[0] for row in self.conn.execute(
                "SELECT id FROM symbols WHERE file_id=? "
                "AND kind IN ('function','syscall') AND start_line<=? AND end_line>=?",
                (file_id, start, end),
            )}
        return self.scopes[key]

    def ownership(self, file_id: int) -> list:
        if file_id not in self.owners:
            self.owners[file_id] = [dict(row) for row in self.conn.execute(
                "SELECT s.name,p.is_primary FROM path_subsys p "
                "JOIN subsystems s ON s.id=p.subsystem_id "
                "WHERE p.ref_kind='file' AND p.ref_id=? ORDER BY p.rank,s.name",
                (file_id,),
            )]
        return self.owners[file_id]

    def definitions(self, reference: TypeReference, occurrence,
                    target_id: int | None = None) -> dict:
        key = (reference.kind, reference.name, occurrence["file_id"],
               occurrence["evidence_line"], occurrence["evidence_end_line"], target_id)
        if key in self.candidates:
            return self.candidates[key]
        if reference.kind:
            where = "s.kind=? AND s.name=? AND s.is_anonymous=0"
            params = reference.kind, reference.name
        else:
            where = ("s.kind IN ('struct','union','enum') AND EXISTS "
                     "(SELECT 1 FROM type_aliases a WHERE a.symbol_id=s.id AND a.name=?)")
            params = (reference.name,)
        rows = self.conn.execute(
            f"SELECT {_OWNER},s.conditions FROM symbols s "
            "JOIN files f ON f.id=s.file_id WHERE " + where +
            " ORDER BY f.path,s.start_line,s.id", params,
        )
        use_scope = self.scope(occurrence["file_id"], occurrence["evidence_line"],
                               occurrence["evidence_end_line"])
        # Count the complete set at each scope without materializing every
        # matching definition or looking up ownership for omitted candidates.
        groups = [{"rows": [], "total": 0, "target": None} for _ in range(3)]
        for candidate in rows:
            own_scope = self.scope(candidate["file_id"], candidate["start_line"],
                                   candidate["end_line"])
            same_file = candidate["file_id"] == occurrence["file_id"]
            if own_scope and (not same_file or not own_scope & use_scope
                              or candidate["start_line"] > occurrence["evidence_line"]):
                continue
            # A definition in a different C source file does not supply a tag
            # to this translation unit. Header inclusion remains unverified.
            if not same_file and not candidate["path"].lower().endswith((".h", ".h_shipped", ".hpp", ".hh")):
                continue
            priority = (2 if own_scope else 1 if same_file
                        and candidate["start_line"] <= occurrence["evidence_line"] else 0)
            group = groups[priority]
            group["total"] += 1
            entry = candidate, own_scope
            if len(group["rows"]) < CANDIDATE_LIMIT:
                group["rows"].append(entry)
            if candidate["id"] == target_id:
                group["target"] = entry
        # Retain conditional alternatives at the selected scope, never choose
        # an arbitrary same-spelled global identity over explicit local scope.
        selected = next((group for group in reversed(groups) if group["total"]), groups[0])
        # An incoming query must still recognize and display its exact target
        # even when the target sorts past the bounded candidate sample.
        if selected["target"] is not None and not any(
                entry[0]["id"] == target_id for entry in selected["rows"]):
            selected["rows"][-1] = selected["target"]
        result = []
        for candidate, scope in selected["rows"]:
            visibility = ("same_function" if scope else
                          "same_file" if candidate["file_id"] == occurrence["file_id"]
                          else "header_visibility_unverified")
            result.append({
                "id": candidate["id"], "name": candidate["name"],
                "kind": candidate["kind"], "path": candidate["path"],
                "line": candidate["start_line"], "end_line": candidate["end_line"],
                "visibility": visibility,
                "conditions": json.loads(candidate["conditions"]),
                "subsystems": self.ownership(candidate["file_id"]),
            })
        summary = {"rows": result, "total": selected["total"],
                   "contains_target": selected["target"] is not None}
        if len(self.candidates) >= 1024:
            self.candidates.pop(next(iter(self.candidates)))
        self.candidates[key] = summary
        return summary

    def relations(self, rows, *, target_id: int | None = None, spellings=None):
        for row in rows:
            if row["generated_by"]:
                continue
            function = row["member_id"] is None
            references = declaration_references(
                row["declaration"], name=row["name"] if function else row["member_name"],
                function=function,
            )
            for reference in references:
                if spellings is not None and (reference.kind, reference.name) not in spellings:
                    continue
                definitions = self.definitions(reference, row, target_id)
                candidates = definitions["rows"]
                # Plain type identifiers need direct indexed typedef evidence;
                # arbitrary scalar aliases are outside aggregate relationships.
                if not reference.kind and not definitions["total"]:
                    continue
                if target_id is not None and not definitions["contains_target"]:
                    continue
                yield {
                    **asdict(reference),
                    "owner": {
                        "id": row["id"], "name": row["name"], "kind": row["kind"],
                        "path": row["path"], "line": row["start_line"],
                        "subsystems": self.ownership(row["file_id"]),
                    },
                    "member": row["member_name"],
                    "line": row["evidence_line"], "end_line": row["evidence_end_line"],
                    "declaration": row["declaration"],
                    "conditions": json.loads(row["conditions"]),
                    "resolution": ("unresolved" if not definitions["total"] else
                                   "ambiguous" if definitions["total"] > 1 else "candidate"),
                    "candidates": candidates,
                    "candidates_total": definitions["total"],
                    "candidates_truncated": definitions["total"] > len(candidates),
                    "candidate_limit": CANDIDATE_LIMIT,
                }


def structure_relationships(conn: sqlite3.Connection, target: Target, *,
                            outgoing: bool = True, incoming: bool = True,
                            limit: int = 100) -> dict:
    """Find bounded explicit declaration relationships for one exact type.

    Candidate definitions retain their source locations. Even a unique name
    match is a candidate, because this index does not preprocess header scopes.
    No source tree access, writes or additional index storage is needed.
    """
    if target.symbol_kind not in {"struct", "union"} or target.kind != "symbol":
        raise ValueError("target is not a struct or union definition")
    if not 1 <= limit <= 1000:
        raise ValueError("relation limit must be between 1 and 1000")
    evidence = _Evidence(conn)
    result = {
        "outgoing": [], "incoming": [], "outgoing_truncated": False,
        "incoming_truncated": False, "limit": limit,
        "candidate_limit": CANDIDATE_LIMIT,
        "outgoing_requested": outgoing, "incoming_requested": incoming,
        "limits": [
            "Relationships describe explicit declarations, not field accesses or runtime data flow.",
            "Definitions are candidates; header visibility and preprocessor alternatives are unverified.",
            "Cross-file candidates use indexed headers; C-source includes and block-scoped shadowing "
            "are not resolved.",
            "Only indexed definitions and retained signatures participate; macros, truncated or malformed "
            "declarations, indirect typedef chains and inline anonymous definitions may be omitted.",
        ],
    }

    def collect(direction, rows, target_id=None, spellings=None):
        for relation in evidence.relations(rows, target_id=target_id, spellings=spellings):
            if len(result[direction]) == limit:
                result[direction + "_truncated"] = True
                break
            result[direction].append(relation)

    if outgoing:
        rows = conn.execute(_MEMBERS + " WHERE s.id=? ORDER BY m.ordinal", (target.id,))
        collect("outgoing", rows)
    if incoming:
        aliases = {row[0] for row in conn.execute(
            "SELECT name FROM type_aliases WHERE symbol_id=?", (target.id,),
        )}
        names = {target.name, *aliases}
        spellings = {(None, alias) for alias in aliases}
        if not target.is_anonymous:
            spellings.add((target.symbol_kind, target.name))
        # SQLite substring filtering cheaply narrows the candidates. The AST
        # pass supplies exact tokens and type positions, avoiding LIKE escapes
        # and false matches on member/function names or larger identifiers.
        member_filter = " OR ".join("instr(m.declaration,?)>0" for _ in names)
        function_filter = " OR ".join("instr(s.signature,?)>0" for _ in names)
        members = conn.execute(
            _MEMBERS + f" WHERE ({member_filter}) ORDER BY f.path,m.start_line,m.ordinal",
            tuple(sorted(names)),
        )
        functions = conn.execute(
            _FUNCTIONS + " WHERE s.kind IN ('function','prototype','syscall') "
            f"AND ({function_filter}) ORDER BY f.path,s.start_line,s.id", tuple(sorted(names)),
        )
        # One shared cap covers both aggregate and function uses.
        from itertools import chain
        collect("incoming", chain(members, functions), target.id, spellings)
    return result
