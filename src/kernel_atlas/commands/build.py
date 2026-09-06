"""Kernel source acquisition and atomic index build orchestration."""

from __future__ import annotations

import shlex
import sqlite3
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..presentation import build as build_output
from .. import config, cparse, indexer, kernelsrc
from ..presentation.progress import Progress


def cmd_build(args, support):
    with build_output.color_mode(args.color):
        return _build(args, support)


@contextmanager
def _source_warning_style():
    """Present acquisition warnings as CLI messages without changing the library."""
    with warnings.catch_warnings():
        original = warnings.showwarning

        def show(message, category, filename, lineno, file=None, line=None):
            if issubclass(category, kernelsrc.UnverifiedRCWarning):
                # Verification warnings remain visible even with --quiet.
                build_output.note("Warning", message, tone="warning", stream=file)
            else:
                original(message, category, filename, lineno, file=file, line=line)

        warnings.showwarning = show
        yield


def _build(args, support):
    quiet = args.quiet
    if args.kinds is None:
        kinds = list(cparse.DEFAULT_KINDS)
    else:
        kinds = support._split_list(args.kinds)
        if not kinds:
            support._die("--kinds must contain at least one symbol kind")
    duplicates = sorted({kind for kind in kinds if kinds.count(kind) > 1})
    if duplicates:
        support._die(
            "duplicate symbol kind(s): " + ", ".join(duplicates))
    bad = [kind for kind in kinds if kind not in cparse.ALL_KINDS]
    if bad:
        support._die(f"unknown symbol kind(s): {', '.join(bad)} "
                     f"(valid: {', '.join(cparse.ALL_KINDS)})")
    if args.with_calls and not ({"function", "syscall"} & set(kinds)):
        support._die(
            "--with-calls requires indexing function and/or syscall symbols")
    missing_call_kinds = {"macro", "variable"} - set(kinds)
    if args.with_calls and missing_call_kinds:
        support._die(
            "--with-calls requires macro and variable symbols so indirect or "
            "macro calls are not falsely linked to unrelated functions")

    if args.src:
        if args.keep_tarball or args.no_verify:
            support._die(
                "--keep-tarball and --no-verify only apply to downloaded source")
        source_arg = Path(args.src).expanduser()
        tree = source_arg.resolve()
        if not (tree / "MAINTAINERS").is_file():
            support._die(
                f"{tree} does not look like a kernel tree (no MAINTAINERS file)")
        if args.version and args.version.lower() in {
                "lts", "longterm", "stable", "mainline", "latest"}:
            support._die(
                f"version alias {args.version!r} does not apply with --src; "
                "omit it to read the tree's Makefile")
        version = args.version or kernelsrc.detect_version(tree)
        if version is None:
            support._die(
                f"could not detect a kernel version from {tree / 'Makefile'}; "
                "pass an explicit version before --src")
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            support._die(str(exc))
        source = str(tree)
        managed_source_version = kernelsrc.managed_source_version(source_arg)
        managed_identity = None
    else:
        spec = args.version or "lts"
        try:
            with Progress("Resolving kernel version", detail=spec, quiet=quiet):
                release = kernelsrc.resolve_version(spec)
        except (OSError, LookupError, ValueError) as exc:
            support._die(str(exc))
        version = release.version
        try:
            version = config.validate_version(version)
        except ValueError as exc:
            support._die(str(exc))
        managed_source_version = version
        managed_identity = None

    out = (Path(args.output).expanduser()
           if args.output else config.index_path(version))
    out = config.require_project_path(out, follow_leaf=False)
    if not args.src and support._path_inside(out, config.source_path(version)):
        support._die(f"index output {out} is inside the source tree "
                     f"{config.source_path(version)}; choose a path outside "
                     "the tree")
    if args.src and support._path_inside(out, tree):
        support._die(f"index output {out} is inside the source tree {tree}; "
                     "choose a path outside the tree")
    build_output.header(
        version, tree if args.src else config.source_path(version), out,
        calls=args.with_calls, workers=args.jobs, quiet=quiet)
    # Every managed build holds its source lock until parsing has finished, and
    # every build holds the output lock until atomic publication has finished.
    # Removal takes the same locks in the same order, so it cannot delete a
    # source tree under a parser or race the final index replacement.
    try:
        with ExitStack() as lifecycle:
            if managed_source_version is not None:
                lifecycle.enter_context(
                    kernelsrc.source_lock(managed_source_version))
            lifecycle.enter_context(kernelsrc.output_lock(out))

            # Repeat mutable output checks under the publication lock.  The
            # earlier source-containment checks are lexical and immutable.
            if out.is_dir():
                support._die(f"index output {out} is a directory")
            if out.exists() and not args.force:
                support._die(
                    f"index already exists at {out} (use --force to rebuild)")

            if not args.src:
                requested_source = (
                    release.source or kernelsrc.tarball_url(version))
                try:
                    with _source_warning_style():
                        tree = kernelsrc.ensure_source(
                            version, keep_tarball=args.keep_tarball, quiet=quiet,
                            verify=not args.no_verify, source_url=requested_source)
                except (OSError, RuntimeError) as exc:
                    support._die(f"could not obtain kernel source: {exc}")
                with Progress("Checking cached source identity", quiet=quiet):
                    managed_identity = kernelsrc.managed_source_identity(version, tree)
                # A kernel.org URL is exact provenance only while the tree still
                # matches the tool-published extraction.  Old, edited, or
                # unverified caches remain usable but are recorded as local.
                source = (managed_identity.source
                          if managed_identity is not None
                          and managed_identity.authoritative else str(tree))

            # ``ensure_source`` is replaceable by callers/tests and a future
            # source provider need not return the conventional cache path.
            if support._path_inside(out, tree):
                support._die(
                    f"index output {out} is inside the source tree {tree}; "
                    "choose a path outside the tree")

            def revalidate_managed_source() -> None:
                if managed_identity is None:
                    return
                current = kernelsrc.managed_source_identity(version, tree)
                if current != managed_identity:
                    raise RuntimeError(
                        "managed source changed while the index was built")

            stats = indexer.build(
                tree, out, version, kinds=kinds, want_calls=args.with_calls,
                jobs=args.jobs, quiet=quiet, source=source,
                managed_tree_identity=(
                    {
                        "managed_tree_id": managed_identity.token,
                        "managed_tree_device": str(managed_identity.device),
                        "managed_tree_inode": str(managed_identity.inode),
                        "managed_tree_digest": managed_identity.digest,
                    }
                    if managed_identity is not None else None),
                pre_publish=(revalidate_managed_source
                             if managed_identity is not None else None))
            size_bytes = out.stat().st_size
            try:
                selectable_by_kernel = (
                    out.resolve() == config.index_path(version).resolve())
            except OSError:
                selectable_by_kernel = False
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        support._die(f"could not build index: {exc}")
    if selectable_by_kernel:
        query_cmd = f"{support.PROG} -K {shlex.quote(out.stem)}"
    else:
        query_cmd = (
            f"{support.PROG} --db {shlex.quote(str(out.resolve()))}")
    build_output.summary(version, out, stats, size=size_bytes,
                         calls=args.with_calls, query_cmd=query_cmd)
