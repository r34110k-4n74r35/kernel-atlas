"""CLI handlers for kernel source and index lifecycle operations.

The public command functions remain in :mod:`kernel_atlas.cli`.  They pass that
module as ``support`` so command code can reuse its stable selection, error, and
path-safety helpers without introducing an import cycle.
"""

from __future__ import annotations

import shlex
import shutil
import sqlite3
import sys
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .. import build_output, config, cparse, db, indexer, kernelsrc, maintainers, render
from ..progress import Progress
from ..terminal import Console, format_size


def cmd_versions(args, support):
    try:
        releases = kernelsrc.list_releases()
    except (OSError, ValueError) as exc:
        support._die(f"could not reach kernel.org ({exc})")
    if args.format == "json":
        sys.stdout.write(render.render_json([r.__dict__ for r in releases]))
        return
    console = Console(args.color)
    console.heading("Current kernel.org releases")
    console.table(
        ("Release", "Version", "Released", "Notes"),
        [(release.moniker, release.version, release.released or "—",
          "Good default for learning" if release.is_lts else "")
         for release in releases],
        tones={1: "accent"},
        row_tones=["success" if release.is_lts else None
                   for release in releases],
    )
    console.blank()
    console.heading("Build an index")
    console.text("Choose lts, stable, mainline, or a version from the table.", tone="muted")
    console.commands([f"{support.PROG} build lts"])


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


def cmd_indexes(args, support):
    paths = config.list_indexes()
    if not paths and args.format != "json":
        console = Console(args.color)
        console.note("no indexes yet")
        console.commands([f"{support.PROG} build lts"])
        return
    active = support.default_index() if paths else None
    rows = []
    sizes = {}
    for path in paths:
        conn = None
        error = None
        try:
            conn = db.connect(path, readonly=True)
            meta = db.validate_schema(conn)
        except (sqlite3.DatabaseError, OSError) as exc:
            meta = {}
            error = str(exc)
        finally:
            if conn is not None:
                conn.close()
        source_here = support.find_source_tree({
            "index_stem": path.stem,
            "kernel_version": meta.get("kernel_version", path.stem),
            "tree_path": meta.get("tree_path"),
        }) is not None
        version = meta.get("kernel_version") or path.stem
        size = path.stat().st_size
        sizes[str(path)] = size
        rows.append({
            "version": version,
            "alias": path.stem,
            "files": meta.get("n_files", "?"),
            "symbols": meta.get("n_symbols", "?"),
            "calls": meta.get("has_calls") == "1",
            "source": source_here,
            "built_at": meta.get("built_at", "?"),
            "size": f"{size / 1048576:.0f} MB",
            "default": support._same_path(path, active),
            "path": str(path),
            "error": error,
        })
    rows.sort(
        key=lambda row: support._version_key(Path(f"{row['version']}.db")),
        reverse=True,
    )
    if args.format == "json":
        sys.stdout.write(render.render_json(rows))
        return
    console = Console(args.color)
    console.heading("Built indexes")
    show_alias = any(row["alias"] != row["version"] for row in rows)
    headers = ["Default", "Version"]
    if show_alias:
        headers.append("Index")
    headers += ["State", "Files", "Symbols", "Calls", "Source", "Built", "Size"]
    display_rows = []
    for row in rows:
        values = ["*" if row["default"] else "—", row["version"]]
        if show_alias:
            values.append(row["alias"])
        state = "broken" if row["error"] else "ok"
        values += [state, _count(row["files"]), _count(row["symbols"]),
                   "yes" if row["calls"] else "no",
                   "yes" if row["source"] else "no", row["built_at"],
                   format_size(sizes[row["path"]])]
        display_rows.append(values)
    console.table(
        headers, display_rows,
        align_right=tuple(headers.index(name) for name in ("Files", "Symbols", "Size")),
        tones={1: "accent"},
        row_tones=["error" if row["error"] else "success" if row["default"] else None
                   for row in rows],
    )
    for row in rows:
        if row["error"]:
            console.note(f"{row['alias']} unusable: {row['error']} (rebuild this index)",
                         tone="error")
    pinned = support._default_version_pin()
    note = (f"pinned with '{support.PROG} use {pinned}'" if pinned
            else f"highest version (pin one with "
                 f"'{support.PROG} use <version>')")
    console.blank()
    console.text(f"* = default index — {note}", tone="muted")


