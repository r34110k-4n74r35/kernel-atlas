"""CLI entry point and shared command services.

Argument definitions and feature handlers live beside this module. Shared
services are passed explicitly from this entry point to each command handler.
"""

from __future__ import annotations

import argparse
import sys

from ..indexing import indexer as indexer
from ..presentation import terminal
from . import (
    aggregate as cli_aggregate,
    browse as cli_browse,
    build as cli_build,
    calls as cli_calls,
    lifecycle as cli_lifecycle,
    parser as cli_parser,
    resources as cli_resources,
    source as cli_source,
)
from .calls import _frames_from_text as _frames_from_text
from .parser import _MAX_CLI_COUNT as _MAX_CLI_COUNT, _MAX_JOBS as _MAX_JOBS
from .output import (
    PROG as PROG,
    _die as _die,
    _split_list as _split_list,
    _COLUMN_OUTPUT_FORMATS as _COLUMN_OUTPUT_FORMATS,
    pick_columns as pick_columns,
    _validate_listing_output as _validate_listing_output,
    _listing_has_columns as _listing_has_columns,
    emit as emit,
    _entry_is_target as _entry_is_target,
    kinds_from_args as kinds_from_args,
    symbol_filter_kinds as symbol_filter_kinds,
    _static_mode as _static_mode,
    _checked_grep as _checked_grep,
    _post_filter as _post_filter,
    _reject_symbol_size_sort as _reject_symbol_size_sort,
)
from .selection import (
    _version_key as _version_key,
    version_prefix_match as version_prefix_match,
    _index_version_key as _index_version_key,
    _same_path as _same_path,
    resolve_index_spec as resolve_index_spec,
    _default_version_pin as _default_version_pin,
    default_index as default_index,
    selected_index as selected_index,
    index_version as index_version,
    open_index as open_index,
    _OPEN_INDEXES as _OPEN_INDEXES,
    _close_indexes as _close_indexes,
    _linux as _linux,
    _unlink_index as _unlink_index,
    _managed_source_record as _managed_source_record,
    _managed_source_recorded_by as _managed_source_recorded_by,
)
from .source import (
    find_source_tree as find_source_tree,
    _TARGET_SUFFIX_RE as _TARGET_SUFFIX_RE,
    _normalize_target_spec as _normalize_target_spec,
    source_tree as source_tree,
    source_member as source_member,
    _path_inside as _path_inside,
    _MAX_SHOW as _MAX_SHOW,
)
from .targeting import (
    _SOURCE_SUFFIXES as _SOURCE_SUFFIXES,
    _suggestions as _suggestions,
    resolve_or_die as resolve_or_die,
    _resolve_area as _resolve_area,
    _target_spec as _target_spec,
    _command_prefix as _command_prefix,
    _call_graph_rebuild_hint as _call_graph_rebuild_hint,
    _call_graph_rebuild_advice as _call_graph_rebuild_advice,
    _require_exact_line_qualifier as _require_exact_line_qualifier,
    _require_unique_symbol_identity as _require_unique_symbol_identity,
    _links_for as _links_for,
    _subsystem_payload as _subsystem_payload,
    _relationship_subsystem as _relationship_subsystem,
)


_SLASH_COUNT = "(LENGTH(path) - LENGTH(REPLACE(path, '/', '')))"


def cmd_versions(args):
    return cli_lifecycle.cmd_versions(args, sys.modules[__name__])


def cmd_build(args):
    return cli_build.cmd_build(args, sys.modules[__name__])


def cmd_indexes(args):
    return cli_lifecycle.cmd_indexes(args, sys.modules[__name__])


def cmd_use(args):
    return cli_lifecycle.cmd_use(args, sys.modules[__name__])


def cmd_remove(args):
    return cli_lifecycle.cmd_remove(args, sys.modules[__name__])


def cmd_stats(args):
    return cli_lifecycle.cmd_stats(args, sys.modules[__name__])


def cmd_check(args):
    return cli_lifecycle.cmd_check(args, sys.modules[__name__])


def cmd_info(args):
    return cli_browse.cmd_info(args, sys.modules[__name__])


def cmd_struct(args):
    return cli_aggregate.cmd_struct(args, sys.modules[__name__])


def cmd_siblings(args):
    return cli_browse.cmd_siblings(args, sys.modules[__name__])


def cmd_ls(args):
    return cli_browse.cmd_ls(args, sys.modules[__name__])


def cmd_find(args):
    return cli_browse.cmd_find(args, sys.modules[__name__])


def cmd_subsystems(args):
    return cli_browse.cmd_subsystems(args, sys.modules[__name__])


def cmd_subsystem(args):
    return cli_browse.cmd_subsystem(args, sys.modules[__name__])


def cmd_tree(args):
    return cli_browse.cmd_tree(args, sys.modules[__name__])


def cmd_path(args):
    return cli_source.cmd_path(args, sys.modules[__name__])


def cmd_show(args):
    return cli_source.cmd_show(args, sys.modules[__name__])


def cmd_trace(args):
    return cli_calls.cmd_trace(args, sys.modules[__name__])


def cmd_calls(args):
    _validate_listing_output(args)
    return cli_calls.cmd_calls(args, sys.modules[__name__])


def cmd_relationships(args):
    return cli_calls.cmd_relationships(args, sys.modules[__name__])


def cmd_web(args):
    return cli_resources.cmd_web(args, sys.modules[__name__])


def cmd_docs(args):
    return cli_resources.cmd_docs(args, sys.modules[__name__])


def cmd_locate(args):
    return cli_resources.cmd_locate(args, sys.modules[__name__])


def build_parser() -> argparse.ArgumentParser:
    return cli_parser.build_parser(sys.modules[__name__])


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    with terminal.color_mode(args.color):
        return _run(args)


def _run(args) -> int:
    db_arg = getattr(args, "db", None)
    kernel_arg = getattr(args, "kernel", None)
    if db_arg and kernel_arg:
        _die("--db and --kernel are mutually exclusive")
    if args.command in {"versions", "build", "indexes", "use", "remove", "rm"}:
        selection = "--db" if db_arg else ("--kernel" if kernel_arg else None)
        if selection:
            _die(f"{selection} does not apply to {args.command!r}")
    try:
        args.func(args)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except (OSError, ValueError) as exc:
        _die(str(exc))
    except KeyboardInterrupt:
        console = terminal.Console(stream=sys.stderr)
        console.blank()
        console.text("interrupted", tone="warning", indent=0)
        return 130
    finally:
        _close_indexes()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
