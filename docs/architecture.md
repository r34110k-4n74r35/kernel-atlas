# Indexing model and limitations

[Project overview](../README.md) · [Getting started](getting-started.md) · [Commands](commands.md)

- [How subsystems are determined](#how-subsystems-are-determined)
- [How parsing works, and its limits](#how-parsing-works-and-its-limits)

## How subsystems are determined

The kernel ships `MAINTAINERS`, the authoritative statement of who owns what.
`kernel-atlas` derives ownership from the sections in the indexed version's
file, keeping the mapping tied to that source snapshot.

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
  dependency rules are also recognized. Recipe bodies and unexpanded `define`
  templates are excluded from build evidence; custom recipes and dynamically
  generated rules remain a lower-bound limitation.
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
  Kbuild evidence also uses this file inventory: symlinked build files and
  excluded directories cannot add compilation domains or include paths.
- A read or parser failure is stored on the affected file and counted in the
  build summary instead of being reported as a successful parse.
