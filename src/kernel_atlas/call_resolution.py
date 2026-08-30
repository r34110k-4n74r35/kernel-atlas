"""Conservative, translation-unit-aware call identity evidence.

The parser records one invocation row for source text, but a quoted ``.c``
member can be instantiated by several top-level translation units.  A call is
therefore concrete only when every containing unit reaches the same outcome.
This module builds the evidence once for both indexing and deep validation so
the validator cannot drift from the resolver's semantics.
"""

from __future__ import annotations

import sqlite3


_TABLES = (
    "expected_call_outcomes",
    "unit_call_outcomes",
    "resolution_names",
    "blocker_resolution_names",
    "global_resolution_names",
    "call_names",
    "arch_blockers",
    "eligible_blocker_domains",
    "eligible_domains",
    "domain_shared_variables",
    "domain_variables",
    "domain_macros",
    "domain_headers",
    "domain_globals",
    "unit_local_bindings",
    "unit_local_callables",
    "call_contexts",
    "call_sites",
    "effective_file_domains",
    "translation_unit_members",
    "program_header_domains",
    "program_roots",
    "file_domains",
)


def drop_evidence(conn: sqlite3.Connection) -> None:
    """Remove temporary resolver evidence from ``conn`` if it exists."""
    for table in _TABLES:
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")


