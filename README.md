# kernel-atlas

Explore Linux kernel structures and the relationships between subsystems from
an offline source index. Start with a function, C structure, file, or directory;
follow its members, callers, owners, and related documentation.

The index combines the source tree, C declarations and comments, and the
kernel's `MAINTAINERS` file. Call analysis distinguishes resolved identities
from ambiguous, macro, and indirect invocations so you can see what the source
actually establishes.

## Get started

Requires Python 3.10+. Install from a checkout:

```bash
git clone https://github.com/r34110k-4n74r35/kernel-atlas.git
cd kernel-atlas
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate

ka build lts --with-calls
ka info mm
ka struct usb_device
ka docs usb_device --under driver-api/usb --explain
```

For a source tree you already have:

```bash
ka build --src /path/to/linux --with-calls
```

The version is read from its Makefile. Use `ka indexes` to list available
snapshots and `ka use VERSION` to select one. `ka --help` and `ka COMMAND --help`
show the command options. `ka` and `kernel-atlas` are equivalent entry points.

A large kernel needs several GB of disk space for source and index; rebuilding
also needs temporary space for a second index. See the
[installation and storage guide](docs/getting-started.md).

## Study a subsystem boundary

```bash
ka struct usb_device                  # members, types, comments, conditions
ka show usb_get_dev                   # source for a function using the structure
ka calls usb_get_dev                  # resolved callees and uncertain calls
ka calls usb_get_dev --callers -n 20   # who invokes it?
ka info drivers/usb                   # ownership composition of the directory
ka relationships 'USB SUBSYSTEM'      # shared ownership and cross-subsystem calls
ka docs usb_device --explain          # related guides and ranking evidence
```

A structure report retains nested members, arrays, bitfields, function
pointers, typedef aliases, and source documentation. It reports missing
documentation and uncertain parsing. Ambiguous structure names require a
qualified selector such as `include/linux/usb.h:usb_device`, or `--all`.

For a guided walk through memory management, scheduling, networking, filesystems,
and device drivers, use the [study guide](docs/study-guide.md).

## Choose a command

| Question | Command |
| --- | --- |
| Where is a declaration? | `ka find tcp_sendmsg --exact` |
| What surrounds this code? | `ka siblings kernel/sched` or `ka ls mm` |
| What does a structure contain? | `ka struct usb_device` |
| Who owns this area? | `ka info drivers/usb` |
| How do subsystems interact? | `ka relationships 'USB SUBSYSTEM'` |
| Which functions are connected? | `ka calls usb_get_dev` |
| Which guides should I read? | `ka docs usb_device --under driver-api --explain` |
| Did a symbol move across releases? | `ka locate tcp_sendmsg` |
| Which code is in a stack trace? | `ka trace` with a log on stdin |
| Is the index consistent? | `ka check` |

Use `-f json` for structured output. `-K VERSION` or `--db PATH` selects an index
for a command. The [command reference](docs/commands.md) covers filtering,
ambiguity, output fields, and examples for every command.

## What the results mean

- Queries work offline. Downloads and release discovery contact kernel.org;
  `ka web` prints links without opening them.
- Source is indexed without running the preprocessor. Conditional definitions
  are possibilities, and structure byte layouts require a configured build.
- `MAINTAINERS` ownership can overlap. Directory reports retain mixed ownership;
  call flows use resolved identities and disjoint primary-owner sets.
- Documentation suggestions use indexed paths, declaration names, and ownership.
  `--explain` shows those signals; it does not claim a document mentions a symbol.
- Indexes are snapshots. Rebuild after editing source. Local/custom source
  indexes support `path` and `show`; upstream links require matching provenance.

See [the indexing model and limitations](docs/architecture.md) for the evidence
behind these results.

## Documentation

| Guide | Contents |
| --- | --- |
| [Getting started](docs/getting-started.md) | Installation, storage, versions, builds, targets, scopes |
| [Command reference](docs/commands.md) | Commands, options, output formats, examples |
| [Study guide](docs/study-guide.md) | Kernel areas and practical exploration workflows |
| [Indexing model](docs/architecture.md) | Ownership, parsing, call resolution, limitations |
| [Troubleshooting](docs/troubleshooting.md) | Ambiguity, source availability, index and pin problems |
| [Development](docs/development.md) | Module responsibilities, tests, compatibility |

An editable checkout keeps downloaded source in `kernels/` and indexes in
`indexes/`. Other installations use `~/.kernel-atlas`; `KERNEL_ATLAS_HOME`
overrides the location. Generated data is Git-ignored. Index metadata and local
Python environments retain absolute workspace paths, so review them before
sharing a workspace archive.
