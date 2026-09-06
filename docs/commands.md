# Command reference

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

Versioned examples illustrate Linux 6.18.46; names, counts, and line numbers vary by release.

- [versions / build / indexes / use / remove](#versions--build--indexes--use--remove)
- [stats](#stats)
- [check / doctor](#check--doctor)
- [info](#info)
- [struct / structure](#struct--structure)
- [siblings (sib) / ls](#siblings-sib--ls)
- [tree](#tree)
- [find](#find)
- [path / show](#path--show)
- [web](#web)
- [docs](#docs)
- [locate](#locate)
- [trace](#trace)
- [subsystems / subsystem](#subsystems--subsystem)
- [calls](#calls)
- [relationships (rels)](#relationships-rels)
- [Controlling the output](#controlling-the-output)

## Commands

### `versions` / `build` / `indexes` / `use` / `remove`

See [Building indexes](getting-started.md#building-indexes) and
[Choosing a kernel version](getting-started.md#choosing-a-kernel-version). After a successful
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

See [The main idea: "same level"](getting-started.md#the-main-idea-same-level). Combine
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

### `web`

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
`--url elixir|ident|git|github|docs` prints a single URL. Three-part modern
versions (6.18.46) use the stable tree and `gregkh/linux`; two-part versions
(7.2) use torvalds. Historical 2.6 releases use three components for mainline
and four for stable updates. docs.kernel.org is versioned by major.minor (`v6.18`), not
the patch level. `ka web` reports that no upstream release-reference URL is
available for a locally supplied or vendor source tree.

```bash
open "$(ka web tcp_sendmsg --url elixir)"
open "$(ka web Documentation/mm/index.rst --url docs)"
```

### `docs`

`docs` lists `Documentation/` files that belong with a target. Ranking combines
several independent signals before applying `--limit`: an exact/contained
Documentation path, primary-owner claims, semantic names from the target and
its declarations, path terms, and known code-to-doc area aliases. This keeps a
specific guide ahead of an incidental broad-owner match. Bare names like `bpf`
mean the *area* (`kernel/bpf/`), not the LSM hook variable of the same name.

When studying a C file or symbol, prose guides rank before device-tree bindings,
schemas, and build files within the same evidence tier. Symbol names contribute
even when the containing file has no specific `MAINTAINERS` owner. Explicit
Documentation targets keep priority, including when the target is a binding.

Use `--under PATH` to restrict candidates to a file or directory inside
`Documentation/` **before** applying the limit. Both `driver-api/usb` and
`Documentation/driver-api/usb` work. Paths are literal and case-sensitive;
wildcard characters are not interpreted. `-n 0` returns all related matches
in the selected scope. The scope restricts related results; it does not make
unrelated documents match.

Use `--explain` to print why each document was selected. In JSON, this adds a
`reasons` array to each result. Without it, the existing JSON fields remain
unchanged. Explanations describe path, name, ownership, and document-type
evidence; document bodies are not searched, and relevance is not guaranteed.

```bash
ka docs mm
ka docs bpf                 # notes that it used kernel/bpf/
ka docs kernel/bpf
ka docs usb_device --under driver-api/usb --explain
ka docs usb_device --under devicetree/bindings -n 10
ka docs usb_device --explain -f json
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
