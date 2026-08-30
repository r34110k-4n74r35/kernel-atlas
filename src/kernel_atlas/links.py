"""Links to Bootlin Elixir, git.kernel.org, GitHub and docs.kernel.org."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit


_UPSTREAM_VERSION_RE = re.compile(
    r"v?(?:\d+\.\d+(?:\.\d+){0,2}|\d+\.\d+-rc\d+)\Z")
_LINUX_NEXT_RE = re.compile(r"next-\d{8}\Z")
_KERNEL_ORG_HOSTS = {
    "cdn.kernel.org",
    "git.kernel.org",
    "kernel.org",
    "www.kernel.org",
}


def elixir_tag(version: str) -> str:
    """elixir.bootlin.com uses git tags like v6.18.45; linux-next has no stable tag."""
    v = (version or "").strip()
    if not v or v.startswith("next-"):
        return "latest"
    return v if v.startswith("v") else f"v{v}"


def docs_series(version: str) -> str:
    """docs.kernel.org versions pages by major.minor (v6.18), not the patch level."""
    v = (version or "").lstrip("v")
    parts = v.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"v{parts[0]}.{parts[1]}"
    return "latest"


def _numeric_parts(version: str) -> list[int]:
    match = re.match(r"v?(\d+(?:\.\d+)*)", version or "")
    return [int(part) for part in match.group(1).split(".")] if match else []


def is_upstream_release(version: str) -> bool:
    """Whether *version* follows an upstream Linux release identity.

    Since Linux 3.0, mainline releases use ``X.Y`` and stable updates use
    ``X.Y.Z`` with a positive stable number.  Linux 2.6 used ``2.6.Z`` for
    mainline releases and ``2.6.Z.N`` for stable updates.  Keeping that era
    distinction prevents both routing 2.6 mainline tags to the stable tree and
    inventing modern tags such as ``v6.18.0``.
    """
    value = (version or "").strip()
    if _UPSTREAM_VERSION_RE.fullmatch(value) is None:
        return False
    if "-rc" in value:
        return True

    parts = _numeric_parts(value)
    if parts[:2] == [2, 6]:
        return (len(parts) == 3
                or (len(parts) == 4 and parts[3] > 0))
    if parts and parts[0] >= 3:
        return (len(parts) == 2
                or (len(parts) == 3 and parts[2] > 0))
    # Preserve the prior shape-based handling for older release families.
    return True


def has_upstream_provenance(version: str, source: str | None = None) -> bool:
    """Whether an index can claim links for this upstream release identity.

    A caller which has no source metadata may still use this module as a small
    URL helper. Index-backed callers pass the recorded source: a matching
    kernel.org archive establishes a release reference, while a local path or
    arbitrary archive is not assumed to correspond to that upstream tag.
    """
    value = (version or "").strip()
    if not (is_upstream_release(value) or _LINUX_NEXT_RE.fullmatch(value)):
        return False
    if source is None or source == "kernel.org":
        return True
    parsed = urlsplit(source)
    if parsed.scheme.lower() != "https" or parsed.hostname not in _KERNEL_ORG_HOSTS:
        return False
    archive = unquote(parsed.path.rsplit("/", 1)[-1])
    suffixes = (".tar.xz", ".tar.gz", ".tar.bz2")
    if _LINUX_NEXT_RE.fullmatch(value):
        return value in archive and archive.endswith(suffixes)
    normalized = value.removeprefix("v")
    return any(archive == f"linux-{normalized}{suffix}" for suffix in suffixes)


def is_stable_patch(version: str) -> bool:
    """Whether *version* belongs to the stable tree rather than mainline."""
    if not is_upstream_release(version):
        return False
    parts = _numeric_parts(version)
    if parts[:2] == [2, 6]:
        return len(parts) == 4
    return len(parts) >= 3


def links(version: str, path: str, line: int | None = None, *,
          is_dir: bool = False, ident: str | None = None,
          source: str | None = None) -> dict[str, str]:
    """URLs for a repo-relative path.

    `docs` is only set for Documentation/ files. `ident` is the Elixir
    cross-reference page for a symbol name.
    """
    if not has_upstream_provenance(version, source):
        return {}

    path = (path or "").lstrip("/")
    encoded_path = quote(path, safe="/")
    tag = elixir_tag(version)
    is_next = (version or "").strip().startswith("next-")
    if is_next:
        # Bootlin's ``latest`` and docs.kernel.org's ``latest`` are mainline,
        # not linux-next, and there is no authoritative GitHub mirror.  Emit
        # only the dated ref in the canonical linux-next repository.
        source_ref = (version or "").strip()
        git = ("https://git.kernel.org/pub/scm/linux/kernel/git/next/"
               f"linux-next.git/tree/{encoded_path}?h="
               f"{quote(source_ref, safe='')}")
        if line and not is_dir:
            git += f"#n{line}"
        return {"git": git}

    elixir = (f"https://elixir.bootlin.com/linux/{tag}/source/{encoded_path}" if path
              else f"https://elixir.bootlin.com/linux/{tag}/source")
    if is_stable_patch(version):
        repo = "gregkh/linux"
        git_repo = "stable/linux.git"
        source_ref = tag
    else:
        repo = "torvalds/linux"
        git_repo = "torvalds/linux.git"
        source_ref = tag
    kind = "tree" if is_dir or not path else "blob"
    github = f"https://github.com/{repo}/{kind}/{source_ref}/{encoded_path}".rstrip("/")
    git = ("https://git.kernel.org/pub/scm/linux/kernel/git/"
           f"{git_repo}/tree/{encoded_path}?h={quote(source_ref, safe='')}")
    if line and not is_dir:
        elixir += f"#L{line}"
        github += f"#L{line}"
        git += f"#n{line}"
    out = {"elixir": elixir, "git": git, "github": github}
    if ident:
        out["ident"] = (f"https://elixir.bootlin.com/linux/{tag}/ident/"
                        f"{quote(ident, safe='')}")
    if path.startswith("Documentation/") and path.endswith((".rst", ".txt", ".md")):
        rel = re.sub(r"\.(rst|txt|md)$", ".html", path[len("Documentation/"):])
        out["docs"] = (f"https://www.kernel.org/doc/html/{docs_series(version)}/"
                       f"{quote(rel, safe='/')}")
    return out
