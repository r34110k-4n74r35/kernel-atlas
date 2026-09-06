"""Conservative Kbuild evidence for compilation domains and C includes.

Build files are read as data; recipes and Make functions are never executed.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from . import cparse

_MAKE_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z0-9_./${}()%-]+)\s*(\+=|:=|\?=|=)\s*(.*?)\s*$")
_MAKE_VAR_RE = re.compile(r"\$[({]([A-Za-z0-9_-]+)[)}]")
_MAKE_ADDPREFIX_RE = re.compile(
    r"\$\(addprefix\s+([^,\s()$]+)\s*,\s*([^()$]*)\)")
_INCLUDE_FLAG_RE = re.compile(r"(?:^|\s)-I\s*([^\s]+)")
_MAKE_RULE_RE = re.compile(r"^\s*([^:#=]+?)\s*:\s*(.*?)\s*$")


def _make_logical_lines(text: str) -> list[str]:
    """Join backslash-continued Make lines without interpreting recipes."""
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        continued = stripped.endswith("\\")
        piece = stripped[:-1] if continued else stripped
        current += (" " if current else "") + piece
        if not continued:
            logical.append(current)
            current = ""
    if current:
        logical.append(current)
    return logical


def _make_evidence_lines(text: str) -> Iterator[str]:
    """Top-level Make evidence, excluding recipes and unexpanded templates."""
    define_depth = 0
    for line in _make_logical_lines(text):
        if line.startswith("\t"):
            continue
        line = line.split("#", 1)[0]
        stripped = line.strip()
        if re.match(r"(?:(?:override|export|private)\s+)*define\s+", stripped):
            define_depth += 1
        elif stripped == "endef" and define_depth:
            define_depth -= 1
        elif not define_depth:
            yield line


def _make_assignments(path: Path) -> tuple[dict[str, list[str]], str]:
    """Return conservative variable assignments from one Kbuild/Makefile.

    This is intentionally not a make interpreter.  Host/user program lists and
    their ``*-objs`` mappings use simple assignments in practice; unknown make
    functions remain unexpanded and are ignored rather than guessed.
    """
    try:
        if path.is_symlink():
            return {}, ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    values: dict[str, list[str]] = defaultdict(list)
    for line in _make_evidence_lines(text):
        match = _MAKE_ASSIGN_RE.match(line)
        if match is None:
            continue
        name, operator, value = match.groups()
        if operator in {"=", ":=", "?="} and name not in values:
            values[name] = [value]
        else:
            values[name].append(value)
    return values, text


def _expand_make_value(value: str, values: dict[str, list[str]]) -> str:
    for _ in range(5):
        expanded = _MAKE_VAR_RE.sub(
            lambda match: (" ".join(values[match.group(1)])
                           if match.group(1) in values else match.group(0)),
            value)
        expanded = _MAKE_ADDPREFIX_RE.sub(
            lambda match: " ".join(
                match.group(1) + token
                for token in match.group(2).split()),
            expanded,
        )
        if expanded == value:
            break
        value = expanded
    return value


def _source_token(directory: str, token: str) -> str | None:
    token = token.strip().replace("${src}/", "").replace("$(src)/", "")
    token = token.replace("${obj}/", "").replace("$(obj)/", "")
    if not token or "$" in token or "%" in token:
        return None
    if token.endswith(".o"):
        token = token[:-2] + ".c"
    elif not token.endswith(".c"):
        token += ".c"
    joined = os.path.normpath(os.path.join(directory, token)).replace(os.sep, "/")
    if joined == ".." or joined.startswith("../"):
        return None
    return joined.removeprefix("./")


def _include_directory(directory: str, token: str) -> str | None:
    """Normalize one ``-I`` operand; ``None`` preserves an opaque search dir.

    An objtree, generated, absolute, or dynamically expanded directory cannot
    be mapped to source-tree identity. It still occupies a real position in the
    compiler's search order, so callers must not silently skip past it and bind
    a later source directory.
    """
    token = token.strip().strip("'\"")
    # objtree paths name generated build output, not source-tree identities.
    # Even an in-tree build cannot prove that an indexed source file is what
    # the compiler will find there, so never alias this provenance to src.
    if any(spelling in token for spelling in (
            "$(objtree)", "${objtree}", "$(obj)", "${obj}")):
        return None
    root_based = any(spelling in token for spelling in (
        "$(srctree)", "${srctree}"))
    directory_based = any(spelling in token for spelling in (
        "$(src)", "${src}"))
    if token.startswith("/") and not root_based and not directory_based:
        return None
    for spelling in ("$(srctree)", "${srctree}"):
        token = token.replace(spelling, "")
    for spelling in ("$(src)", "${src}"):
        token = token.replace(spelling, directory)
    token = token.lstrip("/")
    if "$" in token or "%" in token:
        return None
    if not token:
        return "" if root_based else (directory if directory_based else None)
    if not root_based and not directory_based:
        # Kernel Kbuild, host-tool sub-makes, and standalone tools do not share
        # one working directory. A plain relative operand therefore cannot be
        # assigned a source-tree identity without interpreting that build.
        return None
    normalized = os.path.normpath(token).replace(os.sep, "/").removeprefix("./")
    if normalized == ".." or normalized.startswith("../"):
        return None
    return "" if normalized == "." else normalized


def _kbuild_include_directories(
        tree: Path, *, makefiles: list[Path] | None = None,
        ) -> tuple[
            dict[tuple[str, str], tuple[str | None, ...]],
            dict[tuple[str, str], tuple[str | None, ...]],
            dict[tuple[str, str], tuple[str | None, ...]],
        ]:
    """Return general and target-specific literal Kbuild include paths."""
    evidence: tuple[
        dict[tuple[str, str], list[tuple[int, int, str | None]]], ...
    ] = (defaultdict(list), defaultdict(list), defaultdict(list))
    general, inherited, specific = evidence
    sequence = 0
    if makefiles is None:
        makefiles = sorted({*tree.rglob("Makefile"), *tree.rglob("Kbuild"),
                            *tree.rglob("Build")})
    for makefile in makefiles:
        values, _ = _make_assignments(makefile)
        directory = makefile.parent.relative_to(tree).as_posix()
        if directory == ".":
            directory = ""
        for name, assigned in values.items():
            normalized_name = name.lower().replace("_", "-")
            if "flags-remove" in normalized_name:
                # These operands are removed from _c_flags by Makefile.lib;
                # treating them as active reverses their meaning.
                continue

            target = None
            if name == "KBUILD_CPPFLAGS":
                destination, pipeline, priority = inherited, "kernel", 0
            elif name == "KBUILD_CFLAGS":
                destination, pipeline, priority = inherited, "kernel", 10
            elif normalized_name.startswith("subdir-ccflags-"):
                destination, pipeline, priority = inherited, "kernel", 20
            elif normalized_name.startswith("ccflags-") \
                    or name == "EXTRA_CFLAGS":
                destination, pipeline, priority = general, "kernel", 30
            elif name == "KBUILD_HOSTCFLAGS":
                destination, pipeline, priority = general, "host", 0
            elif name == "HOST_EXTRACFLAGS":
                destination, pipeline, priority = general, "host", 10
            elif name == "KBUILD_USERCFLAGS":
                destination, pipeline, priority = general, "user", 0
            elif name == "userccflags":
                destination, pipeline, priority = general, "user", 10
            elif match := re.fullmatch(r"CFLAGS_(.+\.o)", name):
                destination, pipeline, priority = specific, "kernel", 40
                target = _source_token(directory, match.group(1))
            elif match := re.fullmatch(r"HOSTCFLAGS_(.+\.o)", name):
                destination, pipeline, priority = specific, "host", 40
                target = _source_token(directory, match.group(1))
            elif match := re.fullmatch(r"(.+)-userccflags", name):
                destination, pipeline, priority = specific, "user", 40
                target = _source_token(directory, match.group(1))
            else:
                # AFLAGS, LDFLAGS, RUSTFLAGS, and similarly named variables do
                # not participate in a C compiler's include search path.
                continue
            identity = target if destination is specific else directory
            if identity is None:
                continue
            for value in assigned:
                expanded = _expand_make_value(value, values)
                for match in _INCLUDE_FLAG_RE.finditer(expanded):
                    include = _include_directory(directory, match.group(1))
                    destination[(identity, pipeline)].append(
                        (priority, sequence, include))
                    sequence += 1

    def finalize(rows_by_key):
        finalized: dict[tuple[str, str], tuple[str | None, ...]] = {}
        for key, rows in rows_by_key.items():
            ordered: list[str | None] = []
            for _, _, include in sorted(rows, key=lambda row: row[:2]):
                if include not in ordered:
                    ordered.append(include)
            finalized[key] = tuple(ordered)
        return finalized

    return tuple(finalize(mapping) for mapping in evidence)


def _explicit_rule_sources(
        directory: str, text: str, parsed_sources: set[str]) -> set[str]:
    """Literal sources proven by conservative compile or link dependency rules."""
    compiled: set[str] = set()
    phony = {"all", "clean", "install", "help", "default", "FORCE"}
    for raw in _make_evidence_lines(text):
        match = _MAKE_RULE_RE.match(raw)
        if match is None:
            continue
        targets = match.group(1).split()
        dependencies = match.group(2).split()
        if any("$" in target or "%" in target for target in targets):
            continue
        for target in targets:
            if target.endswith(".o"):
                source = _source_token(directory, target)
                dependency_sources = {
                    _source_token(directory, dependency)
                    for dependency in dependencies if dependency.endswith(".c")
                }
                if source in parsed_sources and source in dependency_sources:
                    compiled.add(source)
                continue
            if Path(target).name in phony or target.startswith("."):
                continue
            for dependency in dependencies:
                if not dependency.endswith(".o") or "$" in dependency:
                    continue
                source = _source_token(directory, dependency)
                if source in parsed_sources:
                    compiled.add(source)
    return compiled


def _is_program_list(name: str) -> bool:
    """Recognize Kbuild variables that name independently linked programs."""
    if name in {"hostprogs", "host-progs", "userprogs", "tprogs-y"}:
        return True
    return re.fullmatch(
        r"(?:hostprogs|userprogs)-always-(?:[ym]|\$\([^)]+\))", name
    ) is not None


def _is_kbuild_object_list(name: str) -> bool:
    """Return whether a make variable provides compile/link object evidence."""
    if re.fullmatch(
            r"(?:obj|lib)-(?:[ymn]|\$\([^)]+\)|\$\{[^}]+\})", name):
        return True
    if name.endswith("-objs"):
        return True
    # Composite objects conventionally use <target>-y/-m.  Exclude the flag
    # families whose values are compiler/linker options rather than objects.
    if name.startswith(("ccflags-", "subdir-ccflags-", "asflags-",
                        "ldflags-", "rustflags-")):
        return False
    return re.fullmatch(
        r".+-(?:[ym]|\$\([^)]+\)|\$\{[^}]+\})", name
    ) is not None


def _special_call_domain(path: str) -> str | None:
    """Return a conservative domain for non-vmlinux target-side images."""
    if path.endswith(".bpf.c"):
        return f"isolated:{path}"
    if path.startswith("drivers/firmware/efi/libstub/"):
        return "image:efi-stub"

    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "arch":
        return None
    arch = parts[1]
    if parts[2] == "boot":
        unit = ("boot-compressed" if len(parts) > 3
                and parts[3] == "compressed" else "boot")
        return f"image:arch:{arch}:{unit}"
    if parts[2] in {"purgatory", "realmode"}:
        return f"image:arch:{arch}:{parts[2]}"

    # vDSO implementation objects form a user-visible shared image.  Keep
    # kernel-side mapping/exception-table glue in the ordinary arch domain.
    vdso_at = next((i for i, part in enumerate(parts[2:-1], 2)
                    if part.startswith("vdso")), None)
    kernel_glue = {"extable.c", "vma.c", "vdso.c", "vdso32-setup.c"}
    if (vdso_at is not None and "include" not in parts[2:vdso_at]
            and parts[-1] not in kernel_glue):
        root = "/".join(parts[:vdso_at + 1])
        return f"image:{root}"
    return None


def _indexed_makefiles(tree: Path, conn: sqlite3.Connection) -> list[Path]:
    """Reuse the scanned tree boundary, including its exclusions and link policy."""
    return [tree / row["path"] for row in conn.execute(
        "SELECT path FROM files WHERE name IN ('Makefile','Kbuild','Build')"
        " AND is_symlink=0 AND index_status='indexed' ORDER BY path")]


def _record_source_includes(tree: Path, conn: sqlite3.Connection, *,
                            makefiles: list[Path] | None = None) -> None:
    """Record C members resolved by source-relative or literal Kbuild paths."""
    existing = {
        row["path"]: row["id"]
        for row in conn.execute(
            "SELECT id,path FROM files WHERE ext IN ('.c','.c_shipped')"
            " AND index_status='parsed'")
    }
    rows: list[tuple[int, int, int]] = []
    if makefiles is None:
        makefiles = _indexed_makefiles(tree, conn)
    general_includes, inherited_includes, specific_includes = \
        _kbuild_include_directories(tree, makefiles=makefiles)
    for row in conn.execute(
            "SELECT id,path,size FROM files"
            " WHERE ext IN ('.c','.c_shipped') "
            "AND index_status='parsed'"):
        try:
            data = (tree / row["path"]).read_bytes()
        except OSError:
            continue
        # Prefilter only the literal operand suffix. Comments and escaped
        # newlines can separate directive tokens, so matching the whole line
        # would drop valid includes. Most C files need no second syntax pass;
        # the syntax tree below rejects commented examples and string literals.
        if b'.c"' not in data and b".c>" not in data:
            continue
        directory = row["path"].rpartition("/")[0]
        seen: set[int] = set()
        for delimiter, token, line in cparse.source_include_directives(data):
            included_path = _source_token(directory, token) \
                if delimiter == '"' else None
            blocked_search = False
            if included_path not in existing:
                # scripts/Makefile.lib orders inherited KBUILD flags before the
                # current directory's ccflags and target-specific CFLAGS. Keep
                # that compiler order. An opaque earlier directory might supply
                # the same basename, so it blocks a confident later binding.
                candidates: set[str] = set()
                pipelines = {
                    pipeline for scope, pipeline in inherited_includes
                    if directory == scope or not scope
                    or directory.startswith(scope + "/")
                }
                pipelines.update(
                    pipeline for scope, pipeline in general_includes
                    if scope == directory)
                pipelines.update(
                    pipeline for target, pipeline in specific_includes
                    if target == row["path"])
                applicable_scopes = sorted(
                    {scope for scope, _ in inherited_includes
                     if directory == scope or not scope
                     or directory.startswith(scope + "/")},
                    key=lambda scope: (scope.count("/") + bool(scope), scope),
                )
                for pipeline in sorted(pipelines):
                    include_dirs: list[str | None] = []
                    for scope in applicable_scopes:
                        include_dirs.extend(inherited_includes.get(
                            (scope, pipeline), ()))
                    include_dirs.extend(general_includes.get(
                        (directory, pipeline), ()))
                    include_dirs.extend(specific_includes.get(
                        (row["path"], pipeline), ()))
                    for include_dir in include_dirs:
                        if include_dir is None:
                            blocked_search = True
                            break
                        candidate = _source_token(include_dir, token)
                        if candidate in existing:
                            candidates.add(candidate)
                            break
                if len(candidates) == 1 and not blocked_search:
                    included_path = next(iter(candidates))
                elif len(candidates) > 1:
                    blocked_search = True
            if (included_path not in existing and not blocked_search
                    and delimiter == '"'):
                # A few kernel translation units spell quoted C members from
                # the source-tree root (for example lib/vdso/*.c).  Prefer the
                # normal includer-relative interpretation, then accept the
                # root spelling only when it names an indexed, parseable file.
                root_path = _source_token("", token)
                if root_path in existing:
                    included_path = root_path
            included_id = existing.get(included_path or "")
            if (included_id is None or included_id == row["id"]
                    or included_id in seen):
                continue
            seen.add(included_id)
            rows.append((row["id"], included_id, line))
    conn.executemany(
        "INSERT INTO source_includes(includer_id,included_id,line)"
        " VALUES (?,?,?)", rows)


def _assign_call_domains(tree: Path, conn: sqlite3.Connection) -> None:
    """Separate kernel objects from independently linked host/user programs.

    Cross-file name resolution is meaningful only inside one linked program.
    Kbuild's hostprogs/userprogs and ``*-objs`` declarations provide explicit
    evidence for those units.  Unmodeled auxiliary trees stay file-isolated;
    this sacrifices uncertain edges instead of linking libc calls to unrelated
    kernel implementations with the same name.
    """
    existing = {row["path"] for row in conn.execute("SELECT path FROM files")}
    parsed_c_ids = {row["path"]: row["id"] for row in conn.execute(
        "SELECT id,path FROM files WHERE ext IN ('.c','.c_shipped')"
        " AND index_status='parsed'")}
    parsed_sources = set(parsed_c_ids)
    explicit: dict[str, set[str]] = defaultdict(set)
    compiled_sources: set[str] = set()

    makefiles = _indexed_makefiles(tree, conn)
    for makefile in makefiles:
        values, text = _make_assignments(makefile)
        directory = makefile.parent.relative_to(tree).as_posix()
        if directory == ".":
            directory = ""
        compiled_sources.update(_explicit_rule_sources(
            directory, text, parsed_sources))
        if not values:
            continue
        # Any literal/expanded object token is direct evidence that its C
        # source can be compiled as a translation-unit root.  This matters for
        # dual-use files such as mm/vma.c and lib/decompress_*.c, which are
        # both objects and quoted members of other sources.
        for name, assigned in values.items():
            if not _is_kbuild_object_list(name):
                continue
            for value in assigned:
                for token in _expand_make_value(value, values).split():
                    if not token.endswith(".o"):
                        continue
                    source = _source_token(directory, token)
                    if source in parsed_c_ids:
                        compiled_sources.add(source)
        program_values = [
            value
            for name, assigned in values.items()
            if _is_program_list(name)
            for value in assigned
        ]
        programs: list[str] = []
        for value in program_values:
            programs.extend(_expand_make_value(value, values).split())

        # A few older in-tree utilities use a standalone BSD-style Makefile.
        # Require both a program/source declaration and explicit userland
        # evidence so ordinary kernel make variables are not reinterpreted.
        custom_sources: list[str] = []
        if "PROG" in values and ("userland app" in text.lower()
                                  or "CSRCS" in values):
            programs.extend(_expand_make_value(
                " ".join(values["PROG"]), values).split())
            for key in ("CSRCS", "SRCS"):
                for value in values.get(key, ()):
                    custom_sources.extend(
                        _expand_make_value(value, values).split())

        for program in programs:
            if "$" in program or program.startswith("-"):
                continue
            base = Path(program).name
            domain = f"program:{directory}:{program}"
            object_values = values.get(f"{base}-objs", ())
            candidates: list[str] = []
            for value in object_values:
                candidates.extend(_expand_make_value(value, values).split())
            if not object_values:
                candidates.append(program)
            if custom_sources:
                candidates.extend(custom_sources)
            for token in candidates:
                source = _source_token(directory, token)
                if source in existing:
                    explicit[source].add(domain)
                    if source in parsed_c_ids:
                        compiled_sources.add(source)

    updates: list[tuple[str, str]] = []
    main_files = {row["path"] for row in conn.execute(
        "SELECT DISTINCT f.path FROM files f JOIN symbols s ON s.file_id=f.id"
        " WHERE s.kind='function' AND s.name='main'")}
    for path in existing:
        domains = explicit.get(path)
        if domains:
            domain = next(iter(domains)) if len(domains) == 1 \
                else f"isolated:{path}"
        else:
            parts = path.split("/")
            special = _special_call_domain(path)
            auxiliary = (parts[0] in {"tools", "scripts", "Documentation"}
                         or "tools" in parts[1:-1]
                         or "Documentation" in parts[1:-1]
                         or path.startswith("arch/um/os-Linux/")
                         or (path.startswith("samples/bpf/")
                             and path.endswith(".c")))
            domain = (special or (f"isolated:{path}"
                                  if auxiliary or path in main_files
                                  else "kernel"))
        if domain != "kernel":
            updates.append((domain, path))
    conn.executemany("UPDATE files SET call_domain=? WHERE path=?", updates)
    conn.executemany(
        "INSERT OR IGNORE INTO translation_unit_roots(file_id) VALUES (?)",
        [(parsed_c_ids[path],) for path in sorted(compiled_sources)],
    )
    _record_source_includes(tree, conn, makefiles=makefiles)
    conn.commit()
