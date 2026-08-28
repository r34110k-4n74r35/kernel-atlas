"""Parse the kernel's MAINTAINERS file into a fast path -> subsystem matcher.

MAINTAINERS is the kernel's own authoritative statement of which subsystem owns
which files, so subsystem answers stay correct per version instead of relying on
a hand-written table.

Matching semantics follow Linux's ``scripts/get_maintainer.pl`` rather than a
generic filesystem glob implementation:

    F: drivers/net/    all files in and below drivers/net
    F: drivers/net/*   all files in drivers/net, but not below
    F: include/drm/drm same-depth paths beginning with that prefix

Single-star expressions may consume slashes while matching, but a non-directory
pattern must have the same slash depth as the candidate.  A trailing slash is
a recursive prefix; ``**`` explicitly disables the depth constraint.  These
quirks matter because MAINTAINERS intentionally relies on them.

Naively testing every pattern against every path is ~5k x ~85k regex calls.
Instead patterns are bucketed by their literal prefix directory, and a path only
tests the buckets that are its own ancestors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Friendly descriptions of the top-level tree. MAINTAINERS answers "who owns
# this file"; this answers "what am I even looking at", which is what a newcomer
# actually needs first.
TOP_LEVEL_AREAS: dict[str, tuple[str, str]] = {
    "arch": ("Architecture", "Per-CPU-architecture code (x86, arm64, riscv...): boot, traps, page tables, syscall entry."),
    "block": ("Block layer", "Generic block device layer: bio submission, request queues, I/O schedulers."),
    "certs": ("Certificates", "Build-time keyring and module-signing certificates."),
    "crypto": ("Crypto API", "Kernel cryptographic algorithm framework and implementations."),
    "Documentation": ("Documentation", "The kernel's own docs, rendered at docs.kernel.org."),
    "drivers": ("Device drivers", "By far the largest area: one subdirectory per device class."),
    "fs": ("Filesystems", "VFS layer plus every individual filesystem (ext4, btrfs, xfs, ...)."),
    "include": ("Headers", "Shared headers. include/linux is the core kernel API; include/uapi is the userspace ABI."),
    "init": ("Init", "Early boot: start_kernel() and friends."),
    "io_uring": ("io_uring", "Asynchronous I/O submission/completion ring interface."),
    "ipc": ("IPC", "System V IPC: message queues, semaphores, shared memory."),
    "kernel": ("Core kernel", "The heart: scheduler, locking, time, cgroups, tracing, bpf, syscall plumbing."),
    "lib": ("Library", "Generic helper routines shared across the kernel."),
    "mm": ("Memory management", "Page allocator, slab, virtual memory, page cache, swap, reclaim."),
    "net": ("Networking", "Network stack: sockets, TCP/IP, netfilter, per-protocol code."),
    "rust": ("Rust support", "Rust abstractions and bindings for kernel APIs."),
    "samples": ("Samples", "Example code demonstrating kernel APIs."),
    "scripts": ("Build scripts", "Kbuild machinery and developer tooling."),
    "security": ("Security", "LSM framework and modules: SELinux, AppArmor, Smack, integrity."),
    "sound": ("Sound", "ALSA: sound core, drivers and SoC support."),
    "tools": ("Tools", "Userspace tools shipped with the kernel (perf, bpftool, selftests)."),
    "usr": ("Initramfs", "Build plumbing for the embedded initial ramdisk."),
    "virt": ("Virtualization", "KVM host-side virtualization support."),
    "LICENSES": ("Licenses", "SPDX license texts shipped with the kernel."),
}

_TAG_RE = re.compile(r"^([A-Z]):\s*(.*)$")
_CATCH_ALL = {"*", "*/", "**"}


@dataclass(slots=True)
class Section:
    id: int
    name: str
    status: str = ""
    maintainers: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    lists: list[str] = field(default_factory=list)
    trees: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    patchwork: list[str] = field(default_factory=list)
    bugs: list[str] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    regexes: list[str] = field(default_factory=list)

    @property
    def web(self) -> str:
        """The first website, retained for callers using the old scalar field."""
        return self.websites[0] if self.websites else ""


def _specificity(pattern: str) -> int:
    """Higher means 'more precise', matching the MAINTAINERS advice to prefer
    the most precise area."""
    if pattern.strip() in _CATCH_ALL:
        return -1000
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    suffix = pattern[len(literal):]
    constrained = len(re.sub(r"[*?\[\]!^/\\]", "", suffix))
    wildcard_constrained = sum(
        len(re.sub(r"[*?\[\]!^\\]", "", component))
        for component in suffix.split("/")
        if any(char in component for char in "*?[")
    )
    # Preserve the strong prefix/depth signal while rewarding constraints
    # after a wildcard.  Linux uses patterns such as ``clock/*imx*`` precisely
    # to be more specific than the containing ``clock/`` directory.
    return (literal.count("/") * 20 + len(literal)
            + constrained + wildcard_constrained)


def _regex_specificity(pattern: str) -> int:
    """A name regex should beat a broad wildcard but not an exact deep path.

    ``N: imx`` is semantically more precise than ``F: arch/*/boot/dts/`` even
    though it has no path prefix.  The longest literal-looking run is a useful,
    deterministic proxy; scores remain capped below a typical exact deep file.
    """
    runs = re.findall(r"[A-Za-z0-9_/-]+", pattern)
    longest = max((len(run) for run in runs), default=0)
    return 30 + min(50, longest * 2)


def _to_regex(pattern: str) -> re.Pattern | None:
    """Compile the F:/X: matcher used by Linux's get_maintainer.pl.

    The upstream script escapes dots, translates shell wildcards to regex
    fragments, anchors only at the start, and applies a separate slash-count
    check to non-directory patterns (except ``**``).  Encoding the depth check
    as a lookahead keeps exclusions and inclusion patterns consistent.
    """
    pattern = pattern.strip()
    if not pattern:
        return None
    trailing_dir = pattern.endswith("/")
    crosses_depth = "**" in pattern
    slash_depth = pattern.count("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append("(?:.*)")
                i += 1
            else:
                out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == ".":
            out.append(r"\.")
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end < 0:
                out.append(r"\[")
            else:
                out.append(pattern[i:end + 1])
                i = end
        else:
            # get_maintainer.pl otherwise leaves the pattern regex-compatible,
            # including its rare escaped wildcard and grouping constructs.
            out.append(ch)
        i += 1
    body = "".join(out)
    depth = ""
    if not trailing_dir and not crosses_depth:
        depth = rf"(?=(?:[^/]*/){{{slash_depth}}}[^/]*$)"
    try:
        return re.compile(f"^{depth}{body}")
    except re.error:
        return None