def prepare_evidence(conn: sqlite3.Connection, *, validating: bool = False) -> None:
    """Build expected outcomes for unresolved build rows or completed rows.

    Only edges with direct occurrences need contextual resolution. Parser
    classifications for aggregate members, dereferences, and active in-file
    macros are persisted as occurrence counts and validated separately.
    """
    drop_evidence(conn)
    call_filter = "c.direct_count>0"
    conn.executescript(
        f"""
        CREATE TEMP TABLE file_domains AS
        SELECT f.id AS file_id,
               CASE
                 WHEN f.call_domain != 'kernel' THEN f.call_domain
                 WHEN f.path LIKE 'tools/%'
                   OR f.path LIKE 'scripts/%'
                   OR f.path LIKE 'Documentation/%'
                   OR f.path LIKE '%/tools/%'
                   OR f.path LIKE '%/Documentation/%'
                   OR f.path LIKE 'arch/um/os-Linux/%' THEN
                   'isolated:' || f.path
                 WHEN EXISTS (
                   SELECT 1 FROM symbols entry
                   WHERE entry.file_id=f.id AND entry.kind='function'
                     AND entry.name='main'
                 ) THEN 'isolated:' || f.path
                 WHEN f.path LIKE 'arch/%/%' THEN
                   'arch:' || substr(substr(f.path, 6), 1,
                                    instr(substr(f.path, 6), '/') - 1)
                 ELSE 'kernel'
               END AS domain
        FROM files f;
        CREATE UNIQUE INDEX temp.idx_file_domains_file
          ON file_domains(file_id);

        -- Kbuild object lists omit headers.  Header identities below a
        -- program's declaring directory remain conservative blockers.
        CREATE TEMP TABLE program_roots AS
        SELECT domain,
               substr(tail, 1, instr(tail, ':') - 1) AS root
        FROM (
          SELECT DISTINCT domain, substr(domain, 9) AS tail
          FROM file_domains
          WHERE domain LIKE 'program:%'
            AND instr(substr(domain, 9), ':') > 0
        );
        CREATE UNIQUE INDEX temp.idx_program_roots_domain
          ON program_roots(domain);
        CREATE TEMP TABLE program_header_domains AS
        SELECT roots.domain, f.id AS file_id
        FROM program_roots roots
        JOIN files f ON f.ext IN ('.h','.h_shipped') AND (
          roots.root='' OR
          substr(f.path, 1, length(roots.root) + 1)=roots.root || '/'
        )
        GROUP BY roots.domain, f.id;
        CREATE UNIQUE INDEX temp.idx_program_header_domains
          ON program_header_domains(domain, file_id);

        -- Only top-level sources seed a translation unit.  An included-only
        -- member does not gain an artificial standalone context.  UNION makes
        -- diamonds idempotent and terminates recursive traversal.
        CREATE TEMP TABLE translation_unit_members AS
        WITH RECURSIVE unit_members(unit_id, member_file_id) AS (
          SELECT f.id, f.id FROM files f
          WHERE NOT EXISTS (
                  SELECT 1 FROM source_includes edge
                  WHERE edge.included_id=f.id
                )
             OR EXISTS (
                  SELECT 1 FROM translation_unit_roots root
                  WHERE root.file_id=f.id
                )
          UNION
          SELECT units.unit_id, edge.included_id
          FROM unit_members units
          JOIN source_includes edge
            ON edge.includer_id=units.member_file_id
        )
        SELECT unit_id, member_file_id FROM unit_members
        GROUP BY unit_id, member_file_id;
        CREATE UNIQUE INDEX temp.idx_translation_unit_members_unit
          ON translation_unit_members(unit_id, member_file_id);
        CREATE INDEX temp.idx_translation_unit_members_member
          ON translation_unit_members(member_file_id, unit_id);

        -- Definitions in an included member are emitted by the root object.
        -- Map their source identity through every effective root domain, while
        -- deduplicating diamonds and repeated roots in the same domain.
        CREATE TEMP TABLE effective_file_domains AS
        SELECT members.member_file_id AS file_id, fd.domain
        FROM translation_unit_members members
        JOIN file_domains fd ON fd.file_id=members.unit_id
        GROUP BY members.member_file_id, fd.domain;
        CREATE UNIQUE INDEX temp.idx_effective_file_domains
          ON effective_file_domains(file_id, domain);

        -- Work only on names that actually occur at a call site; joining every
        -- unit to every one of millions of symbols is unnecessary.
        CREATE TEMP TABLE call_sites AS
        SELECT caller.file_id AS caller_file_id, c.callee AS name
        FROM calls c
        JOIN symbols caller ON caller.id=c.caller_id
        WHERE {call_filter}
        GROUP BY caller.file_id, c.callee;
        CREATE UNIQUE INDEX temp.idx_call_sites_file_name
          ON call_sites(caller_file_id, name);

        CREATE TEMP TABLE call_contexts AS
        SELECT sites.caller_file_id, sites.name, units.unit_id, fd.domain
        FROM call_sites sites
        JOIN translation_unit_members units
          ON units.member_file_id=sites.caller_file_id
        JOIN file_domains fd ON fd.file_id=units.unit_id
        GROUP BY sites.caller_file_id, sites.name, units.unit_id, fd.domain;
        CREATE UNIQUE INDEX temp.idx_call_contexts_identity
          ON call_contexts(caller_file_id, name, unit_id);
        CREATE INDEX temp.idx_call_contexts_domain_name
          ON call_contexts(domain, name);

        CREATE TEMP TABLE unit_local_callables AS
        SELECT contexts.caller_file_id, contexts.name, contexts.unit_id,
               MIN(s.id) AS symbol_id,
               MIN(s.file_id) AS target_file_id,
               COUNT(*) AS n
        FROM call_contexts contexts
        JOIN translation_unit_members members
          ON members.unit_id=contexts.unit_id
        JOIN symbols s
          ON s.file_id=members.member_file_id AND s.name=contexts.name
        WHERE s.kind IN ('function', 'syscall')
        GROUP BY contexts.caller_file_id, contexts.name, contexts.unit_id;
        CREATE UNIQUE INDEX temp.idx_unit_local_callables
          ON unit_local_callables(caller_file_id, name, unit_id);

        CREATE TEMP TABLE unit_local_bindings AS
        SELECT contexts.caller_file_id, contexts.name, contexts.unit_id,
               SUM(s.kind='macro'
                   AND s.file_id!=contexts.caller_file_id) AS macro_n,
               SUM(s.kind='variable') AS variable_n
        FROM call_contexts contexts
        JOIN translation_unit_members members
          ON members.unit_id=contexts.unit_id
        JOIN symbols s
          ON s.file_id=members.member_file_id AND s.name=contexts.name
        WHERE s.kind='variable'
           OR (s.kind='macro' AND s.file_id!=contexts.caller_file_id)
        GROUP BY contexts.caller_file_id, contexts.name, contexts.unit_id;
        CREATE UNIQUE INDEX temp.idx_unit_local_bindings
          ON unit_local_bindings(caller_file_id, name, unit_id);

        CREATE TEMP TABLE domain_globals AS
        SELECT domains.domain, s.name, MIN(s.id) AS symbol_id,
               COUNT(DISTINCT s.id) AS n
        FROM symbols s
        JOIN effective_file_domains domains ON domains.file_id=s.file_id
        WHERE s.kind IN ('function', 'syscall') AND s.is_static=0
        GROUP BY domains.domain, s.name;
        CREATE UNIQUE INDEX temp.idx_domain_globals
          ON domain_globals(domain, name);

        CREATE TEMP TABLE domain_headers AS
        SELECT domain, name, COUNT(*) AS n FROM (
          SELECT fd.domain, s.id AS symbol_id, s.name
          FROM symbols s
          JOIN files f ON f.id=s.file_id
          JOIN file_domains fd ON fd.file_id=s.file_id
          WHERE s.kind IN ('function', 'syscall') AND s.is_static=1
            AND f.ext IN ('.h','.h_shipped')
          UNION
          SELECT visible.domain, s.id AS symbol_id, s.name
          FROM program_header_domains visible
          JOIN symbols s ON s.file_id=visible.file_id
          WHERE s.kind IN ('function', 'syscall') AND s.is_static=1
        ) GROUP BY domain, name;
        CREATE UNIQUE INDEX temp.idx_domain_headers
          ON domain_headers(domain, name);

        CREATE TEMP TABLE domain_macros AS
        SELECT domain, name, COUNT(*) AS n FROM (
          SELECT fd.domain, s.id AS symbol_id, s.name
          FROM symbols s
          JOIN files f ON f.id=s.file_id
          JOIN file_domains fd ON fd.file_id=s.file_id
          WHERE s.kind='macro' AND f.ext IN ('.h','.h_shipped')
          UNION
          SELECT visible.domain, s.id AS symbol_id, s.name
          FROM program_header_domains visible
          JOIN symbols s ON s.file_id=visible.file_id
          WHERE s.kind='macro'
        ) GROUP BY domain, name;
        CREATE UNIQUE INDEX temp.idx_domain_macros
          ON domain_macros(domain, name);

        CREATE TEMP TABLE domain_variables AS
        SELECT domain, name, COUNT(*) AS n FROM (
          SELECT domains.domain, s.id AS symbol_id, s.name
          FROM symbols s
          JOIN files f ON f.id=s.file_id
          JOIN effective_file_domains domains ON domains.file_id=s.file_id
          WHERE s.kind='variable' AND (
            s.is_static=0 OR f.ext IN ('.h','.h_shipped'))
          UNION
          SELECT visible.domain, s.id AS symbol_id, s.name
          FROM program_header_domains visible
          JOIN symbols s ON s.file_id=visible.file_id
          WHERE s.kind='variable'
        ) GROUP BY domain, name;
        CREATE UNIQUE INDEX temp.idx_domain_variables
          ON domain_variables(domain, name);

        CREATE TEMP TABLE domain_shared_variables AS
        SELECT domain, name, COUNT(*) AS n FROM (
          SELECT domains.domain, s.id AS symbol_id, s.name
          FROM symbols s
          JOIN files f ON f.id=s.file_id
          JOIN effective_file_domains domains ON domains.file_id=s.file_id
          WHERE s.kind='variable' AND f.ext IN ('.h','.h_shipped')
          UNION
          SELECT visible.domain, s.id AS symbol_id, s.name
          FROM program_header_domains visible
          JOIN symbols s ON s.file_id=visible.file_id
          WHERE s.kind='variable'
        ) GROUP BY domain, name;
        CREATE UNIQUE INDEX temp.idx_domain_shared_variables
          ON domain_shared_variables(domain, name);

        CREATE TEMP TABLE eligible_domains (
          caller_domain TEXT NOT NULL,
          candidate_domain TEXT NOT NULL,
          PRIMARY KEY (caller_domain, candidate_domain)
        );
        INSERT INTO eligible_domains
          SELECT DISTINCT domain, domain FROM file_domains;
        INSERT OR IGNORE INTO eligible_domains
          SELECT DISTINCT domain, 'kernel' FROM file_domains
          WHERE domain LIKE 'arch:%';

        -- Linkable globals and conservative header blockers have different
        -- visibility.  Boot/vDSO/EFI images must not link vmlinux globals, but
        -- common and architecture headers can still shadow an invocation.
        CREATE TEMP TABLE eligible_blocker_domains (
          caller_domain TEXT NOT NULL,
          candidate_domain TEXT NOT NULL,
          shared_only INTEGER NOT NULL,
          PRIMARY KEY (caller_domain, candidate_domain)
        );
        INSERT INTO eligible_blocker_domains
          SELECT caller_domain, candidate_domain, 0 FROM eligible_domains;
        INSERT OR IGNORE INTO eligible_blocker_domains
          SELECT DISTINCT domain, 'kernel', 1 FROM file_domains
          WHERE domain LIKE 'image:%';
        INSERT OR IGNORE INTO eligible_blocker_domains
          SELECT DISTINCT fd.domain,
                 'arch:' || substr(substr(f.path, 6), 1,
                   instr(substr(f.path, 6), '/') - 1), 1
          FROM file_domains fd JOIN files f ON f.id=fd.file_id
          WHERE fd.domain LIKE 'image:%' AND f.path LIKE 'arch/%/%';

        CREATE TEMP TABLE arch_blockers AS
        SELECT s.name, COUNT(DISTINCT s.id) AS n
        FROM symbols s
        JOIN files f ON f.id=s.file_id
        JOIN effective_file_domains domains ON domains.file_id=s.file_id
        WHERE domains.domain LIKE 'arch:%' AND (
          (s.kind IN ('function','syscall')
             AND (s.is_static=0 OR f.ext IN ('.h','.h_shipped')))
          OR (s.kind='macro' AND f.ext IN ('.h','.h_shipped'))
          OR (s.kind='variable' AND (
            s.is_static=0 OR f.ext IN ('.h','.h_shipped')))
        )
        GROUP BY s.name;
        CREATE UNIQUE INDEX temp.idx_arch_blockers_name
          ON arch_blockers(name);

        CREATE TEMP TABLE call_names AS
        SELECT domain, name FROM call_contexts GROUP BY domain, name;
        CREATE UNIQUE INDEX temp.idx_call_names_domain_name
          ON call_names(domain, name);

        CREATE TEMP TABLE global_resolution_names AS
        SELECT cn.domain, cn.name, MIN(g.symbol_id) AS global_id,
               COALESCE(SUM(g.n), 0) AS global_n
        FROM call_names cn
        JOIN eligible_domains ed ON ed.caller_domain=cn.domain
        LEFT JOIN domain_globals g
          ON g.domain=ed.candidate_domain AND g.name=cn.name
        GROUP BY cn.domain, cn.name;
        CREATE UNIQUE INDEX temp.idx_global_resolution_names
          ON global_resolution_names(domain, name);

        CREATE TEMP TABLE blocker_resolution_names AS
        SELECT cn.domain, cn.name,
               COALESCE(SUM(h.n), 0) AS header_n,
               COALESCE(SUM(m.n), 0) AS macro_n,
               COALESCE(SUM(CASE WHEN ed.shared_only=1 THEN shared.n
                                 ELSE v.n END), 0) AS variable_n
        FROM call_names cn
        JOIN eligible_blocker_domains ed ON ed.caller_domain=cn.domain
        LEFT JOIN domain_headers h
          ON h.domain=ed.candidate_domain AND h.name=cn.name
        LEFT JOIN domain_macros m
          ON m.domain=ed.candidate_domain AND m.name=cn.name
        LEFT JOIN domain_variables v
          ON v.domain=ed.candidate_domain AND v.name=cn.name
        LEFT JOIN domain_shared_variables shared
          ON shared.domain=ed.candidate_domain AND shared.name=cn.name
        GROUP BY cn.domain, cn.name;
        CREATE UNIQUE INDEX temp.idx_blocker_resolution_names
          ON blocker_resolution_names(domain, name);

        CREATE TEMP TABLE resolution_names AS
        SELECT cn.domain, cn.name, globals.global_id, globals.global_n,
               blockers.header_n, blockers.macro_n, blockers.variable_n,
               CASE WHEN cn.domain='kernel' THEN COALESCE(ab.n, 0)
                    ELSE 0 END AS arch_n
        FROM call_names cn
        JOIN global_resolution_names globals
          ON globals.domain=cn.domain AND globals.name=cn.name
        JOIN blocker_resolution_names blockers
          ON blockers.domain=cn.domain AND blockers.name=cn.name
        LEFT JOIN arch_blockers ab ON ab.name=cn.name
        ;
        CREATE UNIQUE INDEX temp.idx_resolution_names
          ON resolution_names(domain, name);

        -- Decide each top-level translation-unit instantiation independently.
        -- The final table below accepts an identity only when all such
        -- instantiations agree on both resolution class and target ID.
        CREATE TEMP TABLE unit_call_outcomes AS
        SELECT contexts.caller_file_id, contexts.name, contexts.unit_id,
          CASE
            WHEN local.n=1 AND binding.name IS NULL THEN
              CASE WHEN local.target_file_id=contexts.caller_file_id
                   THEN 'same_file' ELSE 'included_source' END
            WHEN local.name IS NOT NULL THEN 'ambiguous'
            WHEN binding.variable_n>0 AND binding.macro_n=0 THEN 'indirect'
            WHEN binding.macro_n>0 AND binding.variable_n=0 THEN 'macro'
            WHEN binding.name IS NOT NULL THEN 'ambiguous'
            -- Header include contexts are not recorded.  Same-header evidence
            -- above is sound, but the header's path domain cannot establish
            -- which linked image instantiates an inline call site.
            WHEN caller_file.ext IN ('.h','.h_shipped') AND (
              names.global_n + names.header_n + names.macro_n
              + names.variable_n + names.arch_n > 0
            ) THEN 'ambiguous'
            WHEN caller_file.ext IN ('.h','.h_shipped') THEN 'unresolved'
            WHEN names.global_n=1 AND names.header_n=0 AND names.macro_n=0
              AND names.variable_n=0 AND names.arch_n=0 THEN 'unique_global'
            WHEN names.macro_n>0 AND names.global_n=0 AND names.header_n=0
              AND names.variable_n=0 AND names.arch_n=0 THEN 'macro'
            WHEN names.global_n + names.header_n + names.macro_n
              + names.variable_n + names.arch_n > 0 THEN 'ambiguous'
            ELSE 'unresolved'
          END AS resolution,
          CASE
            WHEN local.n=1 AND binding.name IS NULL THEN local.symbol_id
            WHEN caller_file.ext NOT IN ('.h','.h_shipped')
              AND local.name IS NULL AND binding.name IS NULL
              AND names.global_n=1 AND names.header_n=0 AND names.macro_n=0
              AND names.variable_n=0 AND names.arch_n=0 THEN names.global_id
            ELSE NULL
          END AS callee_id
        FROM call_contexts contexts
        JOIN files caller_file ON caller_file.id=contexts.caller_file_id
        LEFT JOIN unit_local_callables local
          ON local.caller_file_id=contexts.caller_file_id
          AND local.name=contexts.name AND local.unit_id=contexts.unit_id
        LEFT JOIN unit_local_bindings binding
          ON binding.caller_file_id=contexts.caller_file_id
          AND binding.name=contexts.name AND binding.unit_id=contexts.unit_id
        JOIN resolution_names names
          ON names.domain=contexts.domain AND names.name=contexts.name;
        CREATE UNIQUE INDEX temp.idx_unit_call_outcomes
          ON unit_call_outcomes(caller_file_id, name, unit_id);

        CREATE TEMP TABLE expected_call_outcomes AS
        SELECT caller_file_id, name,
          CASE
            WHEN COUNT(DISTINCT resolution)=1 AND (
              MIN(resolution) NOT IN
                ('same_file','included_source','unique_global')
              OR COUNT(DISTINCT callee_id)=1
            ) THEN MIN(resolution)
            ELSE 'ambiguous'
          END AS resolution,
          CASE
            WHEN COUNT(DISTINCT resolution)=1
              AND MIN(resolution) IN
                ('same_file','included_source','unique_global')
              AND COUNT(DISTINCT callee_id)=1 THEN MIN(callee_id)
            ELSE NULL
          END AS callee_id
        FROM unit_call_outcomes
        GROUP BY caller_file_id, name;
        CREATE UNIQUE INDEX temp.idx_expected_call_outcomes
          ON expected_call_outcomes(caller_file_id, name);
        """
    )


