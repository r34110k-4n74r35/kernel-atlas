"""Parse the kernel's MAINTAINERS file into a fast path -> subsystem matcher.

MAINTAINERS is the kernel's own authoritative statement of which subsystem owns
which files, so subsystem answers stay correct per version instead of relying on
a hand-written table.

Matching semantics follow the rules documented in the file's own header:

    F: drivers/net/    all files in and below drivers/net
    F: drivers/net/*   all files in drivers/net, but not below
    F: */net/*         all files in "any top level directory"/net

so ``*`` never crosses a ``/``.

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
    web: str = ""
    files: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    regexes: list[str] = field(default_factory=list)


def _specificity(pattern: str) -> int:
    """Higher means 'more precise', matching the MAINTAINERS advice to prefer
    the most precise area."""
    if pattern.strip() in _CATCH_ALL:
        return -1000
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    return literal.count("/") * 20 + len(literal)


def _to_regex(pattern: str) -> re.Pattern | None:
    pattern = pattern.strip()
    if not pattern:
        return None
    trailing_dir = pattern.endswith("/")
    core = pattern[:-1] if trailing_dir else pattern
    out: list[str] = []
    for ch in core:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch in ".^$+(){}[]|\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    body = "".join(out)
    # A trailing slash covers the directory itself and everything beneath it.
    suffix = "(/.*)?" if trailing_dir else ""
    try:
        return re.compile(f"^{body}{suffix}$")
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
                sec.web = sec.web or value
            elif tag == "T":
                sec.trees.append(value)
            elif tag == "F":
                sec.files.append(value)
            elif tag == "X":
                sec.excludes.append(value)
            elif tag == "N":
                sec.regexes.append(value)
        if sec.files or sec.regexes:
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

    def __init__(self, sections: list[Section]):
        self.sections = sections
        self._exact: dict[str, list[tuple[int, int]]] = {}
        self._dirs: dict[str, list[tuple[int, int]]] = {}
        self._wild: dict[str, list[tuple[re.Pattern, int, int]]] = {}
        self._excludes: dict[int, list[re.Pattern]] = {}
        self._name_res: list[tuple[re.Pattern, int]] = []
        self._name_probe: list[re.Pattern] = []
        self._build(sections)

    def _build(self, sections: list[Section]) -> None:
        for sec in sections:
            for pat in sec.files:
                score = _specificity(pat)
                has_wild = any(c in pat for c in "*?[")
                if not has_wild:
                    if pat.endswith("/"):
                        self._dirs.setdefault(pat.rstrip("/"), []).append((sec.id, score))
                    else:
                        self._exact.setdefault(pat, []).append((sec.id, score))
                else:
                    rx = _to_regex(pat)
                    if rx is not None:
                        bucket = _literal_prefix_dir(pat)
                        self._wild.setdefault(bucket, []).append((rx, sec.id, score))
            for pat in sec.excludes:
                rx = _to_regex(pat)
                if rx is not None:
                    self._excludes.setdefault(sec.id, []).append(rx)
            for pat in sec.regexes:
                try:
                    self._name_res.append((re.compile(pat), sec.id))
                except re.error:
                    continue

        # One combined alternation per chunk lets the common "matches nothing"
        # case be rejected with a handful of regex calls instead of hundreds.
        for i in range(0, len(self._name_res), 40):
            chunk = self._name_res[i:i + 40]
            try:
                self._name_probe.append(
                    re.compile("|".join(f"(?:{rx.pattern})" for rx, _ in chunk))
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
                for rx, sid in self._name_res[i * 40:(i + 1) * 40]:
                    if rx.search(path):
                        hits[sid] = max(hits.get(sid, -10**9), 5)

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
    return SubsystemMap(parse_maintainers(text))


def top_level_area(path: str) -> tuple[str, str] | None:
    top = path.split("/", 1)[0]
    return TOP_LEVEL_AREAS.get(top)
