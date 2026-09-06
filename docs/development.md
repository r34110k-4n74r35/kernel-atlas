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
.venv/bin/python -m pytest -q tests/test_documentation.py
.venv/bin/python -m pytest -q tests/test_kbuild.py tests/test_indexer.py
```

Run the full suite after changing shared models, module boundaries, CLI
definitions, or schema rules. Keep regression tests focused on observable
behavior, including bounded results, ambiguous identities, and path boundaries.

The tests build a small synthetic kernel tree (`tests/fixture.py`) with its own
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
  cli.py               public entry point and shared CLI services
  commands/            argument parser and command orchestration
  cparse*.py           C parser, records, and syntax helpers
  aggregate_parse.py   aggregate declarations and documentation
  indexer.py           scan, parse, attach evidence, publish
  kbuild.py            build domains and literal C includes
  db.py                schema and integrity checks
  query*.py            lookup, scopes, results, and path predicates
  *_query.py           structure and documentation query features
  *render*.py          human and machine output
docs/                  task-oriented user and developer guides
tests/                 synthetic fixtures and offline regression tests
kernels/, indexes/     generated local study data (Git-ignored)
```

The command package groups orchestration by task:

| Module | Responsibility |
| --- | --- |
| `commands/parser.py` | Options, validation, command registration |
| `commands/lifecycle.py` | Releases, builds, selection, removal, statistics, checks |
| `commands/browse.py` | Information, listing, search, subsystems, tree, source |
| `commands/aggregate.py` | Struct/union study reports |
| `commands/calls.py` | Backtraces, call graphs, subsystem relationships |
| `commands/resources.py` | Source links, documentation, cross-version lookup |

Supporting domain modules remain outside the CLI package: `maintainers.py`
handles ownership, `call_resolution.py` resolves identities, `relationships.py`
computes subsystem connections, and `kernelsrc.py` manages downloads and source
identity. `config.py` determines local storage; `links.py` builds upstream URLs.
`progress.py` handles build-phase counters, timing, terminal refresh, and plain
stderr logs. Keep it independent of indexing logic and out of parser workers.

The facade modules (`cparse.py`, `query.py`, `render.py`, and `cli.py`) retain
their established imports and entry points. Parser/query/render feature modules
depend only on shared models and helpers, not their invoking facade. CLI feature
handlers receive shared services from `cli.py` without importing it. The result
has one-way imports, while callers do not need to learn internal module names.

## Choosing where to change code

Keep argument syntax in `commands/parser.py`, command orchestration in the relevant
`commands/` handler, and domain logic in a feature module. For example, documentation
ranking belongs in `documentation_query.py`; its terminal and JSON presentation
belongs in `commands/resources.py`. `query.documentation_for()` still returns file
entries, while `query.documentation_matches()` exposes entries plus reasons.

Kbuild analysis belongs in `kbuild.py`. Keep end-to-end build tests in
`test_indexer.py` and isolated Makefile interpretation tests in `test_kbuild.py`.
The existing indexer helper imports are retained for compatibility.
Build-domain and include analysis share the indexed Makefile inventory, avoiding
redundant tree walks and keeping both analyses inside the same source boundary.
The set of parsed source paths is reused across all Makefiles. Recipe bodies
and unexpanded templates are not top-level build evidence.

Use `query_paths.glob_under()` for directory boundaries; SQLite `LIKE` folds
ASCII case and can mix distinct Linux paths. Keep case-insensitive name search
in `LIKE` predicates. Filter hidden rows before SQL limits, so a bounded query
agrees with the same prefix of its unbounded result.

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
