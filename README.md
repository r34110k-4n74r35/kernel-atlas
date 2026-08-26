# kernel-atlas

A map of the Linux kernel for people who are still learning their way around it.

`kernel-atlas` downloads a kernel from kernel.org, indexes every directory,
file and C symbol, and maps each path to a **subsystem** using the kernel's own
`MAINTAINERS` file. Then you can ask:

- What else lives next to this folder, file, or function?
- Which subsystem owns this symbol, and who maintains it?
- I have a name from an oops. Where is it defined?
- Show me the source, an editor path, or the Elixir / docs.kernel.org page.
- Did this symbol move between the LTS I am running and another release?

Queries are local and offline. `ka web` only *prints* URLs (Bootlin Elixir,
git.kernel.org, GitHub, docs.kernel.org).

Examples below are from **Linux 6.18.46** (current LTS). Line numbers move
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

Requires Python 3.10+ and roughly 2.5 GB of disk per kernel version (about
1.6 GB of source plus a 0.7–1 GB index; `--with-calls` is the larger end).

```bash
git clone <this repo> && cd kernel-atlas
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
ka siblings kernel/sched      # what sits next to the scheduler?
ka find tcp_sendmsg --exact   # where is this symbol?
ka show tcp_sendmsg           # print its source
ka web tcp_sendmsg            # Elixir / git.kernel.org / GitHub URLs
ka docs mm                    # Documentation/ files for this area
ka locate tcp_sendmsg         # same symbol in every built index
dmesg | ka trace              # map a backtrace to subsystems
```

Listing commands print `[Linux 6.18.46]` so the output always names the index
that answered.

## Where everything lives

The kernel source and the index sit **inside the project directory**, not in a
hidden cache, so the code you are studying is next to the tool:

```
kernel-atlas/
├── kernels/
│   └── linux-6.18.46/      <- real kernel tree: open it, grep it
├── indexes/
│   ├── 6.18.46.db
│   └── .default-version    <- written by `ka use`, gitignored
└── src/kernel_atlas/
```

You can keep several versions at once (`kernels/linux-7.2/`, `indexes/7.2.db`,
…). `kernels/` and `indexes/` are gitignored. Point an editor or `grep` at
`kernels/linux-*/`, or let `ka path` hand you absolute paths into it.

Set `KERNEL_ATLAS_HOME` if you want the data on another disk:

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
`indexes/.default-version`. If you `ka remove` that version, the pin is
cleared and commands fall back to the highest remaining index (with a warning
the first time).

```bash
ka remove 6.18                # delete indexes/6.18.46.db; keep the source
ka rm 6.18.46 --source        # also delete kernels/linux-6.18.46/  (~1.6 GB)
```

`remove` (alias `rm`) resolves every name *before* deleting, so
`ka remove 6.18 6.18.46` is the same index named twice, not an error. SQLite
sidecar files (`.db-wal`, `.db-shm`, `.db-journal`) go with the index.

The kernel source is **kept by default**: rebuilding from a tree already on
disk takes about a minute and no download. Pass `--source` only when you also
want that disk back. This does not ask for confirmation — the version
argument is the confirmation. `ka indexes` afterwards shows what is left.

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
connection drops and are checked against kernel.org's `sha256sums.asc`.
Extraction and indexing are atomic (scratch directory / scratch file, renamed
into place only on success): an interrupted build cannot look finished.

| `build` option | Effect |
| --- | --- |
| `--src PATH` | index a kernel tree you already have (version from its `Makefile`) |
| `--kinds LIST` | symbol kinds to index (default: `function,syscall,struct,union,enum,typedef,macro,variable`; add `prototype` if wanted) |
| `--with-calls` | also record the call graph (enables `ka calls`; a few hundred MB extra) |
| `--jobs N` | parser processes (default: one per CPU, max 16) |
| `--output PATH` | write the index somewhere specific |
| `--keep-tarball` | keep the `.tar.xz` after extraction |
| `--no-verify` | skip the checksum (not recommended) |
| `--quiet` | no progress output |

