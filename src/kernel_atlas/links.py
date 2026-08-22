"""Links to Bootlin Elixir, git.kernel.org, GitHub and docs.kernel.org."""

from __future__ import annotations

import re
from urllib.parse import quote


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
    parts = []
    for p in (version or "").lstrip("v").split("."):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return parts


def is_stable_patch(version: str) -> bool:
    """Three-part versions (6.18.45) live on the stable tree, not torvalds/linux."""
    return len(_numeric_parts(version)) >= 3


def links(version: str, path: str, line: int | None = None, *,
          is_dir: bool = False, ident: str | None = None) -> dict[str, str]:
    """URLs for a repo-relative path.

    `docs` is only set for Documentation/ files. `ident` is the Elixir
    cross-reference page for a symbol name.
    """
    path = (path or "").lstrip("/")
    tag = elixir_tag(version)
    elixir = (f"https://elixir.bootlin.com/linux/{tag}/source/{path}" if path
              else f"https://elixir.bootlin.com/linux/{tag}/source")
    repo = "gregkh/linux" if is_stable_patch(version) else "torvalds/linux"
    kind = "tree" if is_dir or not path else "blob"
    github = f"https://github.com/{repo}/{kind}/{tag}/{path}".rstrip("/")
    git_repo = ("stable/linux.git" if is_stable_patch(version)
                else "torvalds/linux.git")
    git = ("https://git.kernel.org/pub/scm/linux/kernel/git/"
           f"{git_repo}/tree/{path}?h={tag}")
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
        out["docs"] = f"https://www.kernel.org/doc/html/{docs_series(version)}/{rel}"
    return out
