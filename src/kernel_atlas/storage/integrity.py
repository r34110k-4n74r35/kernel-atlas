"""Deep audits of stored values, source topology, and ownership rollups.

These checks scan completed index contents. Interactive schema validation
calls them only when an explicit deep audit is requested.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schema import TYPE_ALIAS_KINDS, SchemaError

def _validate_row_domains(conn: sqlite3.Connection) -> None:
    """Reject SQLite values outside the logical domains of the schema.

    SQLite column affinity is intentionally permissive: a hand-built or
    damaged database can put a BLOB in a ``TEXT`` column, despite the declared
    type.  Query and rendering code is entitled to rely on these identities,
    counters, and flags after an explicit deep check, so validate the stored
    value classes as well as the table layout.
    """
    text_or_null = lambda name: (  # noqa: E731 - keeps predicates readable
        f"({name} IS NULL OR (typeof({name})='text'"
        f" AND instr({name},char(0))=0))")
    integer_id = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>0)")
    nonnegative_id = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>=0)")
    nonnegative = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name}>=0)")
    integer = lambda name: f"(typeof({name})='integer')"  # noqa: E731
    boolean = lambda name: (  # noqa: E731
        f"(typeof({name})='integer' AND {name} IN (0,1))")
    clean_text = lambda name, nonempty=True: (  # noqa: E731
        f"(typeof({name})='text'"
        + (f" AND {name}!=''" if nonempty else "")
        + f" AND instr({name},char(0))=0)")

    status_values = (
        "'parsed','indexed','binary','symlink','skipped_binary',"
        "'skipped_oversize','read_error','parse_error'"
    )
    symbol_values = (
        "'function','syscall','struct','union','enum','typedef','macro',"
        "'variable','prototype'"
    )
    resolution_values = (
        "'same_file','included_source','unique_global','ambiguous','macro',"
        "'indirect','unresolved'"
    )
    member_kind_values = (
        "'field','function_pointer','struct','union','struct_group',"
        "'unnamed_bitfield','macro'"
    )
    subsystem_lists = (
        "maintainers", "reviewers", "lists", "trees", "websites",
        "patchwork", "bugs", "chats", "profiles", "keywords",
    )
    checks = {
        "dirs": " AND ".join((
            integer_id("id"), clean_text("path", nonempty=False),
            clean_text("name"),
            "(parent_id IS NULL OR " + integer_id("parent_id") + ")",
            nonnegative("depth"), nonnegative("n_files"),
            nonnegative("n_subdirs"), nonnegative("n_files_recursive"),
        )),
        "files": " AND ".join((
            integer_id("id"), clean_text("path"), integer_id("dir_id"),
            clean_text("name"), clean_text("ext", nonempty=False),
            nonnegative("size"),
            nonnegative("lines"), nonnegative("n_symbols"),
            boolean("is_symlink"), text_or_null("link_target"),
            f"(typeof(index_status)='text' AND index_status IN ({status_values}))",
            text_or_null("index_error"), clean_text("call_domain"),
        )),
        "symbols": " AND ".join((
            integer_id("id"), integer_id("file_id"), clean_text("name"),
            f"(typeof(kind)='text' AND kind IN ({symbol_values}))",
            "(typeof(start_line)='integer' AND start_line>=1)",
            "(typeof(end_line)='integer' AND end_line>=start_line)",
            text_or_null("signature"), text_or_null("summary"),
            text_or_null("description"), boolean("is_static"),
            boolean("is_inline"), boolean("is_exported"),
            boolean("is_anonymous"), boolean("parse_complete"),
            clean_text("parse_warnings", nonempty=False),
            clean_text("unmatched_member_docs", nonempty=False),
            clean_text("conditions", nonempty=False),
        )),
        "type_aliases": " AND ".join((
            integer_id("symbol_id"), clean_text("name"),
        )),
        "type_members": " AND ".join((
            integer_id("id"), integer_id("symbol_id"),
            "(parent_id IS NULL OR " + integer_id("parent_id") + ")",
            nonnegative("ordinal"),
            "(name IS NULL OR " + clean_text("name") + ")",
            f"(typeof(kind)='text' AND kind IN ({member_kind_values}))",
            text_or_null("type_text"), clean_text("declaration"),
            "(typeof(start_line)='integer' AND start_line>=1)",
            "(typeof(end_line)='integer' AND end_line>=start_line)",
            text_or_null("bit_width"), clean_text("array_dimensions", False),
            text_or_null("description"),
            "(description_source IS NULL OR (typeof(description_source)='text'"
            " AND description_source IN ('kernel-doc','inline-kernel-doc',"
            "'source-comment','macro-semantics'))) ",
            "((description IS NULL)=(description_source IS NULL))",
            clean_text("conditions", False),
            "(typeof(visibility)='text' AND visibility IN "
            "('unspecified','public','private'))",
            boolean("is_anonymous"), text_or_null("generated_by"),
        )),
        "subsystems": " AND ".join((
            nonnegative_id("id"), clean_text("name"), text_or_null("status"),
            *(text_or_null(name) for name in subsystem_lists),
            nonnegative("n_files"), nonnegative("n_primary_files"),
            "n_primary_files<=n_files",
        )),
        "path_subsys": " AND ".join((
            "(typeof(ref_kind)='text' AND ref_kind='file')",
            integer_id("ref_id"), nonnegative_id("subsystem_id"),
            integer("score"), nonnegative("rank"),
            boolean("is_primary"),
        )),
        "dir_subsys": " AND ".join((
            integer_id("dir_id"), nonnegative_id("subsystem_id"),
            nonnegative("n_claimed"), nonnegative("n_primary"),
            "n_primary<=n_claimed",
            "(typeof(coverage) IN ('integer','real')"
            " AND coverage>=0.0 AND coverage<=1.0)",
            nonnegative("rank"),
        )),
        "source_includes": " AND ".join((
            integer_id("includer_id"), integer_id("included_id"),
            "(typeof(line)='integer' AND line>=1)",
        )),
        "translation_unit_roots": integer_id("file_id"),
        "calls": " AND ".join((
            integer_id("caller_id"), clean_text("callee"),
            "(callee_id IS NULL OR " + integer_id("callee_id") + ")",
            f"(typeof(resolution)='text'"
            f" AND resolution IN ({resolution_values}))",
            nonnegative("direct_count"), nonnegative("indirect_count"),
            nonnegative("macro_count"),
            "direct_count+indirect_count+macro_count>0",
        )),
    }
    for table, predicate in checks.items():
        bad = conn.execute(
            f"SELECT rowid FROM {table} "
            f"WHERE NOT COALESCE(({predicate}),0) LIMIT 1"
        ).fetchone()
        if bad is not None:
            raise SchemaError(
                f"index table {table} contains an invalid value at row "
                f"{bad[0]}")

    for row in conn.execute(
            "SELECT id," + ",".join(subsystem_lists) + " FROM subsystems"):
        for field in subsystem_lists:
            if row[field] is None:
                continue
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SchemaError(
                    f"index subsystem {row['id']} has invalid {field} metadata"
                ) from exc
            if not isinstance(value, list) \
                    or any(not isinstance(item, str) for item in value):
                raise SchemaError(
                    f"index subsystem {row['id']} has invalid {field} metadata")

    for row in conn.execute(
            "SELECT id,parse_complete,parse_warnings,unmatched_member_docs,conditions"
            " FROM symbols WHERE parse_complete!=1 OR parse_warnings!='[]'"
            " OR unmatched_member_docs!='{}' OR conditions!='[]'"):
        try:
            warnings = json.loads(row["parse_warnings"])
            unmatched = json.loads(row["unmatched_member_docs"])
            conditions = json.loads(row["conditions"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaError(
                f"index symbol {row['id']} has invalid structure metadata") from exc
        if not isinstance(warnings, list) or any(
                not isinstance(value, str) for value in warnings):
            raise SchemaError(
                f"index symbol {row['id']} has invalid parse warnings")
        if bool(row["parse_complete"]) != (len(warnings) == 0):
            raise SchemaError(
                f"index symbol {row['id']} has inconsistent parse completeness")
        if not isinstance(unmatched, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in unmatched.items()):
            raise SchemaError(
                f"index symbol {row['id']} has invalid unmatched member docs")
        if not isinstance(conditions, list) or any(
                not isinstance(value, str) for value in conditions):
            raise SchemaError(
                f"index symbol {row['id']} has invalid conditions")

    for row in conn.execute(
            "SELECT id,array_dimensions,conditions FROM type_members"
            " WHERE array_dimensions!='[]' OR conditions!='[]'"):
        for field in ("array_dimensions", "conditions"):
            try:
                value = json.loads(row[field])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SchemaError(
                    f"index member {row['id']} has invalid {field}") from exc
            if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value):
                raise SchemaError(
                    f"index member {row['id']} has invalid {field}")


def _valid_index_path(path: str, *, root: bool = False) -> bool:
    """Whether a stored source identity is normalized relative POSIX text."""
    if root and path == "":
        return True
    if not path or path.startswith("/") or path.endswith("/") \
            or "\\" in path or "\0" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def validate_structure(conn: sqlite3.Connection,
                             meta: dict[str, str]) -> None:
    """Audit row counts, path topology, parse state, and ownership rollups."""
    _validate_row_domains(conn)
    table_counts = {
        "n_dirs": "dirs", "n_files": "files", "n_symbols": "symbols",
        "n_type_aliases": "type_aliases",
        "n_type_members": "type_members",
        "n_subsystems": "subsystems", "n_calls": "calls",
    }
    for key, table in table_counts.items():
        actual = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if actual != int(meta[key]):
            raise SchemaError(
                f"index {table} row count disagrees with {key} metadata")
    actual_occurrences = int(conn.execute(
        "SELECT COALESCE(SUM(direct_count+indirect_count+macro_count),0)"
        " FROM calls").fetchone()[0])
    if actual_occurrences != int(meta["n_call_occurrences"]):
        raise SchemaError(
            "index call occurrence count disagrees with metadata")

    foreign_error = next(iter(conn.execute("PRAGMA foreign_key_check")), None)
    if foreign_error is not None:
        raise SchemaError(
            f"index has a dangling reference in table {foreign_error[0]!r}")

    # Do not assume a third-party database retained the declared FK/UNIQUE
    # constraints merely because it advertises the current column layout.
    reference_checks = (
        ("directory parent", "SELECT 1 FROM dirs child LEFT JOIN dirs parent"
         " ON parent.id=child.parent_id WHERE child.parent_id IS NOT NULL"
         " AND parent.id IS NULL LIMIT 1"),
        ("file directory", "SELECT 1 FROM files f LEFT JOIN dirs d"
         " ON d.id=f.dir_id WHERE d.id IS NULL LIMIT 1"),
        ("symbol file", "SELECT 1 FROM symbols s LEFT JOIN files f"
         " ON f.id=s.file_id WHERE f.id IS NULL LIMIT 1"),
        ("type alias", "SELECT 1 FROM type_aliases a LEFT JOIN symbols s"
         " ON s.id=a.symbol_id WHERE s.id IS NULL LIMIT 1"),
        ("type member", "SELECT 1 FROM type_members m LEFT JOIN symbols s"
         " ON s.id=m.symbol_id LEFT JOIN type_members p ON p.id=m.parent_id"
         " WHERE s.id IS NULL OR (m.parent_id IS NOT NULL AND p.id IS NULL)"
         " LIMIT 1"),
        ("file ownership", "SELECT 1 FROM path_subsys p LEFT JOIN files f"
         " ON f.id=p.ref_id LEFT JOIN subsystems s ON s.id=p.subsystem_id"
         " WHERE f.id IS NULL OR s.id IS NULL LIMIT 1"),
        ("directory ownership", "SELECT 1 FROM dir_subsys p LEFT JOIN dirs d"
         " ON d.id=p.dir_id LEFT JOIN subsystems s ON s.id=p.subsystem_id"
         " WHERE d.id IS NULL OR s.id IS NULL LIMIT 1"),
        ("source inclusion", "SELECT 1 FROM source_includes edge"
         " LEFT JOIN files a ON a.id=edge.includer_id"
         " LEFT JOIN files b ON b.id=edge.included_id"
         " WHERE a.id IS NULL OR b.id IS NULL LIMIT 1"),
        ("translation-unit root", "SELECT 1 FROM translation_unit_roots root"
         " LEFT JOIN files f ON f.id=root.file_id"
         " WHERE f.id IS NULL LIMIT 1"),
        ("call", "SELECT 1 FROM calls c LEFT JOIN symbols caller"
         " ON caller.id=c.caller_id LEFT JOIN symbols target"
         " ON target.id=c.callee_id WHERE caller.id IS NULL"
         " OR (c.callee_id IS NOT NULL AND target.id IS NULL) LIMIT 1"),
    )
    for label, sql in reference_checks:
        if conn.execute(sql).fetchone() is not None:
            raise SchemaError(f"index has a dangling {label} reference")

    identity_checks = (
        ("directory", "dirs", "id"), ("directory path", "dirs", "path"),
        ("file", "files", "id"), ("file path", "files", "path"),
        ("symbol", "symbols", "id"),
        ("type alias", "type_aliases", "symbol_id,name"),
        ("type member", "type_members", "id"),
        ("type member ordinal", "type_members", "symbol_id,ordinal"),
        ("subsystem", "subsystems", "id"),
        ("subsystem name", "subsystems", "name"),
        ("file ownership", "path_subsys", "ref_kind,ref_id,subsystem_id"),
        ("directory ownership", "dir_subsys", "dir_id,subsystem_id"),
        ("source inclusion", "source_includes", "includer_id,included_id"),
        ("translation-unit root", "translation_unit_roots", "file_id"),
        ("call", "calls", "caller_id,callee"),
    )
    for label, table, columns in identity_checks:
        duplicate = conn.execute(
            f"SELECT 1 FROM {table} GROUP BY {columns}"
            " HAVING COUNT(*)>1 LIMIT 1").fetchone()
        if duplicate is not None:
            raise SchemaError(f"index contains a duplicate {label} identity")

    dirs = conn.execute(
        "SELECT id,path,parent_id,name,depth,n_files,n_subdirs,"
        " n_files_recursive FROM dirs").fetchall()
    roots = [row for row in dirs if row["path"] == ""]
    if len(roots) != 1 or roots[0]["parent_id"] is not None \
            or roots[0]["depth"] != 0:
        raise SchemaError("index must contain exactly one valid kernel root")
    by_dir_id = {row["id"]: row for row in dirs}
    by_dir_path = {row["path"]: row for row in dirs}
    if len(by_dir_id) != len(dirs) or len(by_dir_path) != len(dirs):
        raise SchemaError("index contains duplicate directory identities")

    actual_files = {row["id"]: 0 for row in dirs}
    actual_subdirs = {row["id"]: 0 for row in dirs}
    actual_recursive = {row["id"]: 0 for row in dirs}
    for row in dirs:
        if not _valid_index_path(row["path"], root=True):
            raise SchemaError(
                f"index has an unsafe directory identity {row['path']!r}")
        if not row["path"]:
            continue
        expected_parent = row["path"].rpartition("/")[0]
        parent = by_dir_id.get(row["parent_id"])
        if (parent is None or parent["path"] != expected_parent
                or row["name"] != row["path"].rsplit("/", 1)[-1]
                or row["depth"] != row["path"].count("/") + 1):
            raise SchemaError(
                f"index has an inconsistent directory identity {row['path']!r}")
        actual_subdirs[parent["id"]] += 1

    allowed_status = {
        "parsed", "indexed", "binary", "symlink", "skipped_binary",
        "skipped_oversize", "read_error", "parse_error",
    }
    files = conn.execute(
        "SELECT id,path,dir_id,name,ext,lines,n_symbols,is_symlink,link_target,"
        " index_status,index_error FROM files"
    ).fetchall()
    failed = skipped = oversized = symlinks = 0
    for row in files:
        if not _valid_index_path(row["path"]):
            raise SchemaError(
                f"index has an unsafe file identity {row['path']!r}")
        parent = by_dir_id.get(row["dir_id"])
        expected_parent = row["path"].rpartition("/")[0]
        if (parent is None or parent["path"] != expected_parent
                or row["name"] != row["path"].rsplit("/", 1)[-1]):
            raise SchemaError(
                f"index has an inconsistent file identity {row['path']!r}")
        expected_ext = Path(row["name"]).suffix.lower()
        if row["ext"] != expected_ext:
            raise SchemaError(
                f"index file {row['path']!r} has inconsistent extension metadata")
        if row["index_status"] not in allowed_status:
            raise SchemaError(
                f"index file {row['path']!r} has invalid or unfinished status")
        if row["is_symlink"] not in (0, 1) \
                or bool(row["is_symlink"]) != (row["index_status"] == "symlink"):
            raise SchemaError(
                f"index file {row['path']!r} has inconsistent symlink state")
        if not row["is_symlink"] and row["link_target"] is not None:
            raise SchemaError(
                f"index file {row['path']!r} has an unexpected link target")
        parse_ext = row["ext"] in {".c", ".h", ".c_shipped", ".h_shipped"}
        valid_states = ({"parsed", "skipped_binary", "skipped_oversize",
                         "read_error", "parse_error"} if parse_ext else
                        {"indexed", "binary", "read_error"})
        if not row["is_symlink"] and row["index_status"] not in valid_states:
            raise SchemaError(
                f"index file {row['path']!r} has a status incompatible with"
                " its extension")
        if row["index_status"] in {"read_error", "parse_error"}:
            if not row["index_error"]:
                raise SchemaError(
                    f"index file {row['path']!r} is missing its parse error")
        elif not row["is_symlink"] and row["index_error"] is not None:
            raise SchemaError(
                f"index file {row['path']!r} has an unexpected parse error")
        actual_files[parent["id"]] += 1
        current = parent
        seen: set[int] = set()
        while current is not None:
            if current["id"] in seen:
                raise SchemaError("index directory parent graph contains a cycle")
            seen.add(current["id"])
            actual_recursive[current["id"]] += 1
            current = by_dir_id.get(current["parent_id"])
        failed += row["index_status"] in {"read_error", "parse_error"}
        oversized += row["index_status"] == "skipped_oversize"
        skipped += row["index_status"] in {"skipped_binary", "skipped_oversize"}
        if row["is_symlink"]:
            symlinks += 1
            if Path(row["name"]).suffix.lower() in {
                    ".c", ".h", ".c_shipped", ".h_shipped"}:
                skipped += 1

    for row in dirs:
        did = row["id"]
        if (row["n_files"] != actual_files[did]
                or row["n_subdirs"] != actual_subdirs[did]
                or row["n_files_recursive"] != actual_recursive[did]):
            raise SchemaError(
                f"index directory rollup is inconsistent for {row['path']!r}")

    actual_symbol_counts = {
        row["file_id"]: int(row["n"])
        for row in conn.execute(
            "SELECT file_id,COUNT(*) AS n FROM symbols GROUP BY file_id")
    }
    for row in files:
        if row["n_symbols"] != actual_symbol_counts.get(row["id"], 0):
            raise SchemaError(
                f"index symbol rollup is inconsistent for {row['path']!r}")
    bad_symbol_line = conn.execute(
        "SELECT f.path FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE s.end_line>f.lines"
        " OR f.ext NOT IN ('.c','.h','.c_shipped','.h_shipped')"
        " OR f.index_status!='parsed' LIMIT 1"
    ).fetchone()
    if bad_symbol_line is not None:
        raise SchemaError(
            f"index symbol identity is incompatible with file metadata for "
            f"{bad_symbol_line['path']!r}")

    alias_kind_params = ",".join("?" for _ in TYPE_ALIAS_KINDS)
    bad_alias = conn.execute(
        "SELECT a.name AS alias,s.kind,s.name,f.path"
        " FROM type_aliases a JOIN symbols s"
        " ON s.id=a.symbol_id"
        " JOIN files f ON f.id=s.file_id"
        f" WHERE s.kind NOT IN ({alias_kind_params}) LIMIT 1",
        TYPE_ALIAS_KINDS,
    ).fetchone()
    if bad_alias is not None:
        raise SchemaError(
            f"index has type alias {bad_alias['alias']!r} attached to "
            f"unsupported symbol kind {bad_alias['kind']!r} at "
            f"{bad_alias['path']}:{bad_alias['name']}")
    bad_member = conn.execute(
        "SELECT m.id FROM type_members m JOIN symbols s ON s.id=m.symbol_id"
        " LEFT JOIN type_members p ON p.id=m.parent_id"
        " WHERE s.kind NOT IN ('struct','union')"
        " OR m.start_line<s.start_line OR m.end_line>s.end_line"
        " OR (m.parent_id IS NOT NULL AND (p.symbol_id!=m.symbol_id"
        " OR p.ordinal>=m.ordinal OR p.kind NOT IN "
        " ('struct','union','struct_group','macro')"
        " OR m.start_line<p.start_line OR m.end_line>p.end_line)) LIMIT 1"
    ).fetchone()
    if bad_member is not None:
        raise SchemaError("index has an inconsistent aggregate-member identity")
    bad_member_order = conn.execute(
        "SELECT symbol_id FROM type_members GROUP BY symbol_id"
        " HAVING MIN(ordinal)!=0 OR MAX(ordinal)!=COUNT(*)-1"
        " OR COUNT(DISTINCT ordinal)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_member_order is not None:
        raise SchemaError("index aggregate-member ordinals are not contiguous")

    # Parent ids form a preorder forest.  Once traversal leaves a container's
    # subtree it may never re-enter it; otherwise query reconstruction moves a
    # later root underneath an earlier field despite contiguous ordinals.
    current_symbol_id: int | None = None
    parents: dict[int, int | None] = {}
    previous_chain: list[int] = []
    closed: set[int] = set()
    for row in conn.execute(
            "SELECT symbol_id,id,parent_id FROM type_members"
            " ORDER BY symbol_id,ordinal"):
        if row["symbol_id"] != current_symbol_id:
            current_symbol_id = row["symbol_id"]
            parents.clear()
            previous_chain.clear()
            closed.clear()
        chain: list[int] = []
        current = row["parent_id"]
        seen: set[int] = set()
        while current is not None:
            if current in seen or current not in parents:
                raise SchemaError(
                    "index has an inconsistent aggregate-member hierarchy")
            seen.add(current)
            chain.append(current)
            current = parents[current]
        chain.reverse()
        if any(ancestor in closed for ancestor in chain):
            raise SchemaError(
                "index aggregate-member preorder is not contiguous")
        common = 0
        while common < min(len(previous_chain), len(chain)) \
                and previous_chain[common] == chain[common]:
            common += 1
        closed.update(previous_chain[common:])
        parents[row["id"]] = row["parent_id"]
        previous_chain = [*chain, row["id"]]

    recorded_states = {
        "n_parse_skipped": skipped, "n_parse_failed": failed,
        "n_oversize": oversized, "n_symlinks": symlinks,
    }
    for key, actual in recorded_states.items():
        if actual != int(meta[key]):
            raise SchemaError(f"index file states disagree with {key} metadata")

    bad_primary = conn.execute(
        "SELECT ref_id FROM ("
        " SELECT p.*,MAX(score) OVER (PARTITION BY ref_id) AS max_score"
        " FROM path_subsys p)"
        " WHERE is_primary != (score=max_score) LIMIT 1"
    ).fetchone()
    if bad_primary is not None:
        raise SchemaError("index has inconsistent co-primary ownership evidence")
    bad_rank = conn.execute(
        "SELECT ref_id FROM path_subsys GROUP BY ref_id"
        " HAVING MIN(rank)!=0 OR MAX(rank)!=COUNT(*)-1"
        " OR COUNT(DISTINCT rank)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_rank is not None:
        raise SchemaError("index has inconsistent file-ownership ranks")
    bad_rank_order = conn.execute(
        "SELECT ref_id FROM ("
        " SELECT p.ref_id,p.rank,ROW_NUMBER() OVER ("
        " PARTITION BY p.ref_id ORDER BY p.score DESC,s.name,s.id)-1 expected"
        " FROM path_subsys p JOIN subsystems s ON s.id=p.subsystem_id)"
        " WHERE rank!=expected LIMIT 1"
    ).fetchone()
    if bad_rank_order is not None:
        raise SchemaError("index file-ownership ranks disagree with evidence")

    bad_subsystem = conn.execute(
        "SELECT s.name FROM subsystems s"
        " LEFT JOIN (SELECT subsystem_id,COUNT(*) AS claimed,"
        " SUM(is_primary) AS primary_n FROM path_subsys GROUP BY subsystem_id) p"
        " ON p.subsystem_id=s.id"
        " WHERE s.n_files!=COALESCE(p.claimed,0)"
        " OR s.n_primary_files!=COALESCE(p.primary_n,0) LIMIT 1"
    ).fetchone()
    if bad_subsystem is not None:
        raise SchemaError(
            f"index subsystem rollup is inconsistent for {bad_subsystem['name']!r}")

    bad_directory = conn.execute(
        "SELECT d.path FROM dir_subsys p JOIN dirs d ON d.id=p.dir_id"
        " WHERE p.n_claimed<0 OR p.n_primary<0 OR p.n_primary>p.n_claimed"
        " OR p.n_claimed>d.n_files_recursive"
        " OR ABS(p.coverage-CASE WHEN d.n_files_recursive=0 THEN 0.0"
        "   ELSE 1.0*p.n_primary/d.n_files_recursive END)>1e-12 LIMIT 1"
    ).fetchone()
    if bad_directory is not None:
        raise SchemaError(
            f"index directory ownership is inconsistent for "
            f"{bad_directory['path']!r}")
    bad_directory_rank = conn.execute(
        "SELECT dir_id FROM dir_subsys GROUP BY dir_id"
        " HAVING MIN(rank)!=0 OR MAX(rank)!=COUNT(*)-1"
        " OR COUNT(DISTINCT rank)!=COUNT(*) LIMIT 1"
    ).fetchone()
    if bad_directory_rank is not None:
        raise SchemaError("index has inconsistent directory-ownership ranks")
    bad_directory_order = conn.execute(
        "SELECT dir_id FROM ("
        " SELECT p.dir_id,p.rank,ROW_NUMBER() OVER ("
        " PARTITION BY p.dir_id ORDER BY (s.name='THE REST'),"
        " p.n_primary DESC,p.n_claimed DESC,s.name,s.id)-1 expected"
        " FROM dir_subsys p JOIN subsystems s ON s.id=p.subsystem_id)"
        " WHERE rank!=expected LIMIT 1"
    ).fetchone()
    if bad_directory_order is not None:
        raise SchemaError(
            "index directory-ownership ranks disagree with composition")

    # Recompute every directory/subsystem aggregate from file claims.  This is
    # the central evidence used by info, listings, and relationship queries.
    bad_rollup = conn.execute(
        "WITH RECURSIVE ancestry(file_id,dir_id) AS ("
        " SELECT id,dir_id FROM files UNION ALL"
        " SELECT a.file_id,d.parent_id FROM ancestry a"
        " JOIN dirs d ON d.id=a.dir_id WHERE d.parent_id IS NOT NULL),"
        " actual AS (SELECT a.dir_id,p.subsystem_id,COUNT(*) AS claimed,"
        " SUM(p.is_primary) AS primary_n FROM ancestry a"
        " JOIN path_subsys p ON p.ref_id=a.file_id"
        " GROUP BY a.dir_id,p.subsystem_id),"
        " mismatch AS ("
        " SELECT a.dir_id FROM actual a LEFT JOIN dir_subsys d"
        " ON d.dir_id=a.dir_id AND d.subsystem_id=a.subsystem_id"
        " WHERE d.dir_id IS NULL OR d.n_claimed!=a.claimed"
        " OR d.n_primary!=a.primary_n"
        " UNION ALL"
        " SELECT d.dir_id FROM dir_subsys d LEFT JOIN actual a"
        " ON a.dir_id=d.dir_id AND a.subsystem_id=d.subsystem_id"
        " WHERE a.dir_id IS NULL) SELECT dir_id FROM mismatch LIMIT 1"
    ).fetchone()
    if bad_rollup is not None:
        raise SchemaError("index directory ownership rollups disagree with files")

    bad_include = conn.execute(
        "SELECT edge.includer_id FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE edge.includer_id=edge.included_id OR edge.line<1"
        " OR edge.line>parent.lines"
        " OR parent.ext NOT IN ('.c','.c_shipped')"
        " OR member.ext NOT IN ('.c','.c_shipped')"
        " OR parent.index_status IS NOT 'parsed'"
        " OR member.index_status IS NOT 'parsed' LIMIT 1"
    ).fetchone()
    if bad_include is not None:
        raise SchemaError("index has an invalid C-source inclusion edge")
    bad_unit_root = conn.execute(
        "SELECT root.file_id FROM translation_unit_roots root"
        " JOIN files f ON f.id=root.file_id"
        " WHERE f.ext NOT IN ('.c','.c_shipped')"
        " OR f.index_status!='parsed' LIMIT 1"
    ).fetchone()
    if bad_unit_root is not None:
        raise SchemaError("index has an invalid translation-unit root")
    include_cycle = conn.execute(
        "WITH RECURSIVE reach(origin,current) AS ("
        " SELECT includer_id,included_id FROM source_includes UNION"
        " SELECT reach.origin,edge.included_id FROM reach"
        " JOIN source_includes edge ON edge.includer_id=reach.current)"
        " SELECT origin FROM reach WHERE origin=current LIMIT 1"
    ).fetchone()
    if include_cycle is not None:
        raise SchemaError("index C-source inclusion graph contains a cycle")
