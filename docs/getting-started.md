# Getting started

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

Versioned examples illustrate Linux 6.18.46; names, counts, and line numbers vary by release.

- [Install](#install)
- [Quick start](#quick-start)
- [Where everything lives](#where-everything-lives)
- [Choosing a kernel version](#choosing-a-kernel-version)
- [Building indexes](#building-indexes)
- [Naming a target](#naming-a-target)
- [The main idea: "same level"](#the-main-idea-same-level)

## Install

Requires Python 3.10+ and roughly 3.5 GB of persistent storage per kernel version. A
recent kernel uses about 1.7 GB for source and 1.5 GB for a full call-enabled
index; exact sizes vary by release and selected symbol kinds.
Allow additional free space for the archive, extraction staging, and a second
index during atomic rebuilds. The previous index remains in place until its
replacement validates successfully.

```bash
git clone https://github.com/r34110k-4n74r35/kernel-atlas.git
cd kernel-atlas
python3 -m venv .venv
.venv/bin/pip install -e .
```

This creates two equivalent commands, `kernel-atlas` and the shorter `ka`,
**only inside the venv**. Nothing is added to your system `PATH`. Either
activate the venv:

```bash
source .venv/bin/activate
ka info mm
```

or call the full path (the shebang pins it to the venv's Python):

```bash
~/kernel-atlas/.venv/bin/ka info mm
```

Deleting `.venv/` removes both commands. Deleting `kernels/` and `indexes/`
reclaims the data. `NO_COLOR` (or `--color never`) turns coloring off.

## Quick start

```bash
ka versions                   # live list from kernel.org
ka build lts --with-calls     # download + index the latest LTS (once)
ka indexes                    # see which version the build selected
ka info mm                    # what is this directory, who maintains it?
ka struct usb_device          # every field, shape, condition, and source doc
ka struct usb_device --used-by # references in members and function declarations
ka info usb_get_dev --detail   # documented parameters, context, and return value
ka siblings kernel/sched      # what sits next to the scheduler?
ka find tcp_sendmsg --exact   # where is this symbol?
ka show tcp_sendmsg           # print its source
ka web tcp_sendmsg            # Elixir / git.kernel.org / GitHub URLs
ka docs mm                    # Documentation/ files for this area
ka docs usb_device --under driver-api/usb --explain
ka relationships SCHEDULER    # overlap and resolved flow across subsystems
ka locate tcp_sendmsg         # same symbol in every built index
ka check                      # deep-check the active index
dmesg | ka trace              # map a backtrace to subsystems
```

Default human-readable listing output prints `[Linux 6.18.46]` so it names the
index that answered. JSON rows carry an `index` field; the intentionally
index-free `names`, `plain`, and CSV forms are described under
[Controlling the output](commands.md#controlling-the-output).

Human-readable commands use consistent colored headings, aligned tables and
fields, and separate warnings and next steps. Color is automatic in terminals;
redirected output stays plain. Use `--color never` to disable it or `--color
always` to force it. JSON, CSV, `names`, `plain`, `path`, `web --url`, and
`show --bare` retain their undecorated output for scripts and editors.

## Where everything lives

With the editable checkout installation above, the kernel source and index sit
**inside the project directory**, so the code you are studying is next to the
tool:

```
kernel-atlas/
├── kernels/
│   ├── linux-6.18.46/              <- real kernel tree: open it, grep it
│   └── .linux-6.18.46.source.json <- identity for a downloaded tree
├── indexes/
│   ├── 6.18.46.db
│   └── .default-version    <- written by `ka use`, gitignored
└── src/kernel_atlas/
```

You can keep several versions at once (`kernels/linux-7.2/`, `indexes/7.2.db`,
…). `kernels/` and `indexes/` are gitignored. Point an editor or `grep` at
`kernels/linux-*/`, or let `ka path` hand you absolute paths into it.

The source-identity sidecar is created only for a tree downloaded and published
by kernel-atlas; a custom `--src` build neither creates one nor records deletion
authorization from one. An external `--src` tree is read without changing it.

All application data paths must stay inside the source checkout, including
custom `--output` and `--db` paths. Paths that escape through symlinks are
rejected. Copy an external index into the checkout before querying it: even a
read-only SQLite connection can use writable companion files beside the index.
`KERNEL_ATLAS_HOME` can choose a different data directory **inside**
the checkout, with `kernels/` and `indexes/` created beneath it. For example,
from the project root:

```bash
export KERNEL_ATLAS_HOME="$PWD/data"
```

An installation without its source checkout refuses data operations and asks
you to install an editable checkout. It does not create `~/.kernel-atlas` or
another application directory outside the project. If you need another disk,
place the whole checkout there.

| Generated files | Purpose and retention |
| --- | --- |
| `kernels/linux-V/`, `indexes/V.db`, custom index outputs | Persistent study data. Keep until you no longer need the snapshot. Prefer `ka remove` for managed indexes and sources. |
| Document text, function comments, and call sites within each new `.db` | Stored query evidence. Kept with that index; no separate application cache folder is created. |
| `kernels/.linux-V.source.json`, `indexes/.default-version`, source/output/pin locks | Persistent ownership, selection, and coordination metadata. Keep with the data; lock files are not disposable cache. Use `ka use --clear` to clear a pin. |
| Archives and `.part` files under `kernels/` | Downloads; partial files support resuming. Successful acquisition removes the archive unless `--keep-tarball` is set. |
| `*.building` beside an index, `.extracting-*` under `kernels/` | Temporary build/extraction staging. Normally cleaned up; crashes or cleanup failures can leave residue. |
| `.kernel-atlas-removing/` under `kernels/` | Quarantine for source removal. Preserve it and the source-identity sidecar so interrupted removal can be retried. |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` | Standard regenerable development caches. Tests keep synthetic sources and indexes under `.pytest_cache/tmp/`. |
| `.venv/`, `build/`, `dist/`, `src/kernel_atlas.egg-info/` | Local environment and packaging products. Recreate through installation or packaging when needed. |

`build/` and `dist/` are packaging products, not kernel study data. They need
not be retained after packaging or verification finishes. Routine application
commands do not create them.

Python caches keep their normal layout; the project does not consolidate them
into `.cache/` or require a special command runner. Package installers retain
their normal shared caches, and Python and the operating system may use system
temporary storage. SQLite sorts and temporary tables stay in process memory.
This is an application storage policy, not an operating-system sandbox: it does
not redirect those library and tool facilities. The default data directories are
Git-ignored; add ignore rules for any custom locations.
See [cleanup guidance](troubleshooting.md#leftover-generated-files) before
removing temporary-looking data.

## Choosing a kernel version

Three knobs pick which index a command uses, in this order:

1. `--db PATH` — an explicit index file inside the checkout, for scripts and tests.
2. `-K` / `--kernel VERSION` — this command only.
3. The **default index**: the version pinned with `ka use`, or if nothing is
   pinned (or that index is gone), the **highest built version**.

A unique **prefix** is enough, but only at a version-component boundary:
`6.18` selects `6.18.46`; `6.1` does **not**. `ka use 6` is ambiguous if you
have both 6.12 and 6.18.

```bash
ka use                  # what is pinned, and what is actually active
ka use 6.18             # pin by unique prefix → 6.18.46
ka use 6.18.46          # pin by exact version
ka use --clear          # unpin; go back to "highest built version"
ka indexes              # one row per index; * is the default

ka info tcp_sendmsg              # the pin
ka -K 7.2 info tcp_sendmsg       # another index, this command only
```

`ka indexes` marks the default with `*`. A pin is the file
`indexes/.default-version`. If you `ka remove` that version, the command reports
that it cleared the pin and later commands use the highest remaining index. If
the database was deleted outside kernel-atlas instead, the stale pin produces a
warning until you run `ka use --clear` or pin another index. The displayed
version comes from the index metadata; if a custom database filename differs,
`indexes` also shows that filename as the selection alias.

```bash
ka remove 6.18                # delete indexes/6.18.46.db; keep the source
ka rm 6.18.46 --source        # also delete kernels/linux-6.18.46/  (~1.7 GB)
```

`remove` (alias `rm`) resolves every name *before* deleting, so
`ka remove 6.18 6.18.46` is the same index named twice, not an error. SQLite
sidecar files (`.db-wal`, `.db-shm`, `.db-journal`) go with the index.

The kernel source is **kept by default**: rebuilding from a tree already on
disk avoids another download. Pass `--source` only when you also want that disk
back. This does not ask for confirmation — the version argument is the
confirmation. Recursive removal requires the persistent identity recorded both
in the index and in the downloaded tree's sidecar: its nonce, root device and
inode, and content digest must agree. An arbitrary custom `--src` tree is always
kept. Legacy/unmarked cached trees, and indexes that already recorded an edited
cache as local source, carry no removal authorization; `--source` keeps that
tree while removing the index.

For an authorized tree, source removal is attempted before its index is
deleted. A replacement or subsequently edited tree is refused, and an I/O
failure leaves the index and default pin in place. An in-progress marker lets a
partially completed removal be retried safely with the same command. Restore an
edited tree to its indexed digest before retrying, or handle that tree manually
and remove only the index without `--source`. `ka indexes` afterwards shows
what is left.

## Building indexes

```bash
ka versions                   # current mainline / stable / longterm
ka build lts                  # latest longterm  (best default for learning)
ka build stable
ka build mainline
ka build 6.12.104             # any exact version still on the CDN
ka build lts --force          # rebuild over an existing index
```

Version aliases are resolved live against kernel.org. Downloads resume if the
connection drops. Published CDN archives are checked against kernel.org's
`sha256sums.asc`; if that checksum cannot be obtained, the build fails unless
you explicitly pass `--no-verify`. Current release-candidate archives are the
exception: kernel.org generates them from cgit without a published checksum,
so `build mainline` uses the release feed's HTTPS URL and prints an explicit
warning. Extraction and indexing use unique same-directory scratch paths that
are renamed into place only on success. Per-source and per-output lifecycle
locks serialize builds and removals through final publication, so a source
cannot be removed under active parser workers and concurrent commands cannot
overwrite one another's completed index.

A downloaded extraction receives the identity sidecar shown above. While that
sidecar and tree still match, the full tree digest is checked before indexing
and again immediately before the completed database is published; on a large
kernel, those two reads add noticeable storage I/O. A checksum-verified
kernel.org archive (or the
explicitly warned current-RC exception) records its archive URL and enables
upstream release-reference links. A download accepted via `--no-verify`, a
legacy/unmarked cache, or a cache edited before the build is recorded
conservatively as local source, so `ka web` does not claim that it matches an
upstream tag. Edits made after a completed build do not rewrite the index
snapshot; rebuild after changing the tree.

| `build` option | Effect |
| --- | --- |
| `--src PATH` | index a kernel tree you already have (version from its `Makefile`, unless an explicit positional version is supplied) |
| `--kinds LIST` | symbol kinds to index (default: `function,syscall,struct,union,enum,typedef,macro,variable`; add `prototype` if wanted) |
| `--with-calls` | also record the call graph (enables `ka calls`; a few hundred MB extra; requires the `macro` and `variable` kinds used to prevent false identities) |
| `--jobs N` | parser processes (automatic default: one per CPU, capped at 16; explicit range 1–256) |
| `--output PATH` | write the index to a specific path inside the checkout |
| `--keep-tarball` | keep the downloaded source archive after extraction |
| `--no-verify` | skip the checksum (not recommended) |
| `--force` | replace an existing index (reusing its source tree when present) |
| `--quiet` | suppress progress; keep the final build summary and errors |

Build progress appears on stderr automatically, with aligned labels and colored
statuses. Downloads show bytes and transfer speed; scanning shows discovered
files and directories. Parsing shows a progress bar, files processed, worker
count, symbols, call records, skipped inputs, and failures. Ownership mapping has
its own file counter. Known totals include a percentage and estimated remaining
time based on the phase's measured rate; estimates can change when later files
are more expensive to parse.

Archive extraction, build-domain analysis, call resolution, database indexing,
source-identity checks, and the final integrity audit show their phase and
elapsed time. These steps have no reliable total, so they use a spinner. Each
phase finishes with `done`, `failed`, or `interrupted`; finishing parsing does
not mean the entire index has been built.

Terminals update in place and wrap details to the available width. Redirected
stderr uses start/end lines and occasional counter updates. The final summary
appears on stdout, grouping the index location, elapsed time, counts, any
parsing warnings, and commands to try next. `--quiet` suppresses progress and
routine acquisition messages while keeping the summary, errors, and source
verification warnings.

As with other commands, `--color auto` is the default: each output stream uses
color only when it is a terminal, unless `NO_COLOR` is set or `TERM=dumb`.
Redirected output is therefore plain by default. Use `--color never` to disable
color, or `--color always` to force ANSI colors in human-readable output,
including when redirected. Status words remain visible with every color mode:

```bash
ka build --src /path/to/linux --with-calls --jobs 8
ka build --src /path/to/linux --with-calls 2>build-progress.log
ka build --src /path/to/linux --quiet
ka --color never build --src /path/to/linux
```

With `--src`, download aliases such as `lts` are not version labels: omit the
positional argument to detect the tree's `Makefile` version, or supply an
explicit literal version for a vendor/local tree.

An output elsewhere inside the checkout, outside the managed `indexes/`
directory, is intentionally not discovered by `ka indexes`, `ka use`,
`ka remove`, or `-K`; the build summary prints exact `--db PATH` commands for
querying it, and you manage that database file yourself. A custom filename
inside `indexes/` is discoverable and acts as its selection alias.

A recent kernel is roughly 6,000 directories, 95,000 files, 4.2 million
symbols, and 3,000 MAINTAINERS sections. A full call-enabled build can take
several minutes depending on CPU and storage. Most of the symbol count is
macros; `--kinds function,syscall,struct,enum,typedef` is much smaller if you do
not need them. The build summary and `ka stats` separately report files that
were parsed, skipped, or failed. Before a completed index becomes active, the
same deep structural and semantic audit exposed by `ka check` is run against it.

After editing a local source tree, use
`ka build --src /path/to/linux --with-calls --force` to replace its existing
index. The replacement is fully built and audited before publication.

Schema-6 indexes remain readable for their existing features. Rebuild to obtain
the individual call sites, document text, and function kernel-doc stored in
schema 7. New query modes report missing capabilities; opening an index never
migrates or rewrites it automatically.

## Naming a target

General browsing commands such as `info` accept these target spellings:

```bash
ka info mm                                 # a folder
ka info mm/page_alloc.c                    # a file
ka info mm/page_alloc.c:__alloc_pages_noprof
ka info tcp_sendmsg                        # a bare symbol name
ka info tcp.c:tcp_sendmsg                  # basename:symbol — the symbol picks the right tcp.c
ka info mm/page_alloc.c:5268               # whichever symbol spans that line
ka info page_alloc.c                       # a bare filename (reports if ambiguous)
ka info sched                              # a bare directory name
ka info .                                  # the kernel root
ka info /absolute/path/to/linux/mm/page_alloc.c:5268
```

Indexed paths and subtree boundaries are literal and case-sensitive, including
on a host filesystem that ignores case. A wildcard character in a path is part
of its name. Symbol-specific commands such as `struct` and `calls` additionally
require the corresponding declaration kind; see the [command reference](commands.md).

When a name is ambiguous (a definition per architecture, a stub in
`tools/`, a `#define` copy), the most likely candidate is chosen: real
definitions beat prototypes, non-static beats static, `tools/` / `samples/`
lose to the real tree, shallower paths beat nested stubs — and the
alternatives are listed. Typos get "did you mean" suggestions.
`net/ipv4/tcp.c:no_such_fn` tells you the file exists but that symbol does
not, instead of guessing what the whole string might mean.

Commands which act on one concrete source identity (`calls`, `show`, `path`,
`web`, and `struct`) do not accept that ranking as proof. Use `path:symbol` when
same-named definitions are in different files, or `path:line` when more than
one definition occurs in the same file. A basename-only line selector such as
`super.c:20` is not exact when several indexed files are named `super.c`; use
the full indexed path. An absolute target is accepted only
when it is inside the exact
recorded source tree for the selected index and that tree is still available;
it is normalized to the indexed relative path and may retain a `:line` or
`:symbol` suffix. An absolute path never switches the active index or silently
substitutes a different source snapshot.

`ka struct` resolves only struct/union tags and their direct typedef aliases; a
same-named function or variable cannot win. An optional `struct ` or `union `
prefix constrains the C kind. Because duplicate tags and configuration
alternatives are common, an ambiguous name is an error, not a ranking decision.
Qualify it as `path:name` or, for repeated definitions in one file,
`path:line`; `--all` deliberately reports every matching definition. A line
inside overlapping aggregates (for example a generated tagged group inside an
outer struct) remains ambiguous and is never silently treated as exact.

`ka docs bpf` is the exception: a bare name prefers the *area directory*
(`kernel/bpf/`) over a symbol that happens to share it. `ka info bpf` still
resolves the symbol, because that is what you want from an oops.

## The main idea: "same level"

This is the query the rest of the tool is built around.

Every target lives in a **container**:

| Target | Its container |
| --- | --- |
| a folder | its parent directory |
| a file | its directory |
| a symbol | the file it is defined in |

`ka siblings` lists the other members of that container. `--level` widens
the container; `--kinds` chooses *what* to list, independently of what you
asked about. So you can ask for the functions next to a file, or the files
next to a function.

| `--level` | Scope becomes |
| --- | --- |
| `auto` (default) | the natural container above |
| `file` | the containing file (meaningful for symbols) |
| `dir` | the containing directory |
| `subtree` | that directory and everything beneath it |
| `subsystem` | every file the target's subsystem claims |
| `tree` | the entire kernel |

`--level subsystem` needs a single defensible owner: a file (and therefore a
symbol) must have exactly one primary owner, while every descendant file of a
directory must be covered by the same non-catch-all owner. A co-primary file or
mixed/partially unclassified directory is rejected instead of arbitrarily
choosing one section; name the section with `ka subsystem NAME --files` when
you want its claimed-file view explicitly.

```bash
ka siblings kernel/sched                              # other core-kernel dirs
ka siblings net/ipv4/tcp.c                            # other files in net/ipv4/
ka siblings tcp_sendmsg                               # other functions in tcp.c
ka siblings tcp_sendmsg --level subsystem             # every NETWORKING [TCP] function
ka siblings net/ipv4/tcp.c --kinds function           # functions next to a *file*
ka siblings tcp_sendmsg --level dir --kinds file      # files around a *symbol*
ka siblings kernel/sched --include-self               # keep the target, marked >
ka siblings tcp_sendmsg --exported                    # module-visible API of the file
```

`ka ls` looks *inside* rather than *beside* (children of a folder, or symbols
defined in a file). `--limit` / `-n` counts *other* rows, so `-n 5` is five
siblings, not four plus the thing you asked about. `ka ls` with no argument
lists the kernel root.
