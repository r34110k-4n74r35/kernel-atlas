# kernel-atlas

A map of the Linux kernel for people who are still learning their way around it.

`kernel-atlas` downloads a kernel release from kernel.org, indexes every
directory, file and C symbol in it, and works out which **subsystem** each path
belongs to by parsing the kernel's own `MAINTAINERS` file. Then you can ask it
questions:

- *What else lives next to this file?*
- *What other functions are in this file / this directory / this subsystem?*
- *I have a function name from an oops. Where is it, and whose subsystem is it?*

Everything is local and offline once the index is built.

```
$ kernel-atlas info fs/ext4/inode.c:ext4_bmap

fs/ext4/inode.c:ext4_bmap

  kind         function
  defined in   fs/ext4/inode.c:3363-3391 (29 lines)
  signature    static sector_t ext4_bmap(struct address_space *mapping, sector_t block)
  linkage      static (file-local)

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

This installs two equivalent commands: `kernel-atlas` and the shorter `ka`.

## Build an index

```bash
kernel-atlas versions        # what kernel.org currently offers
kernel-atlas build lts       # download + index the latest longterm release
```

`build` accepts `lts` (default, recommended for learning), `stable`, `mainline`,
or an exact version like `6.12.104`. The version list is fetched live from
kernel.org, so nothing is hardcoded and old releases keep working.

The tarball is verified against kernel.org's `sha256sums.asc`, and downloads
resume automatically if the connection drops (a truncated 147 MB transfer is
otherwise easy to mistake for a complete one). Extracted source and built
indexes stay cached under `~/.cache/kernel-atlas/`, so rebuilds skip the
download entirely.

For reference, indexing Linux 6.18.45 on a laptop:

```
6,048 directories, 91,107 files
4,057,118 symbols from 61,467 C files
3,144 subsystems from MAINTAINERS
742 MB, 56s
```

That index is chunky mostly because the kernel defines ~2.9M macros. If you
don't need them:

```bash
kernel-atlas build lts --kinds function,syscall,struct,enum,typedef   # much smaller
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

```bash
# Other filesystems, i.e. folders next to fs/ext4
ka siblings fs/ext4

# Other files in fs/ext4/, biggest first
ka siblings fs/ext4/inode.c --sort lines

# Other functions in the same file
ka siblings fs/ext4/inode.c:ext4_bmap

# Widen: every function in the whole EXT4 subsystem
ka siblings fs/ext4/inode.c:ext4_bmap --level subsystem
```

You choose what to list with `--kinds`, independently of what you asked about.
So you can ask for the *functions* next to a *file*:

```bash
ka siblings fs/ext4/inode.c --kinds function
ka siblings fs/ext4        --kinds file,dir
ka siblings mm/slab.c      --kinds struct,typedef
```

Valid kinds: `dir`, `file`, `function`, `syscall`, `struct`, `union`, `enum`,
`typedef`, `macro`, `variable`, `prototype`, plus the shortcuts `all`,
`symbols`, `paths`, `functions`, `types`.

## Ways to name a target

```bash
ka info fs/ext4                      # a folder
ka info fs/ext4/inode.c              # a file
ka info fs/ext4/inode.c:ext4_bmap    # a symbol in a known file
ka info ext4_bmap                    # a bare symbol name
ka info fs/ext4/inode.c:2768         # whatever symbol spans that line
ka info inode.c                      # a bare filename (reports if ambiguous)
```

## Backtraces

Paste a kernel oops, a ftrace stack or a list of names, and get every frame
mapped to a file, a line and a subsystem. This works without a call graph.

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

## Other commands

```bash
ka stats                        # what's in the index
ka ls fs/ext4                   # contents of a folder
ka ls fs/ext4/inode.c           # symbols defined in a file
ka tree fs -d 1                 # draw the directory tree
ka tree fs/ext4 --files
ka find ext4_ --prefix          # search symbols
ka find 'ext4_*_super' --glob
ka find vfs_write --exact
ka subsystems --grep ext4       # subsystems from MAINTAINERS
ka subsystem "EXT4 FILE SYSTEM"
ka indexes                      # indexes you have built
```

Use `-K/--kernel` to pick between indexes when you have several
(`ka -K 6.12.104 info fs/ext4`).

## Customising output

Every listing command takes:

- `--format` — `table` (default), `plain`, `names`, `json`, `csv`, `tree`
- `--columns` — pick and order columns: `kind,name,path,dir,line,span,lines,size,symbols,subdirs,files,flags,subsystem,signature`
- `--limit`, `--sort` (`name`, `path`, `kind`, `line`, `size`, `lines`)
- `--grep` — filter names by regex
- `--with-subsystem` — add a subsystem column
- `--exported` — only `EXPORT_SYMBOL`'d symbols
- `--static-only` / `--no-static`

`names` and `plain` print bare values with no header, so they pipe cleanly:

```bash
ka siblings fs/ext4 -f names | head
ka ls fs/ext4 --kinds function -f json | jq '.[].name'
ka siblings fs/ext4/inode.c -c name,lines,size --sort lines
```

## Call graph (optional)

```bash
kernel-atlas build lts --with-calls --force
ka calls vfs_write             # what it calls
ka calls ext4_get_block --callers
```

This makes the index considerably larger, which is why it is off by default.
`ka trace` does **not** need it.

## How subsystems are determined

The kernel ships `MAINTAINERS`, which is the authoritative statement of who owns
what. `kernel-atlas` parses its ~3,100 sections rather than hardcoding a
subsystem table, so the mapping is always correct for the version you indexed.

Pattern matching follows the rules in that file's own header, notably that `*`
never crosses a `/`:

```
F: drivers/net/     all files in and below drivers/net
F: drivers/net/*    all files in drivers/net, but not below
F: */net/*          all files in "any top level directory"/net
X: fs/ext4/         excluded, even if an F: line above matched
```

When several sections match, the most precise one wins, matching the advice in
`MAINTAINERS` to "look for the most precise areas first". The catch-all `THE
REST` section claims every path in the tree, so it is only reported when nothing
more specific matches — and in that case a plain-English description of the
top-level directory (`fs/` → Filesystems, `mm/` → Memory management) is shown
instead.

## How parsing works, and its limits

C is parsed with [tree-sitter](https://tree-sitter.github.io/), which is fast
and error-tolerant — but it does **not** run the C preprocessor, and the kernel
is extremely macro-heavy. A few kernel idioms are therefore handled explicitly:

- `SYSCALL_DEFINE3(open, ...)` does not parse as a function at all: the macro
  call becomes a statement and the body a *sibling* block. It is reassembled
  into `sys_open`. `COMPAT_SYSCALL_DEFINE4(openat, ...)` correctly becomes
  `compat_sys_openat`, a genuinely different symbol from `sys_openat`.
- `EXPORT_SYMBOL(foo)` / `EXPORT_SYMBOL_GPL(foo)` marks `foo` as available to
  modules.
- `static DECLARE_WORK(free_ipc_work, free_ipc);` declares `free_ipc_work`.
- Trailing attribute macros such as
  `struct sem { ... } ____cacheline_aligned_in_smp;` are not mistaken for
  variable names.

Known limits, stated plainly:

- Code inside `#if` branches is indexed regardless of configuration, so a symbol
  may be listed that your `.config` would not build.
- Functions generated entirely by macros other than the ones above are missed.
- The call graph, when enabled, matches on name only. It does not resolve
  function pointers, which the kernel uses heavily (`->read()`, ops structs).
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
