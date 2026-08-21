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
  on disk      /Users/you/kernel-atlas/kernels/linux-6.18.45/fs/ext4/inode.c

  Area: Filesystems
    VFS layer plus every individual filesystem (ext4, btrfs, xfs, ...).

  Subsystem (from MAINTAINERS)
   * EXT4 FILE SYSTEM   [Maintained]  81 files
       maintainer  "Theodore Ts'o" <tytso@mit.edu>
       list        linux-ext4@vger.kernel.org
```

## Install

Requires Python 3.10+.

```bash
git clone <this repo> && cd kernel-atlas
python3 -m venv .venv
.venv/bin/pip install -e .
```

This creates two equivalent commands, `kernel-atlas` and the shorter `ka`, but
**only inside the venv** — nothing is added to your system `PATH`. Either
activate the venv:

```bash
source .venv/bin/activate
ka info fs/ext4
```

or call the full path from anywhere (the shebang pins it to the venv's Python):

```bash
~/kernel-atlas/.venv/bin/ka info fs/ext4
```

## Where everything lives

The kernel source and the index are kept **inside the project directory**, not
in a hidden cache, so the code you are studying sits right next to the tool:

```
kernel-atlas/
├── kernels/
│   └── linux-6.18.45/      <- the real kernel tree: open it, grep it, browse it
├── indexes/
│   └── 6.18.45.db
└── src/kernel_atlas/
```

Both `kernels/` and `indexes/` are gitignored. That means you can point your
editor, `grep`, `ctags` or anything else straight at
`kernel-atlas/kernels/linux-6.18.45/` — and `ka path` will hand you absolute
paths into it (see [Reading the code](#reading-the-code)).

Set `KERNEL_ATLAS_HOME` to put them somewhere else:

```bash
export KERNEL_ATLAS_HOME=/mnt/big-disk/kernel-atlas
```

## Build an index

```bash
ka versions          # what kernel.org currently offers
ka build lts         # download + index the latest longterm release
```

`build` accepts `lts` (default, recommended for learning), `stable`, `mainline`,
or an exact version like `6.12.104`. The version list is fetched live from
kernel.org, so nothing is hardcoded and old releases keep working.

The tarball is verified against kernel.org's `sha256sums.asc`, and downloads
resume automatically if the connection drops (a truncated 147 MB transfer is
otherwise easy to mistake for a complete one).

Indexing Linux 6.18.45 on a laptop:

| | |
| --- | --- |
| directories / files | 6,048 / 91,107 |
| symbols | 4,051,600 from 61,467 C files |
| subsystems | 3,144 sections from `MAINTAINERS` |
| time | ~57s (plus a one-off 147 MB download) |
| index size | 741 MB, or 922 MB with `--with-calls` |

Most of that size is the kernel's ~2.9M macros. If you don't need them:

```bash
ka build lts --kinds function,syscall,struct,enum,typedef   # far smaller
```

You can hold several versions at once and pick between them with `-K`:

```bash
ka build 6.12.104
ka indexes
ka -K 6.12.104 info fs/ext4
```

## The main idea: "same level"

Every target lives in a **container**:

| Target | Its container |
| --- | --- |
| a folder | its parent directory |
| a file | its directory |
| a function or other symbol | the file it is defined in |

`siblings` lists the other members of that container, and `--level` widens the
container outwards: `file` → `dir` → `subtree` → `subsystem` → `tree`.

You also choose *what* to list with `--kinds`, independently of what you asked
about. So you can ask for the functions next to a file, or the files next to a
function.

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
$ ka siblings tcp_sendmsg --level dir --kinds file    # files around it instead
```

### Memory management — searching when you don't know the name

Names drift between releases. Search rather than guess:

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

$ ka siblings kernel/sched/core.c -n 6
$ ka ls kernel/sched --kinds file --sort lines
```

A common name resolving to three different things in three different areas is
exactly why `find` reports the subsystem alongside each hit.

### Block layer — when neighbours belong elsewhere

Add `-S` to show which subsystem each result belongs to. Files sitting in the
same directory often do not share an owner:

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
       list        intel-wired-lan@lists.osuosl.org (moderated for non-subscribers)
     NETWORKING DRIVERS   [Maintained]  4,864 files
       list        netdev@vger.kernel.org
```