Linux 6.18.46 on a laptop is about 6,000 directories, 91,000 files, 4.05
million symbols, 3,100 subsystems, a minute to index. Most of the size is the
kernel's ~2.9 million macros; `--kinds function,syscall,struct,enum,typedef`
is much smaller if you do not need them. `ka stats` summarises an index.

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
```

When a name is ambiguous (a definition per architecture, a stub in
`tools/`, a `#define` copy), the most likely candidate is chosen: real
definitions beat prototypes, non-static beats static, `tools/` / `samples/`
lose to the real tree, shallower paths beat nested stubs — and the
alternatives are listed. Typos get "did you mean" suggestions.
`net/ipv4/tcp.c:no_such_fn` tells you the file exists but that symbol does
not, instead of guessing what the whole string might mean.

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
build, two first queries are printed. These commands only touch files under
`indexes/` (and, with `--source`, `kernels/`). They never change your system
`PATH`.

### `stats`

Totals for the active index, symbols by kind, and the largest top-level
directories with a one-line description (`mm` → Memory management, `net` →
Networking). Useful as a first orientation.

### `info`

The "what is this?" command. For a directory: how many files sit in it, the
top-level *area* (plain English for `mm/`, `net/`, `kernel/`, …), every
*interesting* `MAINTAINERS` section (most precise first, with maintainers
and lists — the catch-all `THE REST` is omitted), a walk of parent
directories, the on-disk path, and an Elixir URL.

For a file: size, line count, and how many symbols of each kind it defines.

For a symbol: kind, line span, signature, linkage (`EXPORT_SYMBOL` /
`static` / global), plus Elixir's *ident* page (every use of that name).

