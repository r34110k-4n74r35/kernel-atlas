# Development

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

## Setup and checks

From the project root, install the development extra into your virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
git diff --check
```

For a narrower feedback loop, run the tests for the feature being changed:

```bash
.venv/bin/python -m pytest -q tests/queries/test_documentation.py
.venv/bin/python -m pytest -q tests/indexing/
.venv/bin/python -m pytest -q tests/commands/ tests/presentation/
```

Run the full suite after changing shared models, module boundaries, CLI
definitions, or schema rules. Keep regression tests focused on observable
behavior, including bounded results, ambiguous identities, and path boundaries.

The tests build a small synthetic kernel tree (`tests/support/kernel_tree.py`) with its own
`MAINTAINERS`. Each test gets an isolated `KERNEL_ATLAS_HOME`, including source
and output locks, so the suite needs no network and does not touch your real
indexes. Numbered temporary runs live under `.pytest_cache/tmp/` inside the
checkout; this includes generated kernel fixtures and databases. Pytest discovery
is confined to `tests/`; downloaded kernel selftests are never collected.

Python bytecode, pytest, and Ruff caches use their normal layout. There is no
cache consolidation or special runner. Installation and packaging tools keep
their normal shared caches and temporary-directory behavior. See the
[storage guide](getting-started.md#where-everything-lives) for retention and cleanup.

## Layout

```text
src/kernel_atlas/
  cli.py               public entry point and command dispatch
  cparse.py            parser setup and symbol capture orchestration
  indexer.py           scan, parse, attach evidence, publish
  db.py                public database API and connections
  kernelsrc.py         release lookup, download, extraction, acquisition
  query.py             public target resolution and query orchestration
  render.py            human and machine listing formats
  config.py            project-local storage configuration
  maintainers.py       MAINTAINERS interpretation and ownership patterns
  commands/            command handlers and shared CLI services
  parsing/             syntax, records, recovery, calls, aggregates, documentation
  indexing/            Kbuild domains, literal includes, call identity resolution
  queries/             models, paths, targeting, structures, documentation, links
  storage/             schema, validation, integrity, source identity, locking
  presentation/        terminal formatting, progress, build and structure reports
docs/                  task-oriented user and developer guides
tests/
  commands/            command behavior and process smoke tests
  parsing/             C symbols, recovery, aggregates, calls, MAINTAINERS
  indexing/            builds, Kbuild evidence, includes, call resolution
  queries/             resolution, listings, structures, resources, relationships
  storage/             schema, integrity, acquisition, locking, safe paths
  presentation/        terminal presentation and machine-format contracts
  support/             synthetic kernel, CLI matrix, terminal test helpers
