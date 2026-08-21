# kernel-atlas

A map of the Linux kernel for people who are still learning their way around it.

`kernel-atlas` downloads a kernel release from kernel.org, indexes every
directory, file and C symbol in it, and works out which **subsystem** each path
belongs to by parsing the kernel's own `MAINTAINERS` file. Then you can ask it
questions:

- *What else lives next to this file?*
- *What other functions are in this file / this directory / this subsystem?*
- *I have a function name from an oops. Where is it, and whose subsystem is it?*
- *Just show me the source of this function.*

Everything is local and offline once the index is built.

```
$ ka info fs/ext4/inode.c:ext4_bmap

fs/ext4/inode.c:ext4_bmap

  kind         function
  defined in   fs/ext4/inode.c:3363-3391 (29 lines)
  signature    static sector_t ext4_bmap(struct address_space *mapping, sector_t block)
  linkage      static (file-local)
  on disk      ~/kernel-atlas/kernels/linux-6.18.45/fs/ext4/inode.c

  Area: Filesystems
    VFS layer plus every individual filesystem (ext4, btrfs, xfs, ...).

  Subsystem (from MAINTAINERS)
   * EXT4 FILE SYSTEM   [Maintained]  81 files
       maintainer  "Theodore Ts'o" <tytso@mit.edu>
       list        linux-ext4@vger.kernel.org
```

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Where everything lives](#where-everything-lives)
- [Building indexes](#building-indexes)
- [Naming a target](#naming-a-target)
- [The main idea: "same level"](#the-main-idea-same-level)
- [A tour across the kernel](#a-tour-across-the-kernel)
- [Reading the code](#reading-the-code)
- [Backtraces](#backtraces)
- [Call graph](#call-graph)
- [Controlling the output](#controlling-the-output)
- [Command reference](#command-reference)
- [How subsystems are determined](#how-subsystems-are-determined)
- [How parsing works, and its limits](#how-parsing-works-and-its-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install

Requires Python 3.10+ and roughly 2.5 GB of disk per kernel version
(1.6 GB source + a 0.7–1 GB index).

```bash
git clone <this repo> && cd kernel-atlas
python3 -m venv .venv
.venv/bin/pip install -e .
```

This creates two equivalent commands, `kernel-atlas` and the shorter `ka`, but
**only inside the venv** — nothing is added to your system `PATH` and no shell
profile is touched. Either activate the venv:

```bash
source .venv/bin/activate
ka info fs/ext4
```

or call the full path from anywhere (the shebang pins it to the venv's Python):

```bash
~/kernel-atlas/.venv/bin/ka info fs/ext4
```

Deleting `.venv/` removes both commands; deleting `kernels/` and `indexes/`
reclaims the data.

## Quick start

```bash
ka build lts                  # one-off: download + index the latest LTS (~2 min)

ka info fs/ext4               # what is this? who maintains it?
ka siblings fs/ext4           # what sits at the same level?
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
│   └── 7.2.db
└── src/kernel_atlas/
```

Both directories are gitignored. Point your editor or `grep` straight at
`kernels/linux-*/`, or let `ka path` hand you absolute paths into it.

Set `KERNEL_ATLAS_HOME` to keep the data somewhere else (e.g. a bigger disk):

```bash
export KERNEL_ATLAS_HOME=/mnt/big-disk/kernel-atlas
```

The only other relevant environment variable is `NO_COLOR`, which disables
colored output (as does `--color never`).

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
`sha256sums.asc` and resume automatically if the connection drops. Both
extraction and indexing are atomic: an interrupted build can never leave
behind something that looks like a finished tree or index.

| `build` option | Effect |
| --- | --- |
| `--src PATH` | index a kernel tree you already have (version read from its `Makefile`) |
| `--kinds LIST` | which symbol kinds to index (default: `function,syscall,struct,union,enum,typedef,macro,variable`; add `prototype` if wanted) |
| `--with-calls` | also record the call graph (enables `ka calls`) |
| `--jobs N` | parser processes (default: one per CPU, max 16) |
| `--output PATH` | write the index somewhere specific |
| `--keep-tarball` | don't delete the `.tar.xz` after extraction |
| `--no-verify` | skip the checksum (not recommended) |
| `--quiet` | no progress output |

For scale, Linux 6.18.45 on a laptop: 91,107 files, ~4.05M symbols, 3,144
subsystems, ~60 s to index. Most of the index's size is the kernel's ~2.9M
macros — `--kinds function,syscall,struct,enum,typedef` gives a much smaller
index if you don't need them.

### Multiple versions

Keep as many as you like and choose per command; without `-K`, the **highest
version** is used:

```bash
ka indexes                     # what you have, with sizes and build dates
ka -K 6.18.45 info fs/ext4     # exact
ka -K 6.18 info fs/ext4        # unique prefix works too
ka stats                       # totals and biggest top-level areas of an index
```

## Naming a target

Every command that takes a `target` accepts all of these forms:

```bash
ka info fs/ext4                      # a folder
ka info fs/ext4/inode.c              # a file
ka info fs/ext4/inode.c:ext4_bmap    # a symbol in a known file
ka info ext4_bmap                    # a bare symbol name
ka info inode.c:ext4_bmap            # basename:symbol — the symbol picks the right inode.c
ka info fs/ext4/inode.c:2768         # whatever symbol spans that line number
ka info inode.c                      # a bare filename
ka info ext4                         # a bare directory name
ka info .                            # the kernel root
```

When a name is ambiguous (`inode.c` exists in 60+ places; some symbols have
per-architecture definitions), the most likely candidate is chosen — real
definitions beat prototypes, non-static beats static — and the alternatives are
listed. Typos get "did you mean" suggestions for both symbols and paths, and
`file.c:no_such_symbol` tells you plainly that the file exists but the symbol
does not.

## The main idea: "same level"

Every target lives in a **container**:

| Target | Its container |
| --- | --- |
| a folder | its parent directory |
| a file | its directory |
| a symbol | the file it is defined in |

`ka siblings` lists the other members of that container. `--level` widens the
container outwards, and `--kinds` chooses *what* to list from it, independently
of what you asked about:

| `--level` | Scope becomes |
| --- | --- |
| `auto` (default) | the natural container above |
| `file` | the containing file (symbols only) |
| `dir` | the containing directory |
| `subtree` | that directory and everything beneath it |
| `subsystem` | every file the target's subsystem claims |
| `tree` | the entire kernel |

```bash
ka siblings fs/ext4                                   # other filesystems
ka siblings fs/ext4/inode.c                           # other files in fs/ext4/
ka siblings ext4_bmap                                 # other functions in inode.c
ka siblings ext4_bmap --level subsystem               # every ext4 function
ka siblings fs/ext4/inode.c --kinds function          # functions next to a *file*
ka siblings ext4_bmap --level dir --kinds file        # files around a *symbol*
ka siblings fs/ext4 --include-self                    # keep the target, marked >
```

`ka ls` is the complement — it looks *inside* rather than *beside*: children of
a folder, or symbols defined in a file.

## A tour across the kernel

### Filesystems — start with a directory

```bash
$ ka siblings fs/ext4                    # what other filesystems are there?
$ ka ls fs/ext4 --kinds file --sort lines -n 5
contents of fs/ext4/

  KIND  NAME       LINES    SIZE       SYMBOLS
  file  super.c     7522  208.3K           271
  file  mballoc.c   7191  201.9K           170
  file  inode.c     6801  196.2K           150
  file  extents.c   6251  172.1K           111
  file  namei.c     4241  110.8K           111
```

Sorting by size is a decent way to find where a subsystem's real work happens.

### Networking — sibling files, then widen

```bash
$ ka siblings net/ipv4/tcp.c --sort lines -n 5
Siblings of net/ipv4/tcp.c
  level: directory net/ipv4/   subsystem: NETWORKING [TCP]   showing: file

  KIND  NAME          LINES    SIZE       SYMBOLS
  file  tcp_input.c    7594  218.2K           232
  file  tcp_output.c   4599  134.3K           131
  file  nexthop.c      4180  100.7K           174
  file  udp.c          4051  102.6K           142
  file  tcp_ipv4.c     3829  101.1K           113

$ ka siblings tcp_sendmsg                       # functions in the same file
$ ka siblings tcp_sendmsg --level subsystem     # everything in NETWORKING [TCP]
```

### Memory management — search when you don't know the name

Names drift between releases; search rather than guess:

```bash
$ ka find __alloc_pages --prefix -n 4
  KIND      NAME                  PATH                 LINE  SUBSYSTEM
  macro     __alloc_pages         include/linux/gfp.h   231  MEMORY MANAGEMENT - CORE
  macro     __alloc_pages_bulk    include/linux/gfp.h   240  MEMORY MANAGEMENT - CORE
  macro     __alloc_pages_node    include/linux/gfp.h   292  MEMORY MANAGEMENT - CORE
  function  __alloc_pages_noprof  mm/page_alloc.c      5268  MEMORY MANAGEMENT - PAGE ALLOCATOR
```

Note that `mm/` is not one subsystem but several — `MEMORY MANAGEMENT - CORE`,
`- PAGE ALLOCATOR`, `MEMORY MAPPING` and more. That distinction comes straight
from `MAINTAINERS`.

### Core kernel and the scheduler

```bash
$ ka find schedule --exact
  KIND      NAME      PATH                                  LINE  SUBSYSTEM
  function  schedule  kernel/sched/core.c                   7027  SCHEDULER
  macro     schedule  tools/testing/shared/linux/kernel.h     21  THE REST
  variable  schedule  .../bpf/progs/test_snprintf.c           35  BPF [GENERAL] ...
```

A common name resolving to three different things in three different areas is
exactly why `find` reports the subsystem alongside each hit.

### Block layer — when neighbours belong elsewhere

Add `-S` to any listing for a subsystem column. Files sitting in the same
directory often do not share an owner:

```bash
$ ka siblings block/bio.c --sort lines -n 4 -S
  KIND  NAME           SUBSYSTEM                                    LINES    SIZE
  file  bfq-iosched.c  BFQ I/O SCHEDULER                             7691  264.6K
  file  blk-mq.c       BLOCK LAYER                                   5269  132.7K
  file  blk-iocost.c   CONTROL GROUP - BLOCK IO CONTROLLER (BLKIO)   3553   98.8K
  file  sed-opal.c     SECURE ENCRYPTING DEVICE (SED) OPAL DRIVER    3350   78.0K
```

### Security — one directory, one LSM each

```bash
$ ka ls security --kinds dir -n 6 -S
  KIND  NAME        SUBSYSTEM                                       SUBDIRS  FILES
  dir   apparmor/   APPARMOR SECURITY MODULE                              1     29
  dir   bpf/        BPF [SECURITY & LSM] ...                              0      2
  dir   integrity/  Extended Verification Module (EVM)                    3      7
  dir   ipe/        INTEGRITY POLICY ENFORCEMENT (IPE)                    0     21
  dir   keys/       KEYS/KEYRINGS                                         2     20
  dir   landlock/   LANDLOCK SECURITY MODULE                              1     28
```

### Drivers — who do I email about my NIC?

```bash
$ ka info drivers/net/ethernet/intel/igb
  Area: Device drivers
   * INTEL ETHERNET DRIVERS   [Maintained]  427 files
       maintainer  Tony Nguyen <anthony.l.nguyen@intel.com>
       list        intel-wired-lan@lists.osuosl.org
     NETWORKING DRIVERS   [Maintained]  4,864 files
       list        netdev@vger.kernel.org
```

Both are correct: the precise subsystem first, the umbrella one after. That is
the ordering `MAINTAINERS` itself asks for.

### Syscalls

`SYSCALL_DEFINEn` macros are reassembled into the real symbol names, so
syscalls are searchable even though they are not written as plain C functions:

```bash
$ ka find 'sys_*' --glob --kinds syscall -n 6 -c name,path,line,subsystem
  NAME     PATH                  LINE  SUBSYSTEM
  sys_bpf  kernel/bpf/syscall.c  6294  BPF [CORE]
  sys_brk  mm/mmap.c              115  MEMORY MAPPING
  sys_brk  mm/nommu.c             380  MEMORY MAPPING
  sys_dup  fs/file.c             1457  FILESYSTEMS (VFS and infrastructure)
  sys_ipc  ipc/syscall.c          110  THE REST
  sys_tee  fs/splice.c           1979  FILESYSTEMS (VFS and infrastructure)

$ ka info sys_openat        # the real definition, with its signature
$ ka show sys_open          # and its source
```

`compat_sys_openat` is indexed as its own symbol, distinct from `sys_openat`.

### BPF, io_uring, crypto, arch

```bash
ka info kernel/bpf          # -> BPF [GENERAL], ~2,000 files
ka info io_uring            # -> IO_URING
ka ls crypto --kinds file --sort lines -n 5
ka siblings arch/x86        # every other architecture port
ka tree arch/x86 -d 1
```

## Reading the code

Because the kernel trees live inside the project, `kernel-atlas` can hand you
straight to them.

```bash
$ ka path fs/ext4/inode.c
/Users/you/kernel-atlas/kernels/linux-6.18.45/fs/ext4/inode.c

$ ka path ext4_bmap --line
/Users/you/kernel-atlas/kernels/linux-6.18.45/fs/ext4/inode.c:3363
```

Which makes editors and normal shell tools work directly:

```bash
vim   $(ka path ext4_bmap)
code -g $(ka path ext4_bmap --line)
grep -rn iomap_bmap $(ka path fs/ext4)
```

`show` prints source without leaving the terminal. Given a symbol it prints
exactly that symbol, with its subsystem in the header:

```bash
$ ka show tcp_sendmsg
net/ipv4/tcp.c:1409  tcp_sendmsg   [NETWORKING [TCP]]
  1409 int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
  1410 {
  1411 	int ret;
  ...
  1418 }

ka show ext4_bmap -C 5              # 5 lines of context either side
ka show fs/ext4/inode.c -L 100:140  # a line range from a file
ka show fs/ext4/inode.c             # the whole file
ka show tcp_sendmsg --bare          # no header/numbers — pipe it anywhere
```

## Backtraces

Paste a kernel oops, an ftrace stack, gdb frames, or just names — every frame is
mapped to a file, a line and a subsystem. This does **not** need a call graph.

```bash
$ dmesg | ka trace
$ ka trace ext4_do_writepages kthread vfs_write

Backtrace across 8 frames (Linux 6.18.45)

  #0  ext4_do_writepages        fs/ext4/inode.c:2768          EXT4 FILE SYSTEM
  #1  __alloc_pages_noprof      mm/page_alloc.c:5268          MEMORY MANAGEMENT - PAGE ALLOCATOR
  #2  do_writepages             mm/page-writeback.c:2593      PAGE CACHE
  #3  __writeback_single_inode  fs/fs-writeback.c:1731        FILESYSTEMS (VFS and infrastructure)
  #4  wb_writeback              fs/fs-writeback.c:2162        FILESYSTEMS (VFS and infrastructure)
  #5  tcp_sendmsg               net/ipv4/tcp.c:1409           NETWORKING [TCP]
  #6  kthread                   kernel/kthread.c:380          Core kernel
  #7  ret_from_fork             arch/x86/kernel/process.c:151 X86 ARCHITECTURE (32-BIT AND 64-BIT)

  Areas touched
    Filesystems              3 frames
    Memory management        2 frames
    ...
```

Frames that resolve to several definitions are flagged with `(+N more defs)`,
and `-f json` gives the same data machine-readably.

## Call graph

Built only when you ask (`--with-calls`), because it costs a few hundred MB:

```bash
ka build lts --with-calls --force
ka calls vfs_write              # what does it call?
ka calls do_sys_open --callers  # who calls it?  (sys_open and sys_openat do)
```

It matches on **name only**, so it cannot see calls through function pointers —
which the kernel uses everywhere. `ka calls ext4_bmap --callers` correctly
returns nothing, because `ext4_bmap` is only reached through the `.bmap` entry
of an ops struct. Callees with no definition anywhere in the index (compiler
builtins, unexpanded macros) are listed with kind `?`.

## Controlling the output

All listing commands (`siblings`, `ls`, `find`, `calls`) accept:

| Option | Values / meaning |
| --- | --- |
| `--format`, `-f` | `table` (default), `plain`, `names`, `json`, `csv`, `tree` |
| `--columns`, `-c` | comma-separated, ordered: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature` |
| `--limit`, `-n` | max rows; `0` = all (the target itself never counts against it) |
| `--sort` | `name`, `path`, `kind`, `line`, `size`, `lines` (size/lines sort descending) |
| `--grep`, `-g` | keep only names matching a regex (case-insensitive) |
| `--with-subsystem`, `-S` | add a subsystem column |
| `--kinds`, `-k` | what to list: `dir,file,function,syscall,struct,union,enum,typedef,macro,variable,prototype` or shortcuts `all`, `symbols`, `paths`, `functions`, `types` |
| `--exported` | only `EXPORT_SYMBOL`'d symbols |
| `--static-only` / `--no-static` | keep only / drop `static` symbols |

Global options work before **or** after the subcommand:

| Option | Meaning |
| --- | --- |
| `-K`, `--kernel` | which index to use (`6.18.45`, or a unique prefix like `6.18`) |
| `--db PATH` | use a specific index file |
| `--color` | `auto` (default), `always`, `never` |

`names` and `plain` print bare values with no header or footer, so they pipe
cleanly; `json` and `csv` are for scripts and spreadsheets:

```bash
ka siblings fs/ext4 -f names | head
ka ls net/ipv4 --kinds function -f json | jq -r '.[].name'
ka find ext4_ --prefix -f csv > ext4-symbols.csv
ka siblings mm/page_alloc.c -c name,lines,size --sort lines
ka ls fs/ext4/super.c --kinds function --grep '^ext4_(get|put)' -f plain
```

`plain` prints `path` for files/dirs and `path:line:name` for symbols — the
same shape grep prints, so editors and quickfix lists understand it.

## Command reference

| Command | Arguments | Purpose |
| --- | --- | --- |
| `ka versions` | `[-f table\|json]` | releases currently on kernel.org |
| `ka build` | `[version] [--src PATH] [--kinds L] [--with-calls] [--jobs N] [--output P] [--keep-tarball] [--no-verify] [--force] [--quiet]` | download + index a kernel |
| `ka indexes` | `[-f table\|json]` | list built indexes |
| `ka stats` | `[-f table\|json]` | index totals, symbols by kind, biggest areas |
| `ka info` | `TARGET [--max-subsystems N] [--max-candidates N] [-f table\|json]` | explain one folder/file/symbol |
| `ka siblings` | `TARGET [--level L] [--include-self] [filters] [output]` | what sits at the same level |
| `ka ls` | `[TARGET] [filters] [output]` | contents of a folder / symbols in a file |
| `ka tree` | `[TARGET] [-d DEPTH] [--files] [-f tree\|json]` | draw the directory tree |
| `ka find` | `PATTERN [--exact\|--glob\|--prefix] [filters] [output]` | search symbols by name |
| `ka path` | `TARGET [--line]` | absolute on-disk path |
| `ka show` | `TARGET [-C N] [-L A:B] [--bare]` | print source |
| `ka trace` | `[FRAMES...] [-n N] [-f table\|json]` | annotate a backtrace (or read stdin) |
| `ka subsystems` | `[-g REGEX] [--sort size\|name] [-n N] [-f table\|json]` | list subsystems |
| `ka subsystem` | `NAME [--files] [-n N] [-f table\|json]` | one subsystem in detail |
| `ka calls` | `TARGET [--callers] [output]` | call graph (needs `--with-calls`) |

`siblings` also answers to `ka sib`. `ka <command> --help` shows everything.

## How subsystems are determined

The kernel ships `MAINTAINERS`, the authoritative statement of who owns what.
`kernel-atlas` parses its ~3,100 sections rather than hardcoding a subsystem
table, so the mapping is always correct for the version you indexed.

Pattern matching follows the rules in that file's own header, notably that `*`
never crosses a `/`:

```
F: drivers/net/     all files in and below drivers/net
F: drivers/net/*    all files in drivers/net, but not below
F: */net/*          all files in "any top level directory"/net
X: fs/ext4/         excluded, even if an F: line above matched
N: regex            matched against the whole path
```

When several sections match, the most precise one wins, following the advice in
`MAINTAINERS` to "look for the most precise areas first". The catch-all `THE
REST` section claims every path in the tree, so it is only reported when nothing
more specific matches — and in that case a plain-English description of the
top-level directory (`fs/` → Filesystems, `mm/` → Memory management) is shown
instead.

## How parsing works, and its limits

C is parsed with [tree-sitter](https://tree-sitter.github.io/), which is fast
and error-tolerant — but it does **not** run the C preprocessor, and the kernel
is extremely macro-heavy. Several kernel idioms are handled explicitly:

- `SYSCALL_DEFINE3(open, ...)` does not parse as a function at all: the macro
  call becomes a statement and the body a *sibling* block. It is reassembled
  into `sys_open`, including its call edges. `COMPAT_SYSCALL_DEFINE4(openat,
  ...)` correctly becomes `compat_sys_openat`, a different symbol from
  `sys_openat`.
- `EXPORT_SYMBOL(foo)` / `EXPORT_SYMBOL_GPL(foo)` marks `foo` as available to
  modules (~37k symbols per release).
- `static DECLARE_WORK(free_ipc_work, free_ipc);` declares `free_ipc_work`.
- Trailing attribute macros such as
  `struct sem { ... } ____cacheline_aligned_in_smp;` are not mistaken for
  variable names.
- `#ifdef` blocks reach inside function bodies too, so declarations there are
  treated as locals rather than file-scope variables.
- `int (*fp)(void);` is a function-pointer variable; `int fp(void);` is a
  prototype. The two are told apart.

Known limits, stated plainly:

- Code inside `#if` branches is indexed regardless of configuration, so a
  symbol may be listed that your `.config` would not build.
- A name defined per-architecture (`access_ok`, much of `arch/`) resolves to
  one likely definition; `ka find --exact <name>` shows all of them.
- Functions generated entirely by macros other than the ones above are missed.
- The call graph resolves names, not function pointers.
- Only C is parsed. Assembly and Rust files are indexed as files, without
  symbols.

## Troubleshooting

**"no index built yet"** — run `ka build lts` once; everything else needs an
index.

**"this index has no call graph"** — `ka calls` needs an index built with
`--with-calls`; rebuild with `ka build <version> --with-calls --force`.

**"the source for Linux X is not on disk"** — `ka path` and `ka show` need the
tree under `kernels/`; queries (`info`, `siblings`, `find`, ...) work without
it. Rebuild to re-download.

**"looks like an interrupted build"** — a build was killed at the wrong moment
on an older version of this tool; rebuild with `--force`. (Builds are atomic
now, so this should no longer occur.)

**A download keeps failing** — transfers resume automatically and the checksum
rejects truncated files; if kernel.org is unreachable entirely, `ka versions`
will say so.

**The index disagrees with my tree** — the index is a snapshot; if you edit
files under `kernels/`, line numbers in `ka show` can drift until you rebuild
with `--force`.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

The tests build a small synthetic kernel tree (`tests/fixture.py`) with its own
`MAINTAINERS`, so they run in well under a second and need no network.

### Layout

| File | Purpose |
| --- | --- |
| `config.py` | where kernels and indexes live |
| `kernelsrc.py` | kernel.org release list, resumable download, checksum, atomic extract |
| `cparse.py` | tree-sitter C symbol extraction and kernel macro idioms |
| `maintainers.py` | `MAINTAINERS` parsing and fast path → subsystem matching |
| `indexer.py` | tree walk, parallel parsing, subsystem attachment, atomic build |
| `db.py` | SQLite schema |
| `query.py` | target resolution, container/level model, search |
| `render.py` | table / plain / json / csv / tree output |
| `cli.py` | command line interface |

## Licence

MIT.
