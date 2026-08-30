# kernel-atlas

A map of the Linux kernel for people who are still learning their way around it.

`kernel-atlas` downloads a kernel from kernel.org, records its directory and
file map, extracts the C symbols it can parse, and maps each path to a
**subsystem** using the kernel's own `MAINTAINERS` file. Then you can ask:

- What else lives next to this folder, file, or function?
- Which subsystem owns this symbol, and who maintains it?
- Which subsystems overlap, and which ones call into one another?
- What exactly is inside this structure, and what does each member mean?
- I have a name from an oops. Where is it defined?
- Show me the source, an editor path, or the Elixir / docs.kernel.org page.
- Did this symbol move between the LTS I am running and another release?

Queries are local and offline. `ka web` only *prints* upstream release-reference
URLs (Bootlin Elixir, git.kernel.org, GitHub, docs.kernel.org).

Examples below are from **Linux 6.18.46**. Line numbers move
between releases; `ka locate` is how you compare them.

```
$ ka info tcp_sendmsg

net/ipv4/tcp.c:tcp_sendmsg

  kind         function
  defined in   net/ipv4/tcp.c:1409-1418 (10 lines)
  signature    int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
  linkage      EXPORT_SYMBOL (available to modules)
  on disk      ~/kernel-atlas/kernels/linux-6.18.46/net/ipv4/tcp.c
  index        Linux 6.18.46
  elixir       https://elixir.bootlin.com/linux/v6.18.46/source/net/ipv4/tcp.c#L1409
  ident        https://elixir.bootlin.com/linux/v6.18.46/ident/tcp_sendmsg

  Area: Networking
    Network stack: sockets, TCP/IP, netfilter, per-protocol code.

  Subsystem (from MAINTAINERS)
   * NETWORKING [TCP]   [Maintained]  49 files
       maintainer  Eric Dumazet <edumazet@google.com>
       list        netdev@vger.kernel.org
```

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Where everything lives](#where-everything-lives)
- [Choosing a kernel version](#choosing-a-kernel-version)
- [Building indexes](#building-indexes)
- [Naming a target](#naming-a-target)
- [The main idea: "same level"](#the-main-idea-same-level)
- [Commands](#commands)
- [A tour across subsystems](#a-tour-across-subsystems)
- [Typical workflows](#typical-workflows)
- [Controlling the output](#controlling-the-output)
- [How subsystems are determined](#how-subsystems-are-determined)
- [How parsing works, and its limits](#how-parsing-works-and-its-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install

Requires Python 3.10+ and roughly 3.5 GB of free disk per kernel version. A
recent kernel uses about 1.7 GB for source and 1.5 GB for a full call-enabled
index; exact sizes vary by release and selected symbol kinds.

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
ka use 6.18                   # pin it; unique prefix is enough
ka info mm                    # what is this directory, who maintains it?
ka struct usb_device          # every field, shape, condition, and source doc
ka siblings kernel/sched      # what sits next to the scheduler?
ka find tcp_sendmsg --exact   # where is this symbol?
ka show tcp_sendmsg           # print its source
ka web tcp_sendmsg            # Elixir / git.kernel.org / GitHub URLs
ka docs mm                    # Documentation/ files for this area
ka relationships SCHEDULER    # overlap and resolved flow across subsystems
ka locate tcp_sendmsg         # same symbol in every built index
ka check                      # deep-check the active index
dmesg | ka trace              # map a backtrace to subsystems
```

Default human-readable listing output prints `[Linux 6.18.46]` so it names the
index that answered. JSON rows carry an `index` field; the intentionally
index-free `names`, `plain`, and CSV forms are described under
[Controlling the output](#controlling-the-output).

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
authorization from one. A normal package installation that is not running from
its checkout defaults to
`~/.kernel-atlas/`. Set `KERNEL_ATLAS_HOME` to choose the data root explicitly,
including when you want the data on another disk:

```bash
export KERNEL_ATLAS_HOME=/mnt/big-disk/kernel-atlas
```

## Choosing a kernel version

Three knobs pick which index a command uses, in this order:

1. `--db PATH` — an explicit index file, for scripts and tests.
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
| `--output PATH` | write the index somewhere specific |
| `--keep-tarball` | keep the downloaded source archive after extraction |
| `--no-verify` | skip the checksum (not recommended) |
| `--force` | replace an existing index (reusing its source tree when present) |
| `--quiet` | no progress output |

With `--src`, download aliases such as `lts` are not version labels: omit the
positional argument to detect the tree's `Makefile` version, or supply an
explicit literal version for a vendor/local tree.

An output outside `indexes/` is intentionally not discovered by `ka indexes`,
`ka use`, `ka remove`, or `-K`; the build summary prints exact `--db PATH`
commands for querying it, and you manage that database file yourself. A custom
filename inside `indexes/` is discoverable and acts as its selection alias.

A recent kernel is roughly 6,000 directories, 95,000 files, 4.2 million
symbols, and 3,000 MAINTAINERS sections. A full call-enabled build can take
several minutes depending on CPU and storage. Most of the symbol count is
macros; `--kinds function,syscall,struct,enum,typedef` is much smaller if you do
not need them. The build summary and `ka stats` separately report files that
were parsed, skipped, or failed. Before a completed index becomes active, the
same deep structural and semantic audit exposed by `ka check` is run against it.

## Naming a target

Every command that takes a `target` accepts all of these:

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

## Commands

### `versions` / `build` / `indexes` / `use` / `remove`

See [Building indexes](#building-indexes) and
[Choosing a kernel version](#choosing-a-kernel-version). After a successful
build, two starter queries are printed. A normal build uses or creates its
source tree under `kernels/` and writes its database under `indexes/`; `use` writes the pin,
and `remove --source` may remove both managed objects. An explicit
`build --output PATH` writes the database at that path. These commands never
change your system `PATH`.

### `stats`

Totals for the active index—including call records and their underlying
source-level occurrence count—symbols by kind, and the largest top-level
directories with a one-line description (`mm` → Memory management, `net` →
Networking). One call record groups one caller and invocation spelling; only a
record with a resolved callee identity is a concrete graph edge. Useful as a
first orientation.

### `check` / `doctor`

Runs the full row-level audit used before a completed build becomes active. It
checks metadata and roll-up counts, value types and ranges, safe path topology,
symbol/file compatibility, ownership ranks and co-primary ties, source-include
records, call identities, occurrence counts, and every call-resolution
classification, including parser-proven macro and indirect calls.
Normal queries perform a faster schema/metadata check; use `ka check` after
copying an index, receiving a custom `--db`, or when corruption is suspected.
`-f json` provides a small machine-readable success result; a failed audit exits
with an error instead of treating the index as sound.

### `info`

The "what is this?" command. For a directory: how many files sit in it, the
top-level *area* (plain English for `mm/`, `net/`, `kernel/`, …), its subsystem
composition derived from descendant files, a walk of parent directories, the
recorded on-disk path, and—when the index records a matching authoritative
kernel.org archive—upstream release-reference links. Composition rows are ranked by
primary and claimed descendants and show both counts plus coverage; they do not
pretend a mixed directory has one owner.

For a file: size, line count, parse status, how many symbols of each kind it
defines, and every non-catch-all `MAINTAINERS` match ordered by specificity.
Every section tied for the strongest evidence is marked primary, so equal
claims remain visible as co-primary rather than being broken by name order.
Catch-all-only and genuinely unmatched files are shown as **Unclassified**.

For a symbol: kind, line span, signature, and linkage (`EXPORT_SYMBOL` /
`static` / global). When upstream release-reference links are available, it
also includes Elixir's *ident* page (every use of that name).

`-f json` dumps the same facts, including `links`, `source_path`,
`source_exists`, and structured unclassified-ownership information.
`--max-subsystems` and `--max-candidates` trim the two lists that can get
long.

### `struct` / `structure`

A source-level structure report designed for studying kernel interfaces and
internal data flow:

```bash
ka struct usb_device
ka struct 'struct usb_driver'
ka struct include/linux/usb.h:usb_device
ka struct include/uapi/linux/perf_event.h:1320
ka struct perf_mem_data_src --all -f json
```

The report gives the kernel-doc summary and notes; tag and typedef aliases;
definition span, subsystem ownership, related Documentation and source links;
then every member in declaration order. Nested and anonymous structs/unions are
shown hierarchically. Each member retains a normalized source declaration,
parsed type, line span, array dimensions, bitfield width, callback shape,
conditional directive trail, comment-derived public/private marker,
description, and the description's source. The visibility label reflects
kernel documentation comments such as `/* private: */`; C structs do not have
access-control modifiers. `DECLARE_BITMAP`, flexible arrays, cacheline boundary
markers, sysfs callback alternatives, and `struct_group*` families are decoded
while retaining their original source spelling. Reusable tags created by
`struct_group_tagged` / `__struct_group` can be queried directly.

Source documentation comes from kernel-doc or adjacent comments. Known member
macros can also carry explicitly labelled `macro-semantics` explanations from
the parser; those do not inflate source-documentation coverage. Undocumented
fields are labelled rather than filled with invented prose. A partial parse
keeps the raw declaration and prints warnings. The JSON root is always
`{query,index,n_definitions,definitions}`, including for one result, so `--all`
does not change its shape. Each definition identifies its actual `kind`,
nullable C `tag`, honest `c_name`, an exact `selector` when the command syntax
can express one, parse completeness, source-documentation coverage, separately
counted parser-supplied explanations, ownership evidence, and source
availability. Structure data is stored in the index and remains queryable after
the source tree is removed; `show` still requires that tree.

This is a source-structure view, not an ABI layout calculator. It does not claim
byte offsets, padding, alignment, or `sizeof`; those require a concrete
configuration, architecture, compiler ABI, and fully expanded macros.

### `siblings` (`sib`) / `ls`

See [The main idea: "same level"](#the-main-idea-same-level). Combine
`--level`, `--kinds`, `--sort`, `--grep`, `--exported`, `--static-only` /
`--no-static`, and `--with-subsystem` / `-S`.

A directory's neighbours often do *not* share a subsystem. `-S` makes that
visible: four files in `block/` belong to the block layer, BFQ, cgroup blkio,
and SED Opal respectively.

```bash
ka ls mm --kinds file --sort lines -n 5
ka ls mm/page_alloc.c --kinds function --grep alloc
ka ls security --kinds dir -S
```

### `tree`

Draws a directory tree. `-d N` is visual depth (default 2). `--files`
includes files at that same depth — `ka tree mm -d 1 --files` is the
children of `mm/`, not everything under it. A top-level file
(`ka tree Makefile`) trees the kernel root. `-f json` is a flat list.

```bash
ka tree net -d 1
ka tree kernel/sched -d 1 --files
ka tree rust -d 1                 # Rust crate layout; no C symbols in those files
ka tree virt -d 1
```

### `find`

Searches **symbols** by name. Default is a **case-insensitive** substring
(SQL `LIKE`, ASCII) with a limit of 50. Pass `-n 0` for every hit.
`--exact` (`=`) and `--glob` (`GLOB`) are case-sensitive, matching C.

| Flag | Match |
| --- | --- |
| (none) | substring: `sendmsg` hits `tcp_sendmsg` |
| `--prefix` | `tcp_` hits `tcp_sendmsg`, not `xtcp_…` |
| `--glob` | `tcp_*msg`, `sys_*` (shell glob, not regex) |
| `--exact` | the name is exactly this |

`--kinds` here is restricted to symbol kinds. `--exported`, `--static-only`,
`--grep` (regex on the name, after the search) all work. Each hit is
labelled with its subsystem; paths that only match `THE REST` are labelled
with the top-level area instead (`Core kernel`, `Tools`, …).

```bash
ka find tcp_sendmsg --exact
ka find __alloc_pages --prefix
ka find 'sys_*' --glob --kinds syscall
ka find sendmsg --kinds function --exported
ka find GFP_KERNEL --kinds macro --exact
ka find kthread --exact
#   function  kthread  kernel/kthread.c             Core kernel
#   struct    kthread  kernel/kthread.c             Core kernel
#   function  kthread  drivers/block/aoe/aoecmd.c   ATA OVER ETHERNET
```

### `path` / `show`

`path` prints the absolute path of a folder, file, or symbol on disk:

```bash
vim   "$(ka path tcp_sendmsg)"
code -g "$(ka path tcp_sendmsg --line)"    # /.../net/ipv4/tcp.c:1409
grep -rn lock_sock "$(ka path net/ipv4)"
```

`--line` appends `:LINE` for symbols. This needs the exact source tree recorded
in the index (the managed tree under `kernels/` or the original `--src` tree);
the requested indexed member must still exist there. `info`, `siblings`,
`find`, `docs`, and `locate` still work from the snapshot when source is missing
(and `info` reports the recorded path as missing). `web` also remains
source-independent when the index records an upstream release reference.

`show` prints source without leaving the terminal. Given a symbol, it prints
exactly that symbol:

```
$ ka show tcp_sendmsg
net/ipv4/tcp.c:1409  tcp_sendmsg   [NETWORKING [TCP]]   [Linux 6.18.46]
  1409 int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
  1410 {
  1411 	int ret;
  1412
  1413 	lock_sock(sk);
  1414 	ret = tcp_sendmsg_locked(sk, msg, size);
  1415 	release_sock(sk);
  1416
  1417 	return ret;
  1418 }
```

```bash
ka show tcp_sendmsg -C 5                 # 5 lines of context either side
ka show net/ipv4/tcp.c -L 1409:1418      # a line range from a file
ka show net/ipv4/tcp.c                   # the whole file (capped at 2 MB)
ka show tcp_sendmsg --bare               # no header, no line numbers
```

Whole files larger than 2 MB (generated blobs, huge headers) need
`--lines N:M` or `$EDITOR "$(ka path …)"`. Binary files are refused.

### `web` / `docs`

For an index built from a matching authoritative kernel.org archive, `web`
prints version-reference URLs for the target on Bootlin Elixir,
git.kernel.org, GitHub, and—for supported `Documentation/*.rst`, `*.txt`, and
`*.md` files—docs.kernel.org. Nothing is opened; pipe into `open` / `xdg-open`
if you want a browser. Local, vendor, `--no-verify`, and nonmatching archive
sources do not claim upstream URLs merely because their version string looks
familiar. The managed tree is content-checked when the index is built, but the
links identify the recorded release tag rather than monitoring that tree
afterward; later local edits can make current contents or line numbers differ
from the index and upstream reference.

```
$ ka web tcp_sendmsg

net/ipv4/tcp.c:1409  tcp_sendmsg   [Linux 6.18.46]
  elixir  https://elixir.bootlin.com/linux/v6.18.46/source/net/ipv4/tcp.c#L1409
  ident   https://elixir.bootlin.com/linux/v6.18.46/ident/tcp_sendmsg
  git     https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/net/ipv4/tcp.c?h=v6.18.46#n1409
  github  https://github.com/gregkh/linux/blob/v6.18.46/net/ipv4/tcp.c#L1409
```

`ident` is Elixir's cross-reference for the symbol name.
`--url elixir|ident|git|github|docs` prints a single URL. Three-part upstream
versions (6.18.46) use the stable tree and `gregkh/linux`; two-part versions
(7.2) use torvalds. docs.kernel.org is versioned by major.minor (`v6.18`), not
the patch level. `ka web` reports that no upstream release-reference URL is
available for a locally supplied or vendor source tree.

```bash
open "$(ka web tcp_sendmsg --url elixir)"
open "$(ka web Documentation/mm/index.rst --url docs)"
```

`docs` lists `Documentation/` files that belong with a target. Ranking combines
several independent signals before applying `--limit`: an exact/contained
Documentation path, primary-owner claims, semantic names from the target and
its declarations, path terms, and known code-to-doc area aliases. This keeps a
specific guide ahead of an incidental broad-owner match. Bare names like `bpf`
mean the *area* (`kernel/bpf/`), not the LSM hook variable of the same name.

```bash
ka docs mm
ka docs bpf                 # notes that it used kernel/bpf/
ka docs kernel/bpf
```

### `locate`

Resolves one target in **every** built index, so you can see a symbol move
between the LTS you are running and mainline. The version from `ka use` or
`-K` is listed first and marked `*`. `--db` limits the search to that file.

```
$ ka locate tcp_sendmsg

tcp_sendmsg  across 1 index  * = Linux 6.18.46

  * 6.18.46  function   net/ipv4/tcp.c:1409       NETWORKING [TCP]
```

Build a second version when you want a side-by-side comparison.

### `trace`

Maps a kernel oops, an ftrace stack, gdb frames, or a list of names to a
file, a line and a subsystem. It does **not** need a call-graph index.

```bash
$ dmesg | ka trace
$ ka trace tcp_sendmsg __alloc_pages_noprof kthread

Backtrace across 3 frames (Linux 6.18.46)

  #0  tcp_sendmsg           net/ipv4/tcp.c:1409       NETWORKING [TCP]
  #1  __alloc_pages_noprof  mm/page_alloc.c:5268      MEMORY MANAGEMENT - PAGE ALLOCATOR
  #2  kthread               kernel/kthread.c:380      Core kernel  (+2 more defs)

  Areas touched
    Networking               1 frame
    Memory management        1 frame
    Core kernel              1 frame
```

`(+N more defs)` means the name resolved to several definitions (common for
architecture helpers). `-f json` is the same data for scripts. `-n` caps the
number of frames (`0` = all; default 100).

### `subsystems` / `subsystem`

`subsystems` lists every section parsed out of `MAINTAINERS`, including
metadata-only sections which currently claim no files. It distinguishes files
the section **claims** from files for which it is the most specific
(`primary`) match. `--grep` is a regex on the name;
`--sort size|claimed|primary|name`; `-n` limits the list (`0` = all).

```bash
ka subsystems --grep '^SCHED'
#      53  Maintained       SCHEDULER
#      34  Maintained       SCHEDULER - SCHED_EXT
ka subsystems --sort size -n 10
```

`subsystem NAME` is the detail view: all recorded contact and project metadata,
claimed/primary file counts, and the directories where its descendant files
are concentrated. Directory rows report primary files, claimed files, and
coverage of that directory rather than pretending that a mixed directory has
one owner. A unique substring is enough (`ka subsystem SCHEDULER`). `--files`
lists every claimed file (can be thousands). `-n 0` shows every directory.

### `calls`

Requires an index built with `--with-calls`. Shows what a function calls, or
with `--callers`, what calls it. Default 200 rows; `-n 0` is all.

```bash
ka calls tcp_sendmsg
# lock_sock, tcp_sendmsg_locked, release_sock

ka calls tcp_sendmsg --callers
# tcp_bpf_sendmsg  (the BPF sockmap hook)
```

Invocation names are resolved conservatively. A unique callable in the same
translation unit wins: `same_file` means the definition is in the caller's
file, while `included_source` means it came from a transitively included C
member. Literal quoted includes and angle/quoted includes resolved through one
exact Kbuild `-I` path are supported. If one member is included by several
top-level sources, every translation-unit instance must reach the same
identity; the effective build domain comes from each root, not from the
member's pathname. Otherwise, only one non-static callable in every compatible
root context can become `unique_global`. Indexed macros, function-pointer
objects/variables, static header alternatives, architecture alternatives, and
duplicate definitions block a guessed identity. Every row retains one of these
outcomes in the `resolution` column. `occurrences` summarizes the parser's
source-level evidence as direct (`d`), indirect (`i`), and macro (`m`) counts;
these can coexist for the same invocation name, while `resolution` describes
the direct occurrences when any exist:

| Resolution | Meaning |
| --- | --- |
| `same_file` | one callable identity in the caller's source file |
| `included_source` | one callable identity in an included `.c` member of the translation unit |
| `unique_global` | one compatible non-static identity across files |
| `ambiguous` | relevant definitions or blockers exist, but do not prove one identity |
| `macro` | the invocation is an active in-file macro, or only indexed macro evidence exists |
| `indirect` | a pointer/object binding, explicit dereference, or member/ops-table expression is invoked |
| `unresolved` | no indexed identity or blocker establishes what the name denotes |

Reverse lookup uses only the selected symbol's resolved identity, so unrelated
static functions with the same name are not mixed together. If a target itself
has several callable definitions, use `path:symbol` across files or `path:line`
for duplicates within one file rather than accepting a guessed definition.
Outgoing rows that have no concrete callee identity retain their invocation
evidence with `kind` shown as `?`; the `macro`, `indirect`, `ambiguous`, and
`unresolved` resolution labels explain why. The `--kinds` filter is restricted
to the callable result identities `function` and `syscall`.

Cross-file compatibility follows available build evidence. Kbuild
`hostprogs`/`userprogs`/`tprogs-y` and their multi-object declarations form
independently linked program domains; boot/compressed images, vDSO-style
images, EFI stub code, and `.bpf.c` programs are kept out of the vmlinux
namespace. Unmodelled auxiliary sources (notably `tools/`, `scripts/`, and
Documentation helpers) are isolated rather than linked by spelling, and one
architecture is never bound to another. Architecture code may use its own
domain and generic kernel identities, subject to architecture alternatives
blocking an unsafe choice. Common and architecture-header identities also
block unsafe promotion inside separate images without making vmlinux globals
linkable there. Literal or locally expanded Kbuild compile/link object lists,
plus conservative literal `target.o: source.c` and program-link rules, preserve
a standalone context for dual-use sources that are also included as `.c`
members. Member definitions inherit each root object's domain.

This is intentionally a lower-bound graph, not a whole-program C analysis.
Calls through local/parameter/file-scope pointer objects, explicit
dereferences, and member/ops-table expressions are retained as `indirect`, but
their runtime targets are not inferred; code generated entirely by macros does
not become concrete edges. A source object explicitly linked into several
independent Kbuild programs is isolated, so some valid cross-file program edges
can remain unresolved. These gaps stay visible in `ka stats` and
`ka relationships` coverage instead of being promoted to plausible-looking
targets.

### `relationships` (`rels`)

Shows how a subsystem relates to others using two separate forms of evidence:

- **ownership overlap**: MAINTAINERS sections which claim the same files,
  including coverage and Jaccard similarity;
- **direct C invocation flow**: identity-resolved calls crossing disjoint sets
  of primary file owners, with caller/callee counts and resolution coverage.

```bash
ka relationships SCHEDULER
ka rels kernel/futex --via ownership
ka relationships 'MEMORY MANAGEMENT - CORE' --direction outgoing --min-calls 5
```

The target can be a subsystem name or a directory, file, or symbol that
resolves to one. Use `--via ownership|calls|all`,
`--direction incoming|outgoing|both`, `--include-internal`, `--min-shared`,
`--min-calls`, and `-n` (per ownership/direction group). JSON and CSV retain
the two evidence types as distinct records. Calls whose other endpoint has only
the catch-all `THE REST` owner—or no primary owner at all—are labelled
unclassified rather than presented as a subsystem. If source and target files
share even one co-primary owner, the call is internal to that shared boundary
and does not manufacture a cross-subsystem relationship between their other
owners. Only `same_file`, `included_source`, and `unique_global` identities
contribute flow edges; the other outcomes remain explicit coverage counts. Call
flow requires an index built with `--with-calls`.

## A tour across subsystems

The same few commands work everywhere. What changes is the path you hand them.
Numbers are from 6.18.46.

### Memory management

`mm/` is one directory but several `MAINTAINERS` sections (CORE, PAGE
ALLOCATOR, MEMORY MAPPING, PAGE CACHE, …). `info` and `find` make that split
visible:

```bash
ka info mm                    # 145 files here, 197 in the subtree
ka ls mm --kinds file --sort lines -n 5
# slub.c 10095   hugetlb.c 8022   vmscan.c 7938   page_alloc.c 7704   memory.c 7353
ka find __alloc_pages --prefix -n 4
#   macro     __alloc_pages         include/linux/gfp.h   MEMORY MANAGEMENT - CORE
#   function  __alloc_pages_noprof  mm/page_alloc.c:5268  MEMORY MANAGEMENT - PAGE ALLOCATOR
ka find GFP_KERNEL --kinds macro --exact
#   include/linux/gfp_types.h:378   MEMORY MANAGEMENT - CORE
#   plus copies under include/linux/raid/ and tools/
ka docs mm                    # Documentation/mm/*.rst first
```

The real page allocator is `__alloc_pages_noprof`; `__alloc_pages` is a
wrapper macro. Searching rather than guessing the name is the reliable way
across releases.

### Scheduler, next to the rest of `kernel/`

```bash
ka siblings kernel/sched -n 8
# bpf/  cgroup/  configs/  debug/  dma/  entry/  events/  futex/
ka ls kernel/sched --kinds file --sort lines -n 5
# fair.c 14196   core.c 10906   ext.c 6994   sched.h 3929   deadline.c 3740
ka info schedule              # kernel/sched/core.c:7027, subsystem SCHEDULER
ka subsystem SCHEDULER        # who to mail, which git tree
ka relationships SCHEDULER    # ownership overlap + cross-subsystem calls
```

`kernel/futex` is a useful test of directory ownership: its `F:` rule names
the files immediately below it, and `info` correctly rolls those file matches
up to the dedicated FUTEX SUBSYSTEM instead of glob-matching the directory
string itself.

### Networking

```bash
ka siblings net/ipv4/tcp.c --sort lines -n 5
# tcp_input.c  7594 lines   — receive path, the biggest file
# tcp_output.c 4599 lines   — send path
# nexthop.c, udp.c, tcp_ipv4.c
ka siblings tcp_sendmsg                  # other functions in tcp.c
ka siblings tcp_sendmsg --exported -n 5  # the module-visible API of that file
ka calls tcp_sendmsg                     # needs --with-calls
ka calls tcp_sendmsg --callers
```

Sorting a directory by line count is a decent way to find where the work is.

### Block layer — neighbours that do not share an owner

```bash
ka siblings block/bio.c --sort lines -n 4 -S
# bfq-iosched.c   BFQ I/O SCHEDULER
# blk-mq.c        BLOCK LAYER
# blk-iocost.c    CONTROL GROUP - BLOCK IO CONTROLLER (BLKIO)
# sed-opal.c      SECURE ENCRYPTING DEVICE (SED) OPAL DRIVER
```

`-S` is doing the interesting work here: same folder, four subsystems.

### Security — one directory, one LSM each

```bash
ka ls security --kinds dir -n 6 -S
# apparmor/   APPARMOR SECURITY MODULE
# bpf/        BPF [SECURITY & LSM]
# integrity/  Extended Verification Module (EVM)
# ipe/        INTEGRITY POLICY ENFORCEMENT (IPE)
# keys/       KEYS/KEYRINGS
# landlock/   LANDLOCK SECURITY MODULE
```

### Drivers — who do I email about this NIC?

```bash
ka info drivers/net/ethernet/intel/igb
# INTEL ETHERNET DRIVERS   (precise)    intel-wired-lan@lists.osuosl.org
# NETWORKING DRIVERS       (umbrella)   netdev@vger.kernel.org
```

Both are correct. `MAINTAINERS` asks you to prefer the most precise area;
`info` lists it first.

### BPF, io_uring, crypto, virt, rust, init

```bash
ka info kernel/bpf
# BPF [GENERAL], Alexei Starovoitov, bpf@vger.kernel.org
ka ls kernel/bpf --kinds file --sort lines -n 5
# verifier.c  25165 lines — start here
ka docs bpf                 # Documentation/bpf/, not the LSM hook named bpf
ka show sys_bpf             # SYSCALL_DEFINE3 rebuilt as sys_bpf

ka info io_uring            # IO_URING, Jens Axboe, 78 files
ka ls io_uring --kinds file --sort lines -n 5
# io_uring.c  4144 lines
ka find 'sys_io_uring*' --glob --kinds syscall
# sys_io_uring_enter / _setup / _register

ka info crypto
ka ls crypto --kinds file --sort lines -n 5
# testmgr.h is a 1.4 MB generated-looking header; skip it

ka info virt/kvm            # Area: Virtualization
ka tree virt -d 1           # kvm/  lib/

ka tree rust -d 1           # crates; .rs files are in the index with no symbols
ka info init                # Area: Init — start_kernel() lives here
ka info ipc                 # System V IPC
ka ls . --kinds dir         # every top-level area of the tree
```

### Syscalls and arch

`SYSCALL_DEFINEn` macros are reassembled into the real symbol names
(`sys_bpf`, `compat_sys_iopl`), so syscalls are searchable even though they
are not written as ordinary C functions:

```bash
ka find 'sys_*' --glob --kinds syscall -n 8
# sys_bpf   kernel/bpf/syscall.c   BPF [CORE]
# sys_brk   mm/mmap.c              MEMORY MAPPING
# sys_bind  net/socket.c           NETWORKING [SOCKETS]
ka siblings arch/x86              # every other architecture port
ka tree arch/x86 -d 1
ka find copy_from_user --exact    # include/linux/uaccess.h, plus tools/ copies
```

## Typical workflows

**I have an oops.** `dmesg | ka trace`, then `ka show kthread` and
`ka web kthread --url elixir`.

**I want to email the right list.** `ka info drivers/net/ethernet/intel/igb`
— the first `MAINTAINERS` section is the one to use — or
`ka subsystem 'INTEL ETHERNET'`.

**I am reading code in the browser.**
`open "$(ka web tcp_sendmsg --url elixir)"` (definition) or `--url ident`
(every use of the name). For the handbook:
`open "$(ka web Documentation/mm/index.rst --url docs)"`.

**I want the docs that go with this code.** `ka docs mm`, `ka docs bpf`,
`ka show Documentation/mm/index.rst`.

**I am following data through a subsystem boundary.** Start with
`ka struct usb_device`, follow referenced `struct ...` types with another
`ka struct`, then use the listed owner, Documentation files, and source links
to connect the data representation to its subsystem.

**Open it in an editor.** `vim "$(ka path tcp_sendmsg)"` or
`code -g "$(ka path tcp_sendmsg --line)"`. `--line` appends `:LINE`.
`path` / `show` need the exact source tree recorded in the index (normally
under `kernels/`, or the original `--src` tree); most other commands do not.

## Controlling the output

Listing commands (`siblings`, `ls`, `find`, `calls`) share these controls:

| Option | Values / meaning |
| --- | --- |
| `--format`, `-f` | `table` (default), `plain`, `names`, `json`, `csv`, `tree` |
| `--columns`, `-c` | table/JSON/CSV only; comma-separated, ordered: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature,occurrences,resolution` |
| `--limit`, `-n` | max rows; `0` = all (`find` defaults to 50, `calls` to 200) |
| `--sort` | `name`, `path`, `kind`, `line`, `size`, `lines` (size/lines sort descending; a symbol-only result rejects `size`, so use `lines` for definition span) |
| `--grep`, `-g` | keep only names matching a regex (case-insensitive) |
| `--with-subsystem`, `-S` | add a subsystem column to table/JSON/CSV output |
| `--kinds`, `-k` | `dir,file,function,syscall,struct,union,enum,typedef,macro,variable,prototype` or shortcuts `all`, `symbols`, `paths`, `functions`, `types` |
| `--exported` | only `EXPORT_SYMBOL`'d symbols |
| `--static-only` / `--no-static` | keep only / drop `static` symbols |

`find` substring/prefix matching is case-insensitive; `--exact` and `--glob`
are not, and the three explicit matching modes are mutually exclusive. Listing
JSON stays an array of objects so `jq '.[].name'` works; each row also has an
`index` field naming the kernel version. With `--columns`, JSON is projected to
those fields too. `plain`, `names`, and `tree` have fixed shapes and reject
column controls instead of silently ignoring them. `--static-only` and
`--no-static` are mutually exclusive.

For commands that read an index, global options work before **or** after the
subcommand:

| Option | Meaning |
| --- | --- |
| `-K`, `--kernel` | which index (`6.18.46`, or a unique prefix like `6.18`) |
| `--db PATH` | a specific index file |
| `--color` | `auto` (default), `always`, `never` |

`-K` and `--db` are mutually exclusive. Index-selection options are rejected
by lifecycle commands such as `build`, `indexes`, `use`, and `remove`, where
they would otherwise have no meaning.

`names` and `plain` print bare values with no header or footer, so they pipe
cleanly. `plain` is `path` for files/dirs and `path:line:name` for symbols —
the same shape grep prints, so editors' quickfix lists understand it.

```bash
ka siblings kernel/sched -f names | head
ka ls net/ipv4 --kinds function -f json | jq -r '.[].name'
ka find tcp_ --prefix -f csv > tcp-symbols.csv
ka locate tcp_sendmsg -f json | jq -r '.[] | "\(.version) \(.path):\(.line)"'
ka ls mm/page_alloc.c --kinds function --grep 'alloc' -f plain
```

Unknown `--columns` or `--kinds` values are rejected with the valid list,
rather than silently ignored. A bad `--grep` regex is a one-line error, not a
traceback. `resolution` is populated by `calls`; its `--kinds` filter accepts
only resolved function/syscall result identities, while unfiltered outgoing
results also retain `?` rows for macro, indirect, ambiguous, and unresolved
invocation evidence.

## How subsystems are determined

The kernel ships `MAINTAINERS`, the authoritative statement of who owns what.
`kernel-atlas` parses its ~3,100 sections rather than hardcoding a table, so
the mapping is correct for the version you indexed.

Pattern matching follows Linux's `scripts/get_maintainer.pl` semantics rather
than a generic filesystem glob. `F:` supplies positive path evidence, `N:` is
a regex searched against the whole repository-relative path, and an `X:` match
cancels that section's claim even if one of its `F:`/`N:` rules matched.

```
F: drivers/net/       every file below that directory
F: drivers/net/*      same-depth files immediately inside drivers/net
F: include/drm/drm    same-depth paths beginning with include/drm/drm
F: fs/**/*.c          ** explicitly permits additional path components
X: drivers/net/foo/   remove this section from that subtree
N: (?:^|/)imx[^/]*    regex evidence searched against the full path
```

The upstream matcher translates a single `*` broadly, then enforces equal slash
depth for non-directory expressions; that combination is why
`drivers/net/*` does not reach `drivers/net/ethernet/vendor.c`. A trailing
slash is recursive, as is a literal path which names an existing directory even
when the rule omitted the slash. `**` disables the depth restriction. `?` and
bracket classes such as `[ch]` follow the same matcher, and literal file rules
are start-anchored prefixes at the same depth rather than exact-string matches.

Matches receive deterministic specificity scores. Every section tied for the
highest score is a **primary** owner; no arbitrary single winner is invented.
All lower-ranked claims are retained too, for `info`, ownership-overlap
analysis, and `subsystem --files`.

`F:` and `N:` rules describe files, not ownership of a directory object.
Directory views therefore aggregate the actual claims and primary owners of
all descendant files, including each section's claimed count, primary count,
and coverage. A directory has a singular subsystem only when exactly one
non-catch-all section is represented among its primary owners and it covers
every descendant file; otherwise it is mixed or includes unclassified content.
That makes a specific directory such as `kernel/futex/` discoverable while a
mixed boundary such as `drivers/net/wireless/ath/` honestly shows its several
owners and their coverage.

The catch-all `THE REST` section claims every file in the tree, so it is never
*shown* as the answer when a specific section exists. Otherwise the
plain-English *Area* of the top-level directory is used (`mm/` → Memory
management, `kernel/` → Core kernel). `find` follows the same rule, so a hit in
`tools/` is labelled `Tools` rather than `THE REST`.

## How parsing works, and its limits

C is parsed with [tree-sitter](https://tree-sitter.github.io/). It does **not**
run the C preprocessor, and the kernel is extremely macro-heavy, so several
idioms are handled explicitly:

- `SYSCALL_DEFINE3(open, ...)` does not parse as a function: the macro call
  becomes a statement and the body a *sibling* block. It is rebuilt as
  `sys_open`, including its call edges. `COMPAT_SYSCALL_DEFINE4(...)` becomes
  `compat_sys_…`, a different symbol.
- The canonical `EXPORT_SYMBOL*` family—including GPL, namespace, and
  per-CPU variants—marks a symbol as available to modules.
- `DECLARE_WORK(name, fn)`, `DEFINE_MUTEX(name)`, `LIST_HEAD(name)`,
  `DECLARE_BITMAP(name, n)`, `DEFINE_PER_CPU(type, name)` declare `name`.
- Structure bodies retain direct and nested members, comma declarators,
  anonymous aggregates, arrays, flexible arrays, bitfields, callbacks,
  source attributes/qualifiers, preprocessor directive trails, comment-derived
  visibility markers, typedef aliases, and matching kernel-doc/adjacent
  comments. Aggregate direct-member counts do not include the children of
  nested structs or unions.
- Inside structures, `DECLARE_BITMAP(name, n)` is represented as an
  `unsigned long` array and `DECLARE_FLEX_ARRAY(type, name)` (including its
  underscored form) as a flexible array; their raw macro declarations remain
  authoritative.
- Canonical sysfs attribute families such as `DEVICE_ATTR_*`, `DRIVER_ATTR_*`,
  `BUS_ATTR_*`, `CLASS_ATTR_*`, `BIN_ATTR_*`, and the supported sensor/IIO
  forms are recorded under the backing object name they actually generate.
  Unknown wrapper macros are skipped rather than turning an argument into a
  fictitious variable.
- Trailing attribute macros (`____cacheline_aligned_in_smp`) are not mistaken
  for variable names; their original annotation remains attached to the real
  member. Cacheline group macros are represented by the zero-length marker
  fields they generate (and aligned-end padding where applicable).
- `__SYSFS_FUNCTION_ALTERNATIVE` keeps both callback spellings beneath a
  configuration-dependent aggregate instead of falsely choosing struct or
  union layout. `struct_group*` keeps its mirrored member hierarchy, and tagged
  forms also create an independently resolvable structure definition.
- Declarations inside `#ifdef` *in a function body* are locals, not file-scope.
- `int (*fp)(void);` is a function-pointer variable; `int fp(void);` is a
  prototype. The two are told apart.

Known limits:

- Code inside `#if` branches is indexed regardless of `.config`. Structure
  reports label each alternative with its directive trail; they do not imply
  that mutually exclusive members coexist in one compiled layout.
- Unknown member-generating macros cannot be expanded without the preprocessor.
  Their normalized raw invocations are retained as explicit macro evidence,
  and the aggregate is marked partial when member identity remains uncertain.
- Aggregate summaries/descriptions and member documentation are source
  evidence. Explicitly labelled `macro-semantics` member explanations are kept
  separate, and missing or unmatched source documentation remains visible.
- Structure reports cannot determine byte offsets, padding, alignment, or
  `sizeof` without a selected configuration, target ABI, compiler, and macro
  expansion.
- Interactive target lookup may rank one likely definition of a name that is
  defined per architecture; `ka find --exact <name> -n 0` shows all of them.
  Call-identity resolution is stricter and never guesses one architecture.
- Functions generated entirely by macros other than the ones above are missed.
- Conditional/configuration-specific export wrappers are not marked exported;
  the index marks literal uses of the canonical `EXPORT*_SYMBOL*` family.
- Literal quoted `.c` includes and angle/quoted `.c` includes reached through
  one exact literal Kbuild `-I` path are recognized as translation-unit
  membership, including source-tree-root spellings used by vDSO code. Ambiguous
  or computed includes and build-generated aggregation are not guessed. A
  member used by several roots yields a concrete edge only when all roots
  agree.
- Standalone evidence for a source that is also included comes from recognized
  Kbuild object-list families (`obj-*`, `lib-*`, and composite `*-y`/`*-m`/
  `*-objs` lists) in `Makefile`, `Kbuild`, and tools `Build` files. Local
  variables and pure `addprefix` object expansion are followed; arbitrary make
  functions are not. Conservative literal object compile and program-link
  dependency rules are also recognized; custom recipes remain a lower-bound
  limitation.
- Header inclusion contexts are not modelled. Calls may resolve to identities
  in the same header, but cross-file candidates for a header-origin call remain
  ambiguous or unresolved rather than borrowing the header pathname's build
  domain.
- The call graph resolves direct identifiers by translation-unit or compatible
  domain-local unique-global identity. Macro, variable/function-pointer,
  static-header, cross-tools, and cross-architecture alternatives prevent a
  guessed identity. Calls through local, parameter, or file-scope objects,
  explicit dereferences, and ops/member expressions are retained as `indirect`
  evidence without inventing their runtime target.
- C, headers, and shipped `.c_shipped`/`.h_shipped` inputs are parsed. Assembly
  and Rust files appear as files, without symbols.
- C/H inputs over 4 MiB are line-counted but not sent to tree-sitter, because
  generated headers can contain millions of definitions. Their status is
  recorded as `skipped_oversize` and included in build statistics.
- Symlinks are represented in the file map but are not followed or parsed, so
  directory cycles and links outside the selected tree cannot broaden the
  index unexpectedly.
- A read or parser failure is stored on the affected file and counted in the
  build summary instead of being reported as a successful parse.

## Troubleshooting

**"no index built yet"** — `ka build lts` once. Everything else needs an index.

**"no index for X"** — `ka indexes` lists what you have. Prefixes must be
unique *and* land on a version-component boundary (`-K 6` is ambiguous if you
have both 6.12 and 6.18; `-K 6.1` does not select `6.18.46`).

**"this index has no call graph"** — rebuild that version with
`ka build X --with-calls --force`. If the query selected an explicit custom
`--db`, use the exact command printed by the error: when its recorded source is
available, the hint preserves that `--src` tree and `--output` database instead
of rebuilding an unrelated managed index. `ka trace` does not need a call graph.

**"the source for Linux X is not on disk"** — `path` and `show` need the exact
tree recorded when the index was built. Other commands do not. `ka build X
--force` uses the valid managed tree at `kernels/linux-X`, downloading it when
absent; a removed custom `--src` tree must be restored or re-indexed.

**"source destination … is not a complete Linux tree"** — an entry already
occupies the managed source path, but it cannot be proven to belong to
kernel-atlas. It is never deleted automatically. Inspect it, then move or
remove that exact entry yourself before retrying the build.

**"not the pristine tool-owned source recorded by this index"** — a downloaded
tree that once matched the index has been edited, replaced, or lost its valid
identity sidecar. `remove --source` keeps both the index and tree rather than
risk deleting unrelated study work. Restore the exact indexed tree and retry,
or manage the tree yourself and then remove only the index without `--source`.

**"is this index internally consistent?"** — `ka check` (or the identical
`ka doctor`) runs the deep count, topology, ownership, and call-identity audit.
This is especially useful for a copied or custom `--db`.

**"is N bytes; pass --lines"** — `show` will not dump a file bigger than 2 MB
whole. Use `--lines N:M`, or open it with `$EDITOR "$(ka path …)"`.

**"no Documentation/ files related to …"** — that area has no matching
`Documentation/` path. Try `ka ls Documentation --kinds dir` or `ka docs mm`.

**"pinned version has no index any more"** — you `remove`d the version `use`
was pointing at (or the pin is stale). `ka use --clear` or `ka use <other>`.

**"not a usable index" / "unsupported index schema"** — the file is partial,
corrupt, or was built with an incompatible schema. Rebuild that version with
`--force`. Completed builds are validated and moved into place atomically.

**The index disagrees with the file I just edited** — the index is a snapshot.
Rebuild with `--force` after editing its managed or custom source tree. An
edited managed cache is intentionally recorded as local source, so that rebuilt
index does not claim upstream `web` links for content it can no longer attest.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The tests build a small synthetic kernel tree (`tests/fixture.py`) with its own
`MAINTAINERS`, plus a throwaway `KERNEL_ATLAS_HOME` for `use` / `remove`, so
they need no network and never touch your real indexes. Pytest discovery is
confined to `tests/`; downloaded kernel selftests are never collected.

### Layout

| File | Purpose |
| --- | --- |
| `config.py` | where kernels, indexes, and the `use` pin live |
| `kernelsrc.py` | kernel.org releases, resumable downloads, checksum policy, atomic extraction, lifecycle locks, and persistent source identity/digests |
| `cparse.py` | tree-sitter ownership plus general function, call, declaration, and macro extraction |
| `cparse_models.py` | parser symbol kinds and shared symbol/member records |
| `cparse_shared.py` | kernel-C syntax tables and source/tree helpers shared by parsers |
| `aggregate_parse.py` | struct/union/enum definitions, member shapes, source documentation, and aggregate macro idioms |
| `maintainers.py` | `MAINTAINERS` parsing and path → subsystem matching |
| `indexer.py` | tree walk, parallel parsing, subsystem attachment, atomic build |
| `db.py` | SQLite schema plus structural and semantic integrity validation |
| `call_resolution.py` | conservative direct-call identity resolution and evidence accounting |
| `query.py` | generic target resolution, container/level model, search, and compatibility facade |
| `query_models.py` | shared query result, target, resolution, and scope records |
| `query_targeting.py` | target normalization and deterministic candidate ranking |
| `structure_query.py` | strict aggregate resolution, selectors, and nested study payloads |
| `relationships.py` | ownership overlap and resolved cross-subsystem call flow |
| `links.py` | Elixir / git.kernel.org / GitHub / docs.kernel.org URLs |
| `render.py` | generic table / plain / json / csv / tree output and compatibility facade |
| `render_format.py` | terminal-formatting primitives shared by renderers |
| `structure_render.py` | hierarchical human-readable aggregate reports |
| `cli.py` | argument parser, shared command services, public entry point, and compatibility facade |
| `cli_lifecycle.py` | release, build, index selection/removal, statistics, and integrity commands |
| `cli_browse.py` | information, listing, search, subsystem, tree, path, and source commands |
| `cli_aggregate.py` | detailed struct/union study command |
| `cli_calls.py` | backtrace, call-graph, and subsystem-relationship commands |
| `cli_resources.py` | source links, related documentation, and cross-version lookup |

The facade modules (`cparse.py`, `query.py`, `render.py`, and `cli.py`) retain
their established imports and entry points. Parser/query/render feature modules
depend only on shared models and helpers, not their invoking facade. CLI feature
handlers receive shared services from `cli.py` without importing it. The result
has one-way imports, while callers do not need to learn internal module names.