kernels/, indexes/     generated local study data (Git-ignored)
```

The six feature packages have matching names under `src/kernel_atlas/` and
`tests/`. Tests of a public root module belong to its feature group: for example,
`indexer.py` is covered by `tests/indexing/`, and `render.py` by
`tests/presentation/`. `tests/support/` contains test fixtures only, so it has no
production counterpart.

Internal modules use short names within their feature package:

| Package | Modules |
| --- | --- |
| `parsing/` | `models.py`, `syntax.py`, `calls.py`, `recovery.py`, `aggregates.py`, `documentation.py`, `groups.py` |
| `indexing/` | `kbuild.py`, `call_resolution.py` |
| `queries/` | `models.py`, `paths.py`, `targeting.py`, `structures.py`, `documentation.py`, `relationships.py`, `links.py` |
| `storage/` | `schema.py`, `validation.py`, `integrity.py`, `managed_source.py`, `locks.py` |
| `presentation/` | `terminal.py`, `progress.py`, `build.py`, `structure.py` |

The command package groups orchestration by task:

| Module | Responsibility |
| --- | --- |
| `commands/parser.py` | Options, validation, command registration |
| `commands/build.py` | Source acquisition and atomic index builds |
| `commands/lifecycle.py` | Releases, index selection, removal, statistics, checks |
| `commands/browse.py` | Information, listing, search, subsystems, tree |
| `commands/source.py` | Recorded-source paths, containment checks, `path` and `show` |
| `commands/aggregate.py` | Struct/union study reports |
| `commands/calls.py` | Backtraces, call graphs, subsystem relationships |
| `commands/resources.py` | Source links, documentation, cross-version lookup |
| `commands/selection.py` | Index selection, connection lifetime, safe removal authorization |
| `commands/targeting.py` | Target resolution, ambiguity diagnostics, follow-up commands |
| `commands/output.py` | CLI diagnostics, listing columns, formats, symbol filters |

Supporting domain modules remain outside the CLI package: `maintainers.py`
handles ownership, `indexing/call_resolution.py` resolves identities, `queries/relationships.py`
computes subsystem connections, and `kernelsrc.py` provides the public source
acquisition API. `storage/managed_source.py` owns provenance and removal;
`storage/locks.py` coordinates concurrent operations. `config.py` determines
local storage; `queries/links.py` builds upstream URLs.
`presentation/progress.py` handles build-phase counters, timing, terminal refresh, and plain
stderr logs. Keep it independent of indexing logic and out of parser workers.

The facade modules (`cparse.py`, `query.py`, `render.py`, `db.py`, `kernelsrc.py`,
and `cli.py`) retain their established public imports and entry points.
Feature modules depend on shared models and helpers, not their invoking facade.
Parser fragments and filesystem rename operations receive explicit callbacks
where orchestration must supply a dependency. CLI handlers receive shared
services through the entry point; `cli.py` re-exports those services from their
own modules. Keep these imports one-way.

Tests are Python packages, so same-purpose filenames can live beside their
feature without import-name collisions. Keep feature-specific setup in that
directory's `helpers.py` or `conftest.py`. Put only utilities shared across
features in `tests/support/`; test modules must not import other test modules.
The CLI smoke and presentation suites share `tests/support/cli_matrix.py`,
which checks every registered command and advertised format.

## Choosing where to change code

Keep argument syntax in `commands/parser.py`, command orchestration in the relevant
`commands/` handler, and domain logic in a feature module. For example, documentation
ranking belongs in `queries/documentation.py`; its terminal and JSON presentation
belongs in `commands/resources.py`. `query.documentation_for()` still returns file
entries, while `query.documentation_matches()` exposes entries plus reasons.

Kbuild analysis belongs in `indexing/kbuild.py`. Keep end-to-end build tests in
`tests/indexing/test_indexer_build.py` and isolated Makefile interpretation tests
in `tests/indexing/test_kbuild.py`. Translation-unit, include-path, and call
resolution regressions have separate modules in the same directory.
The existing indexer helper imports are retained for compatibility.
Build-domain and include analysis share the indexed Makefile inventory, avoiding
redundant tree walks and keeping both analyses inside the same source boundary.
The set of parsed source paths is reused across all Makefiles. Recipe bodies
and unexpanded templates are not top-level build evidence.

Use `queries.paths.glob_under()` for directory boundaries; SQLite `LIKE` folds
ASCII case and can mix distinct Linux paths. Keep case-insensitive name search
in `LIKE` predicates. Filter hidden rows before SQL limits, so a bounded query
agrees with the same prefix of its unbounded result.

Keep normal C capture dispatch in `cparse.py`, malformed-source recovery in
`parsing/recovery.py`, and invocation classification in `parsing/calls.py`.
Aggregate member traversal belongs in `parsing/aggregates.py`; its documentation
and struct-group interpretation have dedicated helpers. Split by responsibility
when it separates independently understandable behavior, rather than imposing
a line limit on a cohesive syntax-tree traversal.

The documentation is split by reader task: `README.md` introduces the tool,
`getting-started.md` covers setup, `commands.md` defines behavior, and
`architecture.md` records evidence and limitations. Update command help and
the reference together when adding options. Documentation links are relative
so they work in a checkout and on the repository website.

## Packaging and compatibility

```bash
.venv/bin/python -m build
```

This builds a source archive and a wheel from that archive. `MANIFEST.in` keeps
the guides and complete test fixtures in the source archive. The project
version is defined once in `src/kernel_atlas/__init__.py`; setuptools reads it
for package metadata. Runtime dependencies stay separate from the `dev` extra.

`ka`, `kernel-atlas`, `python -m kernel_atlas`, and imports from
`kernel_atlas.cli` keep their existing entry points. Internal `cli_*` handler
modules now live in `kernel_atlas.commands`; import that package when working
on command internals. Short command aliases share the same handlers and remain
available.

These changes use the existing schema. Query fixes work with existing indexes;
the Kbuild and C-include fixes require rebuilding call graphs with
`--with-calls --force`. Preserve the original `--src` and `--output` when
rebuilding a custom snapshot.
