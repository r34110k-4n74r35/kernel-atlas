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
  __init__.py          package metadata and version
  __main__.py          python -m kernel_atlas entry point
  commands/            CLI entry point, command handlers, shared CLI services
  parsing/             C parser, MAINTAINERS, syntax, records, recovery, documentation
  indexing/            index builds, Kbuild domains, includes, call identity resolution
  queries/             target resolution, listings, structures, documentation, links
  storage/             configuration, database, source acquisition, identity, locking
  presentation/        listing formats, terminal output, progress, study reports
docs/                  task-oriented user and developer guides
tests/
  __init__.py          test package marker
  conftest.py          shared fixtures and project-local temporary directories
  commands/            command behavior and process smoke tests
  parsing/             C symbols, recovery, aggregates, calls, MAINTAINERS
  indexing/            builds, Kbuild evidence, includes, call resolution
  queries/             resolution, listings, structures, resources, relationships
  storage/             schema, integrity, acquisition, locking, safe paths
  presentation/        terminal presentation and machine-format contracts
  support/             synthetic kernels and indexes, CLI matrix, terminal helpers
kernels/, indexes/     generated local study data (Git-ignored)
```

All implementation modules live in the six feature packages, with matching
names under `src/kernel_atlas/` and `tests/`. For example,
`indexing/indexer.py` is covered by `tests/indexing/`, and
`presentation/render.py` by `tests/presentation/`. Only package metadata and
the module entry point remain at the source package root. Shared test
configuration stays at the test root; `tests/support/` contains test fixtures
only, so it has no production counterpart.

Internal modules use short names within their feature package:

| Package | Modules |
| --- | --- |
| `parsing/` | `cparse.py`, `maintainers.py`, `models.py`, `syntax.py`, `calls.py`, `recovery.py`, `aggregates.py`, `documentation.py`, `groups.py` |
| `indexing/` | `indexer.py`, `kbuild.py`, `call_resolution.py` |
| `queries/` | `query.py`, `models.py`, `paths.py`, `targeting.py`, `structures.py`, `documentation.py`, `relationships.py`, `links.py` |
| `storage/` | `config.py`, `db.py`, `kernelsrc.py`, `schema.py`, `validation.py`, `integrity.py`, `managed_source.py`, `locks.py` |
| `presentation/` | `render.py`, `terminal.py`, `progress.py`, `build.py`, `structure.py` |

The command package groups orchestration by task:

| Module | Responsibility |
| --- | --- |
| `commands/cli.py` | Entry point, dispatch, shared services for handlers |
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

Supporting domain modules remain outside the CLI package: `parsing/maintainers.py`
handles ownership, `indexing/call_resolution.py` resolves identities, `queries/relationships.py`
computes subsystem connections, and `storage/kernelsrc.py` provides the source
acquisition API. `storage/managed_source.py` owns provenance and removal;
`storage/locks.py` coordinates concurrent operations. `storage/config.py` determines
local storage; `queries/links.py` builds upstream URLs.
`presentation/progress.py` handles build-phase counters, timing, terminal refresh, and plain
stderr logs. Keep it independent of indexing logic and out of parser workers.

Orchestration and API modules live alongside their feature helpers. Helpers
depend on shared models and other helpers, not their invoking orchestration module.
Parser fragments and filesystem rename operations receive explicit callbacks
where orchestration must supply a dependency. CLI handlers receive shared
services through the entry point; `commands/cli.py` re-exports those services from their
own modules. Keep these imports one-way.

Tests are Python packages, so same-purpose filenames can live beside their
feature without import-name collisions. Keep feature-specific setup in that
directory's `helpers.py` or `conftest.py`. Put only utilities shared across
features in `tests/support/`; test modules must not import other test modules.
The CLI smoke and presentation suites share `tests/support/cli_matrix.py`,
which checks every registered command and advertised format.
Shared documentation and path-query indexes, along with simulated storage
boundaries, live in `tests/support/indexes.py`; `tests/conftest.py` registers
their fixtures. Keep query API assertions in
`tests/queries/`, command behavior in `tests/commands/`, and color, wrapping, and
output-format contracts in `tests/presentation/`.

## Choosing where to change code

Keep argument syntax in `commands/parser.py`, command orchestration in the relevant
`commands/` handler, and domain logic in a feature module. For example, documentation
ranking belongs in `queries/documentation.py`; its terminal and JSON presentation
belongs in `commands/resources.py`. In `queries/query.py`, `documentation_for()`
returns file entries, while `documentation_matches()` exposes entries plus reasons.

Kbuild analysis belongs in `indexing/kbuild.py`. Keep end-to-end build tests in
`tests/indexing/test_indexer_build.py` and isolated Makefile interpretation tests
in `tests/indexing/test_kbuild.py`. Translation-unit, include-path, and call
resolution regressions have separate modules in the same directory.
`indexing/indexer.py` coordinates the build and imports analysis helpers from
`indexing/kbuild.py` and `indexing/call_resolution.py`.
Build-domain and include analysis share the indexed Makefile inventory, avoiding
redundant tree walks and keeping both analyses inside the same source boundary.
The set of parsed source paths is reused across all Makefiles. Recipe bodies
and unexpanded templates are not top-level build evidence.

Use `queries.paths.glob_under()` for directory boundaries; SQLite `LIKE` folds
ASCII case and can mix distinct Linux paths. Keep case-insensitive name search
in `LIKE` predicates. Filter hidden rows before SQL limits, so a bounded query
agrees with the same prefix of its unbounded result.

Keep normal C capture dispatch in `parsing/cparse.py`, malformed-source recovery in
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

`ka`, `kernel-atlas`, and `python -m kernel_atlas` keep the same command syntax
and behavior. Both installed console scripts now point to
`kernel_atlas.commands.cli:main`. Reinstall an existing editable checkout with
`.venv/bin/python -m pip install -e '.[dev]'` to refresh its console scripts.
Short command aliases share the same handlers and remain available.

Python imports must use the feature packages. For example, replace
`from kernel_atlas import db, query` with `from kernel_atlas.storage import db`
and `from kernel_atlas.queries import query`; use
`from kernel_atlas.commands.cli import main` for the CLI entry point.
The former root module paths have no compatibility shims. This regrouping
preserves the database schema and index format, so existing indexes remain
usable without rebuilding.