`-f json` dumps the same facts, including a `links` object.
`--max-subsystems` and `--max-candidates` trim the two lists that can get
long.

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
vim   $(ka path tcp_sendmsg)
code -g $(ka path tcp_sendmsg --line)    # /.../net/ipv4/tcp.c:1409
grep -rn lock_sock $(ka path net/ipv4)
```

`--line` appends `:LINE` for symbols. This needs the source tree under
`kernels/`; `info`, `siblings`, `find`, `web`, `docs` and `locate` do not.

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
`--lines N:M` or `$EDITOR $(ka path …)`. Binary files are refused.

### `web` / `docs`

`web` prints URLs for the same target on Bootlin Elixir, git.kernel.org,
GitHub, and — for `Documentation/*.rst` — docs.kernel.org. Nothing is
opened; pipe into `open` / `xdg-open` if you want a browser.

```
$ ka web tcp_sendmsg

net/ipv4/tcp.c:1409  tcp_sendmsg   [Linux 6.18.46]
  elixir  https://elixir.bootlin.com/linux/v6.18.46/source/net/ipv4/tcp.c#L1409
  ident   https://elixir.bootlin.com/linux/v6.18.46/ident/tcp_sendmsg
  git     https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/net/ipv4/tcp.c?h=v6.18.46#n1409
  github  https://github.com/gregkh/linux/blob/v6.18.46/net/ipv4/tcp.c#L1409
```

`ident` is Elixir's cross-reference for the symbol name. `--url elixir|ident|git|github|docs` prints a single URL. Three-part versions (6.18.46) use the stable tree and `gregkh/linux`; two-part versions (7.2) use torvalds. docs.kernel.org is versioned by major.minor (`v6.18`), not the patch level.

```bash
open $(ka web tcp_sendmsg --url elixir)
open $(ka web Documentation/mm/index.rst --url docs)
```

`docs` lists `Documentation/` files that belong with a target.
`Documentation/<name>/` is listed first. Bare names like `bpf` mean the
*area* (`kernel/bpf/`), not the LSM hook variable of the same name.

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

`subsystems` lists every section parsed out of `MAINTAINERS`, with file count
and status. `--grep` is a regex on the name; `--sort size|name`; `-n` limits
the list (`0` = all).

```bash
ka subsystems --grep '^SCHED'
#      53  Maintained       SCHEDULER
#      34  Maintained       SCHEDULER - SCHED_EXT
ka subsystems --sort size -n 10
```

`subsystem NAME` is the detail view: maintainers, reviewers, lists, git tree,
website, file count, and the top directories that section claims. A unique
substring is enough (`ka subsystem SCHEDULER`). `--files` lists every claimed
file (can be thousands). `-n 0` shows every top directory.

### `calls`

Requires an index built with `--with-calls`. Shows what a function calls, or
with `--callers`, what calls it. Default 200 rows; `-n 0` is all.

```bash
ka calls tcp_sendmsg
# lock_sock, tcp_sendmsg_locked, release_sock

ka calls tcp_sendmsg --callers
# tcp_bpf_sendmsg  (the BPF sockmap hook)
```

It matches on **name only**, so it cannot see calls through function pointers
— which the kernel uses everywhere (ops structs, `.read()`, `.bmap`). A
callee with no definition anywhere in the index (compiler builtin, unexpanded
macro) is listed with kind `?`.

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
```

`kernel/futex` has no dedicated `MAINTAINERS` section. `info` then shows the
Area (Core kernel) and skips dumping `THE REST` and its 90k files.

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
`open $(ka web tcp_sendmsg --url elixir)` (definition) or `--url ident`
(every use of the name). For the handbook:
`open $(ka web Documentation/mm/index.rst --url docs)`.

**I want the docs that go with this code.** `ka docs mm`, `ka docs bpf`,
`ka show Documentation/mm/index.rst`.

**Open it in an editor.** `vim $(ka path tcp_sendmsg)` or
`code -g $(ka path tcp_sendmsg --line)`. `--line` appends `:LINE`.
`path` / `show` need the tree under `kernels/`; most other commands do not.

## Controlling the output

Listing commands (`siblings`, `ls`, `find`, `calls`) accept:

| Option | Values / meaning |
| --- | --- |
| `--format`, `-f` | `table` (default), `plain`, `names`, `json`, `csv`, `tree` |
| `--columns`, `-c` | comma-separated, ordered: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature` |
| `--limit`, `-n` | max rows; `0` = all (`find` defaults to 50, `calls` to 200) |
| `--sort` | `name`, `path`, `kind`, `line`, `size`, `lines` (size/lines sort descending) |
| `--grep`, `-g` | keep only names matching a regex (case-insensitive) |
| `--with-subsystem`, `-S` | add a subsystem column |
| `--kinds`, `-k` | `dir,file,function,syscall,struct,union,enum,typedef,macro,variable,prototype` or shortcuts `all`, `symbols`, `paths`, `functions`, `types` |
| `--exported` | only `EXPORT_SYMBOL`'d symbols |
| `--static-only` / `--no-static` | keep only / drop `static` symbols |

`find` substring/prefix matching is case-insensitive; `--exact` and `--glob`
are not. Listing JSON stays an array of objects so `jq '.[].name'` works;
each row also has an `index` field naming the kernel version.

Global options work before **or** after the subcommand:

| Option | Meaning |
| --- | --- |
| `-K`, `--kernel` | which index (`6.18.46`, or a unique prefix like `6.18`) |
| `--db PATH` | a specific index file |
| `--color` | `auto` (default), `always`, `never` |

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
REST` section claims every path in the tree, so it is never *shown* as the
answer: a more specific section wins when one exists, and otherwise the
plain-English *Area* of the top-level directory is used (`mm/` → Memory
management, `kernel/` → Core kernel). `find` follows the same rule, so a
hit in `tools/` is labelled `Tools` rather than `THE REST`.

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
unique *and* land on a version-component boundary (`-K 6` is ambiguous if you
have both 6.12 and 6.18; `-K 6.1` does not select `6.18.46`).

**"this index has no call graph"** — rebuild that version with
`--with-calls --force`. `ka trace` does not need it.

**"the source for Linux X is not on disk"** — `path` and `show` need the tree
under `kernels/`. Other commands do not. `ka build X --force` re-downloads.

**"is N bytes; pass --lines"** — `show` will not dump a file bigger than 2 MB
whole. Use `--lines N:M`, or open it with `$EDITOR $(ka path …)`.

**"no Documentation/ files related to …"** — that area has no matching
`Documentation/` path. Try `ka ls Documentation --kinds dir` or `ka docs mm`.

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
| `links.py` | Elixir / git.kernel.org / GitHub / docs.kernel.org URLs |
| `render.py` | table / plain / json / csv / tree output |
| `cli.py` | command line interface |

## Licence

MIT.
