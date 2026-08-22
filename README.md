# kernel-atlas

A map of the Linux kernel for people who are still learning their way around it.

`kernel-atlas` downloads a kernel release from kernel.org, indexes every
directory, file and C symbol in it, and works out which **subsystem** each path
belongs to by parsing the kernel's own `MAINTAINERS` file. Then you can ask it
questions of the form:

- *What else lives next to this folder / file / function?*
- *Which subsystem owns this symbol, and who maintains it?*
- *I have a function name from an oops. Where is it defined?*
- *Show me the source of this function, or an absolute path I can open in an editor.*

Once an index is built, every query is local and offline.

```
$ ka info tcp_sendmsg

net/ipv4/tcp.c:tcp_sendmsg

  kind         function
  defined in   net/ipv4/tcp.c:1446-1455 (10 lines)
  signature    int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
  linkage      EXPORT_SYMBOL (available to modules)
  on disk      ~/kernel-atlas/kernels/linux-7.2/net/ipv4/tcp.c

  Area: Networking
    Network stack: sockets, TCP/IP, netfilter, per-protocol code.

  Subsystem (from MAINTAINERS)
   * NETWORKING [TCP]   [Maintained]  47 files
       maintainer  Eric Dumazet <edumazet@google.com>
       list        netdev@vger.kernel.org
```

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Where everything lives](#where-everything-lives)
- [Choosing a kernel version](#choosing-a-kernel-version)
- [Deleting indexes](#deleting-indexes)
- [Building indexes](#building-indexes)
- [Naming a target](#naming-a-target)
- [The main idea: "same level"](#the-main-idea-same-level)
- [Commands in detail](#commands-in-detail)
- [A tour across subsystems](#a-tour-across-subsystems)
- [Controlling the output](#controlling-the-output)
- [How subsystems are determined](#how-subsystems-are-determined)
- [How parsing works, and its limits](#how-parsing-works-and-its-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install

Requires Python 3.10+ and roughly 2.5 GB of disk per kernel version
(about 1.6 GB of source plus a 0.7–1 GB index).

```bash
git clone <this repo> && cd kernel-atlas
python3 -m venv .venv
.venv/bin/pip install -e .
```

This creates two equivalent commands, `kernel-atlas` and the shorter `ka`,
**only inside the venv**. Nothing is added to your system `PATH` and no shell
profile is touched. Either activate the venv:

```bash
source .venv/bin/activate
ka info mm
```

or call the full path from anywhere (the shebang pins it to the venv's Python):

```bash
~/kernel-atlas/.venv/bin/ka info mm
```

Deleting `.venv/` removes both commands. Deleting `kernels/` and `indexes/`
reclaims the data. `NO_COLOR` (or `--color never`) turns coloring off.

## Quick start

```bash
ka build lts                  # one-off: download + index the latest LTS
ka use 6.18                   # optional: pin that version as the default
ka info mm                    # what is this directory, who maintains it?
ka siblings kernel/sched      # what sits next to the scheduler?
ka find tcp_sendmsg           # where is this symbol?
ka show tcp_sendmsg           # print its source
dmesg | ka trace              # map a whole backtrace to subsystems
```

## Where everything lives

The kernel source and the index are kept **inside the project directory**, not
in a hidden cache, so the code you are studying sits right next to the tool:

```
kernel-atlas/
├── kernels/
│   ├── linux-6.18.45/      <- real kernel trees: open them, grep them
│   └── linux-7.2/
├── indexes/
│   ├── 6.18.45.db
│   ├── 7.2.db
│   └── .default-version    <- written by `ka use`, gitignored
└── src/kernel_atlas/
```

`kernels/` and `indexes/` are gitignored. Point your editor or `grep` at
`kernels/linux-*/`, or let `ka path` hand you absolute paths into it.

Set `KERNEL_ATLAS_HOME` if you want the data on another disk:

```bash
export KERNEL_ATLAS_HOME=/mnt/big-disk/kernel-atlas
```

## Choosing a kernel version

You can keep several indexes at once. Three knobs pick which one a command
uses, in this order:

1. `--db PATH` — an explicit index file, for scripts and tests.
2. `-K` / `--kernel VERSION` — this command only. A unique prefix is enough
   (`-K 6.18` selects `6.18.45` if that is the only 6.18.x you have).
3. The **default index**, which is:
   - the version you pinned with `ka use`, if that index still exists;
   - otherwise the **highest built version** (7.2 beats 6.18.45). That is
     predictable, unlike "whichever file was touched last".

### `ka use` — pin the default

This is the command that makes "I am studying 6.18 right now" stick, so you do
not have to pass `-K` on every invocation.

```bash
ka use                  # show what is pinned and what is actually active
ka use 6.18             # pin by unique prefix → 6.18.45
ka use 6.18.45          # pin by exact version
ka use --clear          # unpin; go back to "highest built version"
```

`ka indexes` marks the default with a `*`:

```
    VERSION         FILES    SYMBOLS CALLS  SOURCE  BUILT                    SIZE
  * 7.2             94744    4238157 -      yes     2026-08-21T03:03:58    778 MB
    6.18.45         91107    4052310 yes    yes     2026-08-21T03:02:51    923 MB

  * = default index — highest version (pin one with 'kernel-atlas use <version>')
```

A pin is just the file `indexes/.default-version`. If you later `ka remove` that
version, the pin is cleared automatically and commands fall back to the highest
remaining index (with a warning the first time, so it is not silent).

`-K` always wins over the pin, so you can keep 6.18 as default and still peek
at 7.2 for one command:

```bash
ka use 6.18
ka info tcp_sendmsg              # 6.18.45
ka -K 7.2 info tcp_sendmsg       # 7.2, this command only
```

## Deleting indexes

```bash
ka remove 7.2                 # delete indexes/7.2.db; keep kernels/linux-7.2/
ka remove 6.18                # unique prefix works the same as for `use`
ka remove 6.18.45 7.2         # several at once
ka rm 7.2 --source            # also delete kernels/linux-7.2/  (the 1.6 GB)
```

`remove` (alias `rm`) resolves every name *before* deleting anything, so
`ka remove 6.18 6.18.45` is not an error — it is the same index named twice.
SQLite sidecar files (`.db-wal`, `.db-shm`, `.db-journal`) go with the index.

The kernel source is **kept by default**, because rebuilding an index from a
tree that is already on disk takes about a minute and no download. Pass
`--source` only when you also want that disk back. If the deleted index was
the pinned default, the pin is cleared.

This does not ask for confirmation: the version argument is already the
confirmation. `ka indexes` afterwards is the way to see what is left.

## Building indexes

```bash
ka versions                   # live list of releases from kernel.org
ka build lts                  # latest longterm  (best default for learning)
ka build stable               # latest stable
ka build mainline             # newest release
ka build 6.12.104             # any exact version
ka build lts --force          # rebuild over an existing index
```

Version aliases are resolved live against kernel.org, so nothing is hardcoded
and old releases keep working. Downloads are verified against kernel.org's
`sha256sums.asc` and resume if the connection drops. Extraction and indexing
are atomic (scratch directory / scratch file, renamed into place only on
success): an interrupted build cannot leave behind something that looks
finished.

| `build` option | Effect |
| --- | --- |
| `--src PATH` | index a kernel tree you already have (version read from its `Makefile`) |
| `--kinds LIST` | which symbol kinds to index (default: `function,syscall,struct,union,enum,typedef,macro,variable`; add `prototype` if wanted) |
| `--with-calls` | also record the call graph (enables `ka calls`; a few hundred MB extra) |
| `--jobs N` | parser processes (default: one per CPU, max 16) |
| `--output PATH` | write the index somewhere specific |
| `--keep-tarball` | keep the `.tar.xz` after extraction |
| `--no-verify` | skip the checksum (not recommended) |
| `--quiet` | no progress output |

Scale, for orientation: Linux 7.2 on a laptop is ~6,200 directories, ~95,000
files, ~4.2 million symbols, ~3,300 subsystems, about a minute to index.
Most of the size is the kernel's ~3 million macros;
`--kinds function,syscall,struct,enum,typedef` is much smaller if you do not
need them.

`ka stats` summarises an index: symbol counts by kind, and the largest
top-level areas of the tree (`drivers`, `arch`, `net`, `mm`, …).

## Naming a target

Every command that takes a `target` accepts all of these forms:

```bash
ka info mm                           # a folder
ka info mm/page_alloc.c              # a file
ka info mm/page_alloc.c:__alloc_pages_noprof   # a symbol in a known file
ka info tcp_sendmsg                  # a bare symbol name
ka info tcp.c:tcp_sendmsg            # basename:symbol — the symbol picks the right tcp.c
ka info mm/page_alloc.c:5333         # whatever symbol spans that line number
ka info page_alloc.c                 # a bare filename (reports if ambiguous)
ka info sched                        # a bare directory name
ka info .                            # the kernel root
```

When a name is ambiguous (some symbols have a definition per architecture),
the most likely candidate is chosen — real definitions beat prototypes,
non-static beats static, shorter paths beat longer ones — and the alternatives
are listed. Typos get "did you mean" suggestions for both symbols and paths.
`net/ipv4/tcp.c:no_such_fn` tells you the file exists but that symbol does not,
instead of guessing what the whole string might mean.

## The main idea: "same level"

This is the query the rest of the tool is built around.

Every target lives in a **container**:

| Target | Its container |
| --- | --- |
| a folder | its parent directory |
| a file | its directory |
| a symbol | the file it is defined in |

`ka siblings` lists the other members of that container. `--level` widens the
container outwards, and `--kinds` chooses *what* to list from it, independently
of what you asked about. So you can ask for the functions next to a file, or
the files next to a function.

| `--level` | Scope becomes |
| --- | --- |
| `auto` (default) | the natural container above |
| `file` | the containing file (meaningful for symbols) |
| `dir` | the containing directory |
| `subtree` | that directory and everything beneath it |
| `subsystem` | every file the target's subsystem claims, even if they are not under the same folder |
| `tree` | the entire kernel |

```bash
ka siblings kernel/sched                              # other core-kernel dirs
ka siblings net/ipv4/tcp.c                            # other files in net/ipv4/
ka siblings tcp_sendmsg                               # other functions in tcp.c
ka siblings tcp_sendmsg --level subsystem             # every NETWORKING [TCP] function
ka siblings net/ipv4/tcp.c --kinds function           # functions next to a *file*
ka siblings tcp_sendmsg --level dir --kinds file      # files around a *symbol*
ka siblings kernel/sched --include-self               # keep the target, marked >
```

`ka ls` is the complement: it looks *inside* rather than *beside* (children of
a folder, or symbols defined in a file). `--limit` / `-n` counts *other* rows,
so `-n 5` is five siblings, not four plus the thing you asked about.

## Commands in detail

### `versions`

Talks to kernel.org and prints the current mainline, stable, and longterm
releases with dates. Use it to pick an argument for `build`. `-f json` if you
are scripting.

### `build`

See [Building indexes](#building-indexes). After a successful build it prints
where the index landed and two suggested first queries.

### `indexes`

One row per built index: version, file/symbol counts, whether the call graph
is present, whether the source tree is still on disk, build time, size. The
`*` is the default index as `ka use` currently defines it.

### `use` / `remove`

See [Choosing a kernel version](#choosing-a-kernel-version) and
[Deleting indexes](#deleting-indexes). These only touch files under `indexes/`
(and, with `--source`, `kernels/`). They never change your system `PATH`.

### `stats`

Totals for the active index, a breakdown of symbols by kind, and the largest
top-level directories of the tree with a one-line description of each
(`drivers` → Device drivers, `mm` → Memory management, `net` → Networking).
Useful as a first orientation when you have just built an index.

### `info`

The "what is this?" command. For a directory it reports how many files and
subdirectories sit in it, the top-level *area* (a plain-English description of
`mm/`, `net/`, `kernel/`, …), every `MAINTAINERS` section that claims the path
(most precise first, with maintainers and lists), a walk of parent directories
each labelled with *their* subsystem, and the on-disk path.

For a file it also reports size, line count, and how many symbols of each kind
the file defines.

For a symbol it reports the kind (`function`, `syscall`, `struct`, …), the
line span, the signature, and the linkage: `EXPORT_SYMBOL` (callable from
modules), `static` (file-local), or global.

`-f json` dumps the same facts for scripts. `--max-subsystems` and
`--max-candidates` trim the two lists that can get long.

### `siblings` (`sib`)

See [The main idea: "same level"](#the-main-idea-same-level). This is the
command you will use most. Combine `--level`, `--kinds`, `--sort`, `--grep`,
`--exported`, `--static-only` / `--no-static`, and `--with-subsystem` / `-S`
to slice the container.

A directory's neighbours often do *not* share a subsystem. `-S` makes that
visible: four files in `block/` belong to the block layer, BFQ, cgroup blkio,
and SED Opal respectively.

### `ls`

Lists the *contents* of a folder (subdirectories and files) or, given a file,
the symbols defined in it. Same filters and output flags as `siblings`.
`ka ls` with no argument lists the kernel root.

```bash
ka ls mm --kinds file --sort lines -n 5
ka ls mm/page_alloc.c --kinds function --grep alloc
ka ls security --kinds dir -S
```

### `tree`

Draws a directory tree. `-d N` is depth (default 2). `--files` includes files.
`-f json` is a flat list rather than ASCII art.

```bash
ka tree net -d 1
ka tree kernel/sched -d 1 --files
```

### `find`

Searches **symbols** by name. The default is a case-sensitive substring with
a limit of 50 (unlike `siblings`, which defaults to all).

| Flag | Match |
| --- | --- |
| (none) | substring: `sendmsg` hits `tcp_sendmsg` |
| `--prefix` | `tcp_` hits `tcp_sendmsg`, not `xtcp_…` |
| `--glob` | `tcp_*msg`, `sys_*` (shell glob, not regex) |
| `--exact` | the name is exactly this |

`--kinds` here is restricted to symbol kinds (`function`, `syscall`, `struct`,
…). `--exported`, `--static-only`, `--grep` (regex on the name, applied after
the search) all work. Each hit is labelled with its subsystem so you can see
when one name lives in three different areas.

```bash
ka find tcp_sendmsg --exact
ka find __alloc_pages --prefix
ka find 'sys_*' --glob --kinds syscall
ka find sendmsg --kinds function --exported
```

### `path`

Prints the absolute path of a folder, file, or symbol on disk, so editors and
ordinary Unix tools work:

```bash
vim   $(ka path tcp_sendmsg)
code -g $(ka path tcp_sendmsg --line)    # /.../net/ipv4/tcp.c:1446
grep -rn lock_sock $(ka path net/ipv4)
```

`--line` appends `:LINE` for symbols. This needs the source tree under
`kernels/`; queries (`info`, `siblings`, `find`) do not.

### `show`

Prints source without leaving the terminal. Given a symbol, it prints exactly
that symbol, with a header naming the file, the line, and the subsystem:

```
$ ka show tcp_sendmsg
net/ipv4/tcp.c:1446  tcp_sendmsg   [NETWORKING [TCP]]
  1446 int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
  1447 {
  1448 	int ret;
  1449
  1450 	lock_sock(sk);
  1451 	ret = tcp_sendmsg_locked(sk, msg, size);
  1452 	release_sock(sk);
  1453
  1454 	return ret;
  1455 }
```

```bash
ka show tcp_sendmsg -C 5                 # 5 lines of context either side
ka show net/ipv4/tcp.c -L 1446:1455      # a line range from a file
ka show net/ipv4/tcp.c                   # the whole file
ka show tcp_sendmsg --bare               # no header, no line numbers
```

### `trace`

Maps a kernel oops, an ftrace stack, gdb frames, or a list of names to a file,
a line and a subsystem. It does **not** need a call-graph index.

```bash
$ dmesg | ka trace
$ ka trace tcp_sendmsg __alloc_pages_noprof kthread

Backtrace across 3 frames (Linux 7.2)

  #0  tcp_sendmsg           net/ipv4/tcp.c:1446       NETWORKING [TCP]
  #1  __alloc_pages_noprof  mm/page_alloc.c:5333      MEMORY MANAGEMENT - PAGE ALLOCATOR
  #2  kthread               kernel/kthread.c:380      Core kernel  (+2 more defs)

  Areas touched
    Networking               1 frame
    Memory management        1 frame
    Core kernel              1 frame
```

`(+N more defs)` means the name resolved to several definitions (common for
architecture helpers). `-f json` is the same data for scripts. `-n` caps the
number of frames.

### `subsystems` / `subsystem`

`subsystems` lists every section parsed out of `MAINTAINERS`, with file count
and status (`Maintained`, `Supported`, `Odd Fixes`, …). `--grep` is a regex on
the name; `--sort size|name`; `-n` limits the list.

```bash
ka subsystems --grep SCHED
ka subsystems --sort size -n 10
```

`subsystem NAME` is the detail view: maintainers, reviewers, lists, git tree,
website, file count, and the top directories that section claims. A unique
substring is enough (`ka subsystem SCHEDULER`). `--files` lists every claimed
file (can be thousands).

### `calls`

Requires an index built with `--with-calls`. Shows what a function calls, or
with `--callers`, what calls it.

```bash
ka -K 6.18.45 calls tcp_sendmsg
# lock_sock, tcp_sendmsg_locked, release_sock

ka -K 6.18.45 calls tcp_sendmsg_locked --callers
```

It matches on **name only**, so it cannot see calls through function pointers
— which the kernel uses everywhere (ops structs, `.read()`, `.bmap`). A
callee with no definition anywhere in the index (compiler builtin, unexpanded
macro) is listed with kind `?`.

## A tour across subsystems

The same few commands work everywhere. What changes is the path you hand them.

### Memory management

`mm/` is one directory but several `MAINTAINERS` sections (CORE, PAGE
ALLOCATOR, MEMORY MAPPING, PAGE CACHE, …). `info` and `find` make that split
visible:

```bash
ka info mm
ka find __alloc_pages --prefix -n 4
#   macro     __alloc_pages         include/linux/gfp.h   MEMORY MANAGEMENT - CORE
#   function  __alloc_pages_noprof  mm/page_alloc.c       MEMORY MANAGEMENT - PAGE ALLOCATOR
```

The real page allocator is `__alloc_pages_noprof`; `__alloc_pages` is a wrapper
macro. Searching rather than guessing the name is the reliable way across
releases.

### Scheduler, next to the rest of `kernel/`

```bash
ka siblings kernel/sched -n 8
# bpf/  cgroup/  dma/  entry/  events/  futex/  ...
ka ls kernel/sched --kinds file --sort lines
ka info schedule          # kernel/sched/core.c, subsystem SCHEDULER
ka subsystem SCHEDULER    # who to mail, which git tree
```

### Networking

```bash
ka siblings net/ipv4/tcp.c --sort lines -n 5
# tcp_input.c  7805 lines   — receive path, the biggest file
# tcp_output.c 4664 lines   — send path
# udp.c, route.c, ...
ka siblings tcp_sendmsg                  # other functions in tcp.c
ka siblings tcp_sendmsg --level subsystem
```

Sorting a directory by line count is a decent way to find where the work is.

### Block layer — neighbours that do not share an owner

```bash
ka siblings block/bio.c --sort lines -n 4 -S
# bfq-iosched.c   BFQ I/O SCHEDULER
# blk-mq.c        BLOCK LAYER
# sed-opal.c      SECURE ENCRYPTING DEVICE (SED) OPAL DRIVER
# blk-iocost.c    CONTROL GROUP - BLOCK IO CONTROLLER (BLKIO)
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

### Syscalls, BPF, io_uring, arch

`SYSCALL_DEFINEn` macros are reassembled into the real symbol names
(`sys_bpf`, `compat_sys_iopl`), so syscalls are searchable even though they
are not written as ordinary C functions:

```bash
ka find 'sys_*' --glob --kinds syscall -n 8
# sys_bpf   kernel/bpf/syscall.c   BPF [CORE]
# sys_brk   mm/mmap.c              MEMORY MAPPING
# sys_bind  net/socket.c           NETWORKING [SOCKETS]
# sys_fork  kernel/fork.c          EXEC & BINFMT API, ELF
ka show sys_bpf
ka info io_uring                  # IO_URING, Jens Axboe
ka siblings arch/x86              # every other architecture port
ka tree arch/x86 -d 1
```

## Controlling the output

Listing commands (`siblings`, `ls`, `find`, `calls`) accept:

| Option | Values / meaning |
| --- | --- |
| `--format`, `-f` | `table` (default), `plain`, `names`, `json`, `csv`, `tree` |
| `--columns`, `-c` | comma-separated, ordered: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature` |
| `--limit`, `-n` | max rows; `0` = all |
| `--sort` | `name`, `path`, `kind`, `line`, `size`, `lines` (size/lines sort descending) |
| `--grep`, `-g` | keep only names matching a regex (case-insensitive) |
| `--with-subsystem`, `-S` | add a subsystem column |
| `--kinds`, `-k` | `dir,file,function,syscall,struct,union,enum,typedef,macro,variable,prototype` or shortcuts `all`, `symbols`, `paths`, `functions`, `types` |
| `--exported` | only `EXPORT_SYMBOL`'d symbols |
| `--static-only` / `--no-static` | keep only / drop `static` symbols |

Global options work before **or** after the subcommand:

| Option | Meaning |
| --- | --- |
| `-K`, `--kernel` | which index (`6.18.45`, or a unique prefix like `6.18`) |
| `--db PATH` | a specific index file |
| `--color` | `auto` (default), `always`, `never` |

`names` and `plain` print bare values with no header or footer, so they pipe
cleanly. `plain` is `path` for files/dirs and `path:line:name` for symbols —
the same shape grep prints, so editors' quickfix lists understand it.

```bash
ka siblings kernel/sched -f names | head
ka ls net/ipv4 --kinds function -f json | jq -r '.[].name'
ka find tcp_ --prefix -f csv > tcp-symbols.csv
ka ls mm/page_alloc.c --kinds function --grep 'alloc' -f plain
```

Unknown `--columns` or `--kinds` values are rejected with the valid list,
rather than silently ignored. A bad `--grep` regex is a one-line error, not a
traceback.

## How subsystems are determined

The kernel ships `MAINTAINERS`, the authoritative statement of who owns what.
`kernel-atlas` parses its ~3,100 sections rather than hardcoding a table, so
the mapping is correct for the version you indexed.

Pattern matching follows the rules in that file's own header. `*` never
crosses a `/`:

```
F: drivers/net/     all files in and below drivers/net
F: drivers/net/*    all files in drivers/net, but not below
F: */net/*          all files in "any top level directory"/net
X: path             excluded, even if an F: line above matched
N: regex            matched against the whole path
```

When several sections match, the most precise one wins. The catch-all `THE
REST` section claims every path in the tree, so it is only *shown* as the
answer when nothing more specific matches — and in that case a plain-English
description of the top-level directory (`mm/` → Memory management, `net/` →
Networking) is preferred.

## How parsing works, and its limits

C is parsed with [tree-sitter](https://tree-sitter.github.io/). It does **not**
run the C preprocessor, and the kernel is extremely macro-heavy, so several
idioms are handled explicitly:

- `SYSCALL_DEFINE3(open, ...)` does not parse as a function: the macro call
  becomes a statement and the body a *sibling* block. It is rebuilt as
  `sys_open`, including its call edges. `COMPAT_SYSCALL_DEFINE4(...)` becomes
  `compat_sys_…`, a different symbol.
- `EXPORT_SYMBOL` / `EXPORT_SYMBOL_GPL` / `EXPORT_PER_CPU_SYMBOL` mark a
  symbol as available to modules.
- `DECLARE_WORK(name, fn)`, `DEFINE_MUTEX(name)`, `LIST_HEAD(name)`,
  `DECLARE_BITMAP(name, n)`, `DEFINE_PER_CPU(type, name)` declare `name`.
- Trailing attribute macros (`____cacheline_aligned_in_smp`) are not mistaken
  for variable names.
- Declarations inside `#ifdef` *in a function body* are locals, not file-scope.
- `int (*fp)(void);` is a function-pointer variable; `int fp(void);` is a
  prototype. The two are told apart.

Known limits:

- Code inside `#if` branches is indexed regardless of `.config`.
- A name defined per-architecture resolves to one likely definition;
  `ka find --exact <name>` shows all of them.
- Functions generated entirely by macros other than the ones above are missed.
- The call graph resolves names, not function pointers.
- Only C is parsed. Assembly and Rust files appear as files, without symbols.

## Troubleshooting

**"no index built yet"** — `ka build lts` once. Everything else needs an index.

**"no index for X"** — `ka indexes` lists what you have. Prefixes must be
unique (`-K 6` is ambiguous if you have both 6.12 and 6.18).

**"this index has no call graph"** — rebuild that version with
`--with-calls --force`. `ka trace` does not need it.

**"the source for Linux X is not on disk"** — `path` and `show` need the tree
under `kernels/`. Other commands do not. `ka build X --force` re-downloads.

**"pinned version has no index any more"** — you `remove`d the version `use`
was pointing at (or the pin is stale). `ka use --clear` or `ka use <other>`.

**"looks like an interrupted build"** — killed mid-index on an older version
of this tool; `--force` rebuilds. Builds are atomic now.

**The index disagrees with the file I just edited** — the index is a snapshot.
Rebuild with `--force` after editing files under `kernels/`.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

The tests build a small synthetic kernel tree (`tests/fixture.py`) with its own
`MAINTAINERS`, plus a throwaway `KERNEL_ATLAS_HOME` for `use` / `remove`, so
they need no network and never touch your real indexes.

### Layout

| File | Purpose |
| --- | --- |
| `config.py` | where kernels, indexes, and the `use` pin live |
| `kernelsrc.py` | kernel.org release list, resumable download, checksum, atomic extract |
| `cparse.py` | tree-sitter C extraction and kernel macro idioms |
| `maintainers.py` | `MAINTAINERS` parsing and path → subsystem matching |
| `indexer.py` | tree walk, parallel parsing, subsystem attachment, atomic build |
| `db.py` | SQLite schema |
| `query.py` | target resolution, container/level model, search |
| `render.py` | table / plain / json / csv / tree output |
| `cli.py` | command line interface |

## Licence

MIT.