def _literal_prefix_dir(pattern: str) -> str:
    """Deepest directory of the pattern that contains no wildcard."""
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    if "/" not in literal:
        return ""
    return literal.rsplit("/", 1)[0]


def _ancestors(path: str):
    """'' , 'fs', 'fs/ext4' ... for 'fs/ext4/inode.c'."""
    yield ""
    parts = path.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def parse_maintainers(text: str) -> list[Section]:
    sections: list[Section] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        first_tag = next((i for i, ln in enumerate(block) if _TAG_RE.match(ln)), None)
        if first_tag is None or first_tag == 0:
            block.clear()
            return
        name = " ".join(ln.strip() for ln in block[:first_tag]).strip()
        sec = Section(id=len(sections), name=name)
        for line in block[first_tag:]:
            m = _TAG_RE.match(line)
            if not m:
                continue
            tag, value = m.group(1), m.group(2).strip()
            if not value:
                continue
            if tag == "M":
                sec.maintainers.append(value)
            elif tag == "R":
                sec.reviewers.append(value)
            elif tag == "L":
                sec.lists.append(value)
            elif tag == "S":
                sec.status = value
            elif tag == "W":
                sec.websites.append(value)
            elif tag == "Q":
                sec.patchwork.append(value)
            elif tag == "B":
                sec.bugs.append(value)
            elif tag == "C":
                sec.chats.append(value)
            elif tag == "P":
                sec.profiles.append(value)
            elif tag == "T":
                sec.trees.append(value)
            elif tag == "F":
                sec.files.append(value)
            elif tag == "X":
                sec.excludes.append(value)
            elif tag == "N":
                sec.regexes.append(value)
            elif tag == "K":
                sec.keywords.append(value)
        # Sections without F:/N: patterns still carry useful contact and
        # workflow metadata (BCACHEFS and BPF [MISC] are real examples).  They
        # cannot claim a path, but they remain valid subsystem records.
        sections.append(sec)
        block.clear()

    for raw in text.splitlines():
        if raw.strip():
            block.append(raw)
        else:
            flush()
    flush()
    return sections