def resolve(conn: sqlite3.Connection, *, keep_evidence: bool = False) \
        -> dict[str, int]:
    """Apply context-consistent outcomes and return counts by resolution."""
    prepare_evidence(conn)
    conn.executescript(
        """
        UPDATE calls AS c
        SET callee_id = (
              SELECT expected.callee_id
              FROM symbols caller
              JOIN expected_call_outcomes expected
                ON expected.caller_file_id=caller.file_id
                AND expected.name=c.callee
              WHERE caller.id=c.caller_id
            ),
            resolution = (
              SELECT expected.resolution
              FROM symbols caller
              JOIN expected_call_outcomes expected
                ON expected.caller_file_id=caller.file_id
                AND expected.name=c.callee
              WHERE caller.id=c.caller_id
            )
        WHERE c.direct_count>0 AND c.resolution='unresolved' AND EXISTS (
          SELECT 1 FROM symbols caller
          JOIN expected_call_outcomes expected
            ON expected.caller_file_id=caller.file_id
            AND expected.name=c.callee
          WHERE caller.id=c.caller_id
        );
        """
    )
    counts = {key: 0 for key in (
        "same_file", "included_source", "unique_global", "ambiguous",
        "macro", "indirect", "unresolved",
    )}
    for row in conn.execute(
            "SELECT resolution, COUNT(*) AS n FROM calls GROUP BY resolution"):
        counts[row["resolution"]] = row["n"]
    if not keep_evidence:
        drop_evidence(conn)
    conn.commit()
    return counts