Both are correct: the precise subsystem is listed first, the umbrella one after.
That is the ordering `MAINTAINERS` itself asks for.

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
$ ka info kernel/bpf          # -> BPF [GENERAL], 1,963 files
$ ka info io_uring            # -> IO_URING, 86 files
$ ka ls crypto --kinds file --sort lines -n 4
$ ka siblings arch/x86        # every other architecture port
$ ka tree arch/x86 -d 1
```

## Reading the code

Because the kernel tree lives inside the project, `kernel-atlas` can hand you
straight to it.

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

`show` prints the source without leaving the terminal. Given a symbol it prints
exactly that symbol:

```bash
$ ka show tcp_sendmsg
net/ipv4/tcp.c:1409  tcp_sendmsg   [NETWORKING [TCP]]
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
ka show ext4_bmap -C 5              # with 5 lines of context either side
ka show fs/ext4/inode.c -L 100:140  # a line range from a file
ka show tcp_sendmsg --bare          # no header, no line numbers, pipeable
```

## Backtraces

Paste a kernel oops, an ftrace stack or just a list of names, and every frame is
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

## Call graph (optional)

```bash
ka build lts --with-calls --force
ka calls vfs_write              # what it calls
ka calls ext4_get_block --callers
```

For 6.18.45 this records about 3.0M call edges and grows the index from 741 MB
to 922 MB. It is off by default; `ka trace` does not need it.

It matches on **name only**, so it cannot see calls made through function
pointers — which the kernel uses everywhere. `ka calls ext4_bmap --callers`
correctly returns nothing, because `ext4_bmap` is only ever reached through the
`.bmap` entry of an ops struct.

## Customising output

Every listing command takes:

- `--format` / `-f` — `table` (default), `plain`, `names`, `json`, `csv`, `tree`
- `--columns` / `-c` — pick and order columns: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature`
- `--limit` / `-n`, `--sort` (`name`, `path`, `kind`, `line`, `size`, `lines`)
- `--grep` / `-g` — filter names by regex
- `--with-subsystem` / `-S` — add a subsystem column
- `--exported` — only `EXPORT_SYMBOL`'d symbols
- `--static-only` / `--no-static`
- `--kinds` / `-k` — `dir`, `file`, `function`, `syscall`, `struct`, `union`,
  `enum`, `typedef`, `macro`, `variable`, `prototype`, plus the shortcuts `all`,
  `symbols`, `paths`, `functions`, `types`

`names` and `plain` print bare values with no header, so they pipe cleanly:

```bash
ka siblings fs/ext4 -f names | head
ka ls net/ipv4 --kinds function -f json | jq -r '.[].name'
ka find ext4_ --prefix -f csv > ext4-symbols.csv
ka siblings mm/page_alloc.c -c name,lines,size --sort lines
```

Global flags `-K/--kernel`, `--db` and `--color` work before *or* after the
subcommand.

## Command reference

| Command | What it does |
| --- | --- |
| `ka versions` | kernel versions available on kernel.org |
| `ka build <ver>` | download and index a release |
| `ka indexes` | indexes you have built |
| `ka stats` | what is in an index |
| `ka info <target>` | explain a folder, file or symbol |
| `ka siblings <target>` | what sits at the same level |
| `ka ls <target>` | contents of a folder, or symbols in a file |
| `ka tree <target>` | draw the directory tree |
| `ka find <pattern>` | search symbols (`--exact`, `--glob`, `--prefix`) |
| `ka path <target>` | absolute on-disk path |
| `ka show <target>` | print source |
| `ka trace` | annotate a backtrace |
| `ka subsystems` | list subsystems from `MAINTAINERS` |
| `ka subsystem <name>` | detail for one subsystem |
| `ka calls <target>` | call graph (needs `--with-calls`) |

### Ways to name a target

```bash
ka info fs/ext4                      # a folder
ka info fs/ext4/inode.c              # a file
ka info fs/ext4/inode.c:ext4_bmap    # a symbol in a known file
ka info ext4_bmap                    # a bare symbol name
ka info fs/ext4/inode.c:2768         # whatever symbol spans that line
ka info inode.c                      # a bare filename (reports if ambiguous)
```

Typos get suggestions rather than a bare failure.

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
is extremely macro-heavy. Several kernel idioms are therefore handled
explicitly:

- `SYSCALL_DEFINE3(open, ...)` does not parse as a function at all: the macro
  call becomes a statement and the body a *sibling* block. It is reassembled
  into `sys_open`. `COMPAT_SYSCALL_DEFINE4(openat, ...)` correctly becomes
  `compat_sys_openat`, a genuinely different symbol from `sys_openat`.
- `EXPORT_SYMBOL(foo)` / `EXPORT_SYMBOL_GPL(foo)` marks `foo` as available to
  modules (36,901 symbols in 6.18.45).
- `static DECLARE_WORK(free_ipc_work, free_ipc);` declares `free_ipc_work`.
- Trailing attribute macros such as
  `struct sem { ... } ____cacheline_aligned_in_smp;` are not mistaken for
  variable names.
- `#ifdef` blocks reach inside function bodies too, so declarations there are
  treated as locals rather than file-scope variables.

Known limits, stated plainly:

- Code inside `#if` branches is indexed regardless of configuration, so a symbol
  may be listed that your `.config` would not build.
- A name defined per-architecture (`access_ok`, and much of `arch/`) resolves to
  one arbitrary definition. `ka find --exact <name>` shows them all.
- Functions generated entirely by macros other than the ones above are missed.
- The call graph resolves names, not function pointers.
- Only C is parsed. Assembly and Rust files are indexed as files, without
  symbols.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

The tests build a small synthetic kernel tree (`tests/fixture.py`) with its own
`MAINTAINERS`, so they run in well under a second and need no network.

## Layout

| File | Purpose |
| --- | --- |
| `config.py` | where kernels and indexes live |
| `kernelsrc.py` | kernel.org release list, resumable download, checksum, extract |
| `cparse.py` | tree-sitter C symbol extraction and kernel macro idioms |
| `maintainers.py` | `MAINTAINERS` parsing and fast path → subsystem matching |
| `indexer.py` | tree walk, parallel parsing, subsystem attachment |
| `db.py` | SQLite schema |
| `query.py` | target resolution, container/level model, search |
| `render.py` | table / plain / json / csv / tree output |
| `cli.py` | command line interface |

## Licence

MIT.
