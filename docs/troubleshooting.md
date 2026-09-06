# Troubleshooting

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

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
If `--under` is set, broaden or remove that scope. Use `--explain` on returned
results to inspect the ranking evidence; `--under driver-api` can focus code
study on API guides.

**"cannot read the default version pin"** — the pin is malformed or unreadable.
`ka use --clear` removes a malformed pin, and `ka use VERSION` selects a new
one. For a permission or I/O error, correct access to `indexes/.default-version`
before retrying. An explicit `--db PATH` selects an index independently of the pin.

**"pinned version has no index any more"** — you `remove`d the version `use`
was pointing at (or the pin is stale). `ka use --clear` or `ka use <other>`.

**"not a usable index" / "unsupported index schema"** — the file is partial,
corrupt, or was built with an incompatible schema. Rebuild that version with
`--force`. Completed builds are validated and moved into place atomically.

**The index disagrees with the file I just edited** — the index is a snapshot.
Rebuild with `--force` after editing its managed or custom source tree. An
edited managed cache is intentionally recorded as local source, so that rebuilt
index does not claim upstream `web` links for content it can no longer attest.
