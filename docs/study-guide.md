# Studying subsystem boundaries

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

Versioned examples illustrate Linux 6.18.46; names, counts, and line numbers vary by release.

Start from the data exchanged at a boundary. For USB, `ka struct usb_device`
shows the generic `struct device` member alongside USB-specific state. Follow
with `ka show usb_get_dev` and `ka calls usb_get_dev` to inspect how USB code
uses the driver core. `ka info drivers/usb` supplies ownership context, and
`ka relationships 'USB SUBSYSTEM'` summarizes resolved interactions.

Read the API documentation alongside the code:

```bash
ka docs usb_device --under driver-api/usb --explain
```

Treat those suggestions as a reading list based on source names and ownership.
Use `ka show` on a returned path to inspect its contents. If you are studying
hardware description instead, change the scope to `devicetree/bindings`.

- [A tour across subsystems](#a-tour-across-subsystems)
- [Typical workflows](#typical-workflows)

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
`ka show Documentation/mm/index.rst`. Use `ka docs usb_device --mentions` or
`ka docs --search 'reference count'` for actual text excerpts instead of
related-guide ranking.

**I am following data through a subsystem boundary.** Start with
`ka struct usb_device --relations` to see member references and declarations
that use the type. `--used-by` focuses on the latter. Follow candidate types
with another `ka struct`, then use owners and documentation to understand the
boundary. Header visibility and conditional alternatives remain uncertain;
declaration references do not prove runtime object flow.

**I am following a function across layers.** Inspect
`ka info usb_get_dev --detail`, then `ka calls usb_get_dev --depth 3 --sites`.
The first shows documented requirements; the second gives resolved source
edges and their invocation lines. For a specific destination, use
`ka calls usb_get_dev --to get_device`. Review unresolved boundaries and search
limits before drawing conclusions about a missing chain.

**Open it in an editor.** `vim "$(ka path tcp_sendmsg)"` or
`code -g "$(ka path tcp_sendmsg --line)"`. `--line` appends `:LINE`.
`path` / `show` need the exact source tree recorded in the index (normally
under `kernels/`, or the original `--src` tree); most other commands do not.