class SubsystemMap:
    """Resolve a repo-relative path to the MAINTAINERS sections that claim it."""

    def __init__(self, sections: list[Section], tree: Path | None = None):
        self.sections = sections
        self._tree = tree
        self._exact: dict[str, list[tuple[int, int]]] = {}
        self._dirs: dict[str, list[tuple[int, int]]] = {}
        self._wild: dict[str, list[tuple[re.Pattern, int, int]]] = {}
        self._excludes: dict[int, list[re.Pattern]] = {}
        self._name_res: list[tuple[re.Pattern, int, int]] = []
        self._name_probe: list[re.Pattern | None] = []
        self._build(sections)

    def _is_existing_dir(self, pattern: str) -> bool:
        return self._tree is not None and not pattern.endswith("/") and \
            not any(c in pattern for c in "*?[") and \
            (self._tree / pattern).is_dir()

    def _build(self, sections: list[Section]) -> None:
        for sec in sections:
            for pat in sec.files:
                score = _specificity(pat)
                has_wild = any(c in pat for c in "*?[")
                if not has_wild and (pat.endswith("/")
                                     or self._is_existing_dir(pat)):
                    self._dirs.setdefault(pat.rstrip("/"), []).append(
                        (sec.id, score))
                else:
                    normalized = pat + "/" if self._is_existing_dir(pat) else pat
                    rx = _to_regex(normalized)
                    if rx is not None:
                        bucket = _literal_prefix_dir(pat)
                        self._wild.setdefault(bucket, []).append((rx, sec.id, score))
            for pat in sec.excludes:
                normalized = pat + "/" if self._is_existing_dir(pat) else pat
                rx = _to_regex(normalized)
                if rx is not None:
                    self._excludes.setdefault(sec.id, []).append(rx)
            for pat in sec.regexes:
                try:
                    self._name_res.append(
                        (re.compile(pat), sec.id, _regex_specificity(pat)))
                except re.error:
                    continue

        # One combined alternation per chunk lets the common "matches nothing"
        # case be rejected with a handful of regex calls instead of hundreds.
        for i in range(0, len(self._name_res), 40):
            chunk = self._name_res[i:i + 40]
            # Combining capturing regexes changes numeric backreference group
            # numbers.  Fall back to the individual expressions for that rare
            # chunk; current kernel patterns are capture-free, so the fast path
            # remains the common one.
            if any(rx.groups for rx, _, _ in chunk):
                self._name_probe.append(None)
                continue
            try:
                self._name_probe.append(
                    re.compile("|".join(f"(?:{rx.pattern})" for rx, _, _ in chunk))
                )
            except re.error:
                self._name_probe.append(None)

    def match(self, path: str) -> list[tuple[Section, int]]:
        """Sections claiming `path`, most precise first."""
        hits: dict[int, int] = {}

        for sid, score in self._exact.get(path, ()):
            hits[sid] = max(hits.get(sid, -10**9), score)

        for anc in _ancestors(path):
            for sid, score in self._dirs.get(anc, ()):
                hits[sid] = max(hits.get(sid, -10**9), score)
            for rx, sid, score in self._wild.get(anc, ()):
                if rx.match(path):
                    hits[sid] = max(hits.get(sid, -10**9), score)
        # Directories are themselves keys in the dir index.
        for sid, score in self._dirs.get(path, ()):
            hits[sid] = max(hits.get(sid, -10**9), score)

        for i, probe in enumerate(self._name_probe):
            if probe is None or probe.search(path):
                for rx, sid, score in self._name_res[i * 40:(i + 1) * 40]:
                    if rx.search(path):
                        hits[sid] = max(hits.get(sid, -10**9), score)

        out: list[tuple[Section, int]] = []
        for sid, score in hits.items():
            if any(rx.match(path) for rx in self._excludes.get(sid, ())):
                continue
            out.append((self.sections[sid], score))
        out.sort(key=lambda t: (-t[1], t[0].name))
        return out


def load(tree: Path) -> SubsystemMap:
    path = tree / "MAINTAINERS"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return SubsystemMap(parse_maintainers(text), tree)


def top_level_area(path: str) -> tuple[str, str] | None:
    top = path.split("/", 1)[0]
    return TOP_LEVEL_AREAS.get(top)