def _count(value):
    """Group known numeric counts without hiding unavailable metadata."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def cmd_use(args, support):
    console = Console(args.color)
    if args.clear and args.version:
        support._die("pass a version or --clear, not both")
    if args.clear:
        invalid_pin = False
        try:
            with kernelsrc.pin_lock():
                try:
                    was = config.get_default_version()
                except ValueError:
                    # ``use --clear`` is the explicit recovery path for a
                    # malformed, hand-edited pin.  I/O failures still abort.
                    was = None
                    invalid_pin = True
                config.clear_default_version()
        except OSError as exc:
            support._die(f"could not clear the default pin: {exc}")
        if was:
            console.note(f"cleared pin on {was}; the highest built version is the "
                         "default again", tone="success")
        elif invalid_pin:
            console.note("cleared invalid default pin", tone="success")
        else:
            console.note("nothing was pinned", tone="muted")
        return
    if not args.version:
        available = config.list_indexes()
        pinned = support._default_version_pin()
        if not available:
            console.note("no indexes built yet")
            console.commands([f"{support.PROG} build lts",
                              f"{support.PROG} use <version>"])
            return
        console.heading("Default index")
        if pinned:
            pin_path = config.index_path(pinned)
            if pin_path.is_file():
                console.field("Pinned", pinned, tone="accent")
            else:
                console.field("Pinned", pinned, tone="warning")
                console.note("The pinned index is gone; clear the pin or select an index.")
                console.commands([f"{support.PROG} use --clear",
                                  f"{support.PROG} use <version>"])
        else:
            console.field("Selection", "Highest built version (nothing pinned)")
        active = support.default_index(warn=False)
        conn = None
        try:
            conn = db.connect(active, readonly=True)
            db.validate_schema(conn)
        except (OSError, sqlite3.DatabaseError) as exc:
            support._die(
                f"active index {active} is not usable ({exc}); rebuild it or "
                f"select another with '{support.PROG} use <version>'")
        finally:
            if conn is not None:
                conn.close()
        console.field("Active index", active.stem, tone="success")
        console.field("Path", active)
        return
    path = support.resolve_index_spec(args.version)
    conn = None
    try:
        with kernelsrc.output_lock(path):
            conn = db.connect(path, readonly=True)
            db.validate_schema(conn)
            conn.close()
            conn = None
            # Removal uses the same lock, so the validated leaf cannot vanish
            # before its selection alias is durably pinned.
            with kernelsrc.pin_lock():
                config.set_default_version(path.stem)
    except (OSError, sqlite3.DatabaseError) as exc:
        support._die(
            f"cannot use {path.stem!r}: {path} is not a usable index ({exc})")
    finally:
        if conn is not None:
            conn.close()
    console.heading(f"Default index is now {path.stem}", tone="success")
    console.field("Path", path)
    console.text("Every command without -K/--db will use this index.", tone="muted")
    console.blank()
    console.heading("Clear the selection")
    console.commands([f"{support.PROG} use --clear"])


def cmd_remove(args, support):
    console = Console(args.color)
    errors = Console(args.color, stream=sys.stderr)
    unique: list[Path] = []
    for spec in args.versions:
        path = support.resolve_index_spec(spec)
        if path not in unique:
            unique.append(path)

    # Read metadata once to choose the source lock, then verify it again while
    # both the source and output are locked.  A concurrently replaced alias can
    # therefore cause a conservative "source kept", never an unlocked delete.
    managed_sources = {
        path: (support._managed_source_record(path)
               if args.source else None)
        for path in unique
    }

    freed = 0
    failures = 0
    completed_sources: dict[
        tuple[str, str], kernelsrc.ManagedSourceIdentity] = {}
    console.heading("Removing indexes")
    for path in unique:
        alias = path.stem
        record = managed_sources[path]
        tree = record[0] if record is not None else None
        recorded_identity = record[1] if record is not None else None
        managed_version = None
        if tree is not None and tree.name.startswith("linux-"):
            try:
                candidate = config.validate_version(tree.name[len("linux-"):])
                if tree == config.source_path(candidate):
                    managed_version = candidate
            except ValueError:
                pass
        if tree is not None and managed_version is None:
            # This should not occur for _managed_source_recorded_by's
            # conventional result, but fail closed if a compatibility shim
            # returns a path whose lock identity cannot be derived.
            tree = None
        source_key = (
            (str(tree), (recorded_identity or {}).get("managed_tree_id", ""))
            if tree is not None else None)
        marker_to_clear = None

        try:
            with ExitStack() as lifecycle:
                # Lock order deliberately matches cmd_build.
                if managed_version is not None:
                    lifecycle.enter_context(
                        kernelsrc.source_lock(managed_version))
                lifecycle.enter_context(kernelsrc.output_lock(path))

                if args.source:
                    current = support._managed_source_record(path)
                    if tree is None:
                        console.note(
                            "source kept (the index does not identify a "
                            "matching managed source tree)")
                    elif (current is None
                          or not support._same_path(current[0], tree)
                          or current[1] != recorded_identity):
                        console.note(
                            "source kept (the index changed while removal "
                            "was waiting for its lifecycle lock)")
                    elif source_key in completed_sources:
                        marker_to_clear = completed_sources[source_key]
                        console.text(f"source already removed at {tree}", tone="muted")
                    else:
                        identity = kernelsrc.source_identity_marker(
                            managed_version)
                        expected = recorded_identity or {}
                        matches_index = (
                            identity is not None
                            and identity.token == expected.get("managed_tree_id")
                            and str(identity.device)
                            == expected.get("managed_tree_device")
                            and str(identity.inode)
                            == expected.get("managed_tree_inode")
                            and identity.digest
                            == expected.get("managed_tree_digest")
                        )
                        if not matches_index:
                            errors.note(
                                f"could not remove source {tree}: the current "
                                "tree/ownership marker is not the pristine "
                                "tool-owned source "
                                "recorded by this index; index kept",
                                tone="error",
                            )
                            failures += 1
                            continue
                        try:
                            removal = kernelsrc.prepare_source_removal(
                                managed_version, identity)
                        except (OSError, RuntimeError, ValueError) as exc:
                            errors.note(
                                f"could not remove source {tree}: {exc}; "
                                "index kept",
                                tone="error",
                            )
                            failures += 1
                            continue
                        if removal is None:
                            errors.note(
                                f"could not remove source {tree}: its "
                                "ownership marker changed; index kept",
                                tone="error",
                            )
                            failures += 1
                            continue
                        identity = removal.identity
                        if removal.already_absent:
                            if tree.exists() or tree.is_symlink():
                                console.note(
                                    "recorded source is already removed; "
                                    f"current entry kept at {tree}")
                            else:
                                console.text(f"source is already absent at {tree}",
                                             tone="muted")
                        else:
                            try:
                                shutil.rmtree(config.require_project_path(
                                    removal.quarantine))
                            except (OSError, ValueError) as exc:
                                errors.note(
                                    f"could not remove source {tree} from "
                                    f"quarantine {removal.quarantine}: {exc}; "
                                    f"anything at {tree} is untouched",
                                    tone="error",
                                )
                                failures += 1
                                # The nonce-derived quarantine and index retain
                                # authorization for an exact later retry.
                                continue
                            console.field("removed source", tree, tone="success")
                        assert source_key is not None
                        completed_sources[source_key] = identity
                        marker_to_clear = identity

                try:
                    size = support._unlink_index(path)
                except OSError as exc:
                    errors.note(f"could not remove index {path}: {exc}", tone="error")
                    failures += 1
                    continue
                freed += size
                console.field("removed index", f"{path}  ({format_size(size)})",
                              tone="success")

                if marker_to_clear is not None:
                    try:
                        kernelsrc.clear_source_identity(
                            managed_version, marker_to_clear.token)
                    except OSError as exc:
                        errors.note(
                            "source and index were removed, but could not "
                            f"clear ownership marker: {exc}", tone="error")
                        failures += 1

                try:
                    with kernelsrc.pin_lock():
                        if config.get_default_version() == alias:
                            config.clear_default_version()
                            console.text(
                                "The pinned default was removed; the pin has been cleared.",
                                tone="muted")
                except (OSError, ValueError) as exc:
                    errors.note(f"could not clear the default pin: {exc}", tone="error")
                    failures += 1
        except (OSError, RuntimeError, ValueError) as exc:
            errors.note(f"could not lock lifecycle for {path}: {exc}", tone="error")
            failures += 1
            continue

        if not args.source:
            try:
                kept_tree = config.source_path(alias)
            except ValueError:
                kept_tree = None
            if kept_tree is not None and kept_tree.is_dir():
                console.text(f"source kept at {kept_tree}; remove it too with --source",
                             tone="muted")
    console.blank()
    console.heading("Removal summary", tone="warning" if failures else "success")
    console.field("Freed", f"{format_size(freed)} of index files")
    if args.source:
        console.text("Source tree sizes are not counted.", tone="muted")
    if failures:
        support._die(
            f"remove did not complete for {failures} item"
            f"{'s' if failures != 1 else ''}; correct the errors and retry")


def cmd_stats(args, support):
    conn, meta = support.open_index(args)
    parsed = int(conn.execute(
        "SELECT COUNT(*) FROM files WHERE index_status='parsed'").fetchone()[0])
    parse_inputs = {
        "parsed": parsed,
        "skipped": int(meta.get("n_parse_skipped", 0)),
        "failed": int(meta.get("n_parse_failed", 0)),
        "oversized": int(meta.get("n_oversize", 0)),
    }
    if args.format == "json":
        extra = {row["kind"]: row["n"] for row in conn.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind")}
        sys.stdout.write(render.render_json({
            "meta": meta, "parse_inputs": parse_inputs,
            "symbols_by_kind": extra,
        }))
        return
    console = Console(args.color)
    console.heading(f"{support._linux(meta)} index")
    console.field("Built", meta.get("built_at", "?"))
    console.field("Source", meta.get("source", "?"))
    console.blank()
    console.heading("Contents")
    for label, key in (("Directories", "n_dirs"), ("Files", "n_files"),
                       ("Subsystems", "n_subsystems"), ("Symbols", "n_symbols")):
        console.field(label, f"{int(meta.get(key, 0)):,}")
    if int(meta.get("n_symlinks", 0)):
        console.field("Symlinks", f"{int(meta['n_symlinks']):,}")
    console.blank()
    console.heading("Parsing")
    console.field("Parsed C/H", f"{parse_inputs['parsed']:,}")
    console.field("Skipped", f"{parse_inputs['skipped']:,}",
                  tone="warning" if parse_inputs["skipped"] else None)
    console.field("Failed", f"{parse_inputs['failed']:,}",
                  tone="error" if parse_inputs["failed"] else None)
    console.field("Oversized", f"{parse_inputs['oversized']:,}",
                  tone="warning" if parse_inputs["oversized"] else None)
    if meta.get("has_calls") == "1":
        console.blank()
        console.heading("Call graph")
        console.field("Call records", f"{int(meta.get('n_calls', 0)):,}")
        console.field("Call sites", f"{int(meta.get('n_call_occurrences', 0)):,} occurrences")
        console.field("Resolved", f"{int(meta.get('n_calls_resolved', 0)):,} identities")
        for label, key in (("Ambiguous", "n_calls_ambiguous"),
                           ("Macro-only", "n_calls_macro"),
                           ("Indirect", "n_calls_indirect"),
                           ("Unresolved", "n_calls_unresolved")):
            console.field(label, f"{int(meta.get(key, 0)):,}")
    console.blank()
    console.heading("Symbols by kind")
    console.table(
        ("Kind", "Symbols"),
        [(row["kind"], f"{row['n']:,}") for row in conn.execute(
            "SELECT kind, COUNT(*) n FROM symbols GROUP BY kind ORDER BY n DESC")],
        align_right=(1,), tones={0: "accent"},
    )
    console.blank()
    console.heading("Largest top-level areas")
    areas = []
    for row in conn.execute(
        "SELECT d.name, COUNT(f.id) n FROM dirs d JOIN files f"
        " ON substr(f.path, 1, length(d.path) + 1) = d.path || '/'"
        " WHERE d.depth = 1"
        " GROUP BY d.id ORDER BY n DESC LIMIT 8"
    ):
        area = maintainers.TOP_LEVEL_AREAS.get(row["name"])
        areas.append((row["name"], f"{row['n']:,}", area[0] if area else ""))
    console.table(("Area", "Files", "Description"), areas,
                  align_right=(1,), tones={0: "accent"})


def cmd_check(args, support):
    """Run the full row-level integrity audit on an index."""
    conn, meta = support.open_index(args)
    try:
        db.validate_schema(conn, deep=True)
    except (db.SchemaError, sqlite3.DatabaseError) as exc:
        support._die(f"index integrity check failed: {exc}")
    payload = {
        "ok": True,
        "index": support.index_version(meta),
        "files": int(meta["n_files"]),
        "symbols": int(meta["n_symbols"]),
        "calls": int(meta["n_calls"]),
        "call_occurrences": int(meta["n_call_occurrences"]),
    }
    if args.format == "json":
        sys.stdout.write(render.render_json(payload))
        return
    console = Console(args.color)
    console.heading(f"{support._linux(meta)} index check passed", tone="success")
    console.text("The index is structurally and semantically consistent.")
    console.blank()
    console.heading("Checked")
    for label, key in (("Files", "files"), ("Symbols", "symbols"),
                       ("Call records", "calls"), ("Occurrences", "call_occurrences")):
        console.field(label, f"{payload[key]:,}")
