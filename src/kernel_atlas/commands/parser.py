"""Argument definitions and validation for the command-line interface.

Handlers are supplied by the entry point so this module never imports it.
"""

from __future__ import annotations

import argparse
import re
import sys

from .. import __version__, cparse, query, render, terminal
from ..documentation_query import documentation_scope

_MAX_CLI_COUNT = 2**31 - 1
_MAX_JOBS = 256


class _Parser(argparse.ArgumentParser):
    """Keep help and usage errors consistent with the CLI's color preference."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Python 3.14 added its own color policy; apply ours after layout so
        # --color, NO_COLOR, and TERM behave alike on every supported Python.
        if hasattr(self, "color"):
            self.color = False

    @staticmethod
    def _color_choice(values):
        choice = None
        for i, value in enumerate(values):
            if value == "--":
                break
            candidate = (values[i + 1] if value == "--color" and i + 1 < len(values)
                         else value.removeprefix("--color=")
                         if value.startswith("--color=") else None)
            if candidate in {"auto", "always", "never"}:
                choice = candidate
        return choice

    def parse_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        choice = self._color_choice(values)
        if choice is None:
            return super().parse_args(values, namespace)
        with terminal.color_mode(choice):
            return super().parse_args(values, namespace)

    def parse_known_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        choice = self._color_choice(values)
        if choice is None:
            return super().parse_known_args(values, namespace)
        with terminal.color_mode(choice):
            return super().parse_known_args(values, namespace)

    def _print_message(self, message, file=None):
        if not message:
            return
        stream = sys.stderr if file is None else file
        color = terminal.color_enabled(stream)
        lines = []
        for raw in message.splitlines(keepends=True):
            end = "\n" if raw.endswith("\n") else ""
            line = terminal.clean(raw.removesuffix("\n"))
            if ": error:" in line:
                line = render.paint(line, "1;31", color)
            elif line and not line.startswith(" ") and line.endswith(":"):
                line = render.paint(line, "1;36", color)
            else:
                line = re.sub(r"(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*",
                              lambda m: render.paint(m[0], "36", color), line)
                if line.startswith("usage:"):
                    line = render.paint("usage:", "1;36", color) + line[6:]
            lines.append(line + end)
        super()._print_message("".join(lines), stream)


def _nonempty_arg(value: str) -> str:
    """Reject empty option values before truthiness can turn them into defaults."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _nonneg_int(value: str) -> int:
    try:
        i = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, not {value!r}")
    if i < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    if i > _MAX_CLI_COUNT:
        raise argparse.ArgumentTypeError(f"must be <= {_MAX_CLI_COUNT}")
    return i


def _positive_int(value: str) -> int:
    i = _nonneg_int(value)
    if i < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return i


def _jobs_int(value: str) -> int:
    i = _positive_int(value)
    if i > _MAX_JOBS:
        raise argparse.ArgumentTypeError(f"must be <= {_MAX_JOBS}")
    return i


def _add_output_opts(p, sorts=True, limit_default=0):
    g = p.add_argument_group("output")
    g.add_argument("--format", "-f", default="table",
                   choices=("table", "plain", "names", "json", "csv", "tree"),
                   help="output format (default: table)")
    g.add_argument("--columns", "-c",
                   help="comma-separated columns: " + ",".join(render.COLUMNS))
    g.add_argument("--limit", "-n", type=_nonneg_int, default=limit_default,
                   help="max rows (0 = all)" if limit_default == 0 else
                        f"max rows (default: {limit_default}; 0 = all)")
    g.add_argument("--grep", "-g", help="only names matching this regex")
    if sorts:
        g.add_argument("--sort", default="name",
                       choices=("name", "path", "kind", "line", "size", "lines"),
                       help="sort key; size applies to path rows, while lines "
                            "is the definition span for symbols")
    g.add_argument("--with-subsystem", "-S", action="store_true",
                   help="add a subsystem column")


def _add_filter_opts(p, *, kinds_help: str | None = None):
    g = p.add_argument_group("filters")
    g.add_argument("--kinds", "-k",
                   help=kinds_help or
                   "what to list: dir,file,function,syscall,struct,union,enum,"
                   "typedef,macro,variable,prototype — or all/symbols/paths/"
                   "functions/types")
    g.add_argument("--exported", action="store_true",
                   help="only EXPORT_SYMBOL'd symbols")
    linkage = g.add_mutually_exclusive_group()
    linkage.add_argument("--static-only", action="store_true",
                         help="only static symbols")
    linkage.add_argument("--no-static", action="store_true",
                         help="hide static symbols")


def _global_opts(parser, suppress: bool):
    """Accept --kernel/--db/--color before *or* after the subcommand.

    Subcommand copies use SUPPRESS so they only override when actually given,
    instead of clobbering the top-level value with their own default.
    """
    kw = {"default": argparse.SUPPRESS} if suppress else {}
    g = parser.add_argument_group("index selection")
    g.add_argument("--kernel", "-K", type=_nonempty_arg,
                   help="which built index to use (e.g. 6.12.104)", **kw)
    g.add_argument("--db", type=_nonempty_arg,
                   help="path to an index file inside the project", **kw)
    g.add_argument("--color", choices=("auto", "always", "never"),
                   **(kw or {"default": "auto"}))


def build_parser(support) -> argparse.ArgumentParser:
    p = _Parser(
        prog=support.PROG,
        description="Index a Linux kernel tree and explore its structure, "
                    "symbols and subsystems.",
        epilog=f"Start with:  {support.PROG} build lts     then:  {support.PROG} info mm",
    )
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    _global_opts(p, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _global_opts(common, suppress=True)
    lifecycle_common = argparse.ArgumentParser(add_help=False)
    lifecycle_display = lifecycle_common.add_argument_group("display")
    lifecycle_display.add_argument(
        "--color", choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
    )

    subs = p.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return subs.add_parser(name, parents=[common], **kwargs)

    def add_lifecycle(name, **kwargs):
        # Lifecycle commands do not select an index to query.  Keep --color
        # usable after the subcommand without advertising meaningless -K/--db
        # options in their help.
        return subs.add_parser(name, parents=[lifecycle_common], **kwargs)

    sp = add_lifecycle(
        "versions", help="list kernel versions available on kernel.org")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_versions)

    sp = add_lifecycle("build", help="download a kernel and build its index")
    # No hardcoded default: with --src the version comes from the tree's own
    # Makefile, otherwise the alias 'lts' is applied in cmd_build.
    sp.add_argument("version", nargs="?", default=None, type=_nonempty_arg,
                    help="version or alias: lts (default), stable, mainline, 6.12.104")
    sp.add_argument("--src", type=_nonempty_arg,
                    help="index an existing local kernel tree instead")
    sp.add_argument("--output", "-o", type=_nonempty_arg,
                    help="write the index here (inside the project)")
    sp.add_argument("--jobs", "-j", type=_jobs_int, help="parallel parser processes")
    sp.add_argument("--kinds", type=_nonempty_arg,
                    help="symbol kinds to index (default: "
                         + ",".join(cparse.DEFAULT_KINDS) + ")")
    sp.add_argument("--with-calls", action="store_true",
                    help="also record a call graph (bigger index, enables 'calls')")
    sp.add_argument("--keep-tarball", action="store_true",
                    help="keep a downloaded source archive after extraction")
    sp.add_argument("--no-verify", action="store_true",
                    help="skip the sha256 check against kernel.org")
    sp.add_argument("--force", action="store_true", help="rebuild if it already exists")
    sp.add_argument("--quiet", "-q", action="store_true",
                    help="suppress phase progress bars and counters; "
                         "keep the final build summary")
    sp.set_defaults(func=support.cmd_build)

    sp = add_lifecycle("indexes", help="list indexes you have built")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_indexes)

    sp = add_lifecycle(
        "use", help="pin which kernel version commands use by default")
    sp.add_argument("version", nargs="?",
                    help="version or unique prefix; omit to show the current one")
    sp.add_argument("--clear", action="store_true",
                    help="unpin; go back to the highest built version")
    sp.set_defaults(func=support.cmd_use)

    sp = add_lifecycle(
        "remove", aliases=["rm"], help="delete built indexes")
    sp.add_argument("versions", nargs="+", metavar="VERSION",
                    help="one or more versions (or unique prefixes) to delete")
    sp.add_argument("--source", action="store_true",
                    help="also delete the kernel source tree under kernels/")
    sp.set_defaults(func=support.cmd_remove)

    sp = add("stats", help="overview of an index")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_stats)

    sp = add("check", aliases=["doctor"],
             help="deep-check index counts and call identities")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_check)

    sp = add("info", help="explain one folder, file or symbol")
    sp.add_argument("target", help="mm | mm/page_alloc.c | tcp_sendmsg | "
                                   "tcp.c:tcp_sendmsg | mm/page_alloc.c:5268")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.add_argument("--max-subsystems", type=_nonneg_int, default=3,
                    help="maximum ownership matches to show (default: 3)")
    sp.add_argument("--max-candidates", type=_nonneg_int, default=10,
                    help="maximum ambiguous target candidates (default: 10)")
    sp.set_defaults(func=support.cmd_info)

    sp = add("struct", aliases=["structure"],
             help="explain a C struct/union and all indexed members")
    sp.add_argument("target", help="usb_device | struct usb_device | "
                                   "union perf_mem_data_src | "
                                   "include/linux/usb.h:usb_device | "
                                   "include/linux/usb.h:661")
    sp.add_argument("--all", action="store_true",
                    help="show every matching definition instead of requiring one")
    sp.add_argument("--max-docs", type=_nonneg_int, default=5,
                    help="maximum related Documentation/ files (0 disables)")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_struct)

    sp = add("siblings", aliases=["sib"],
                        help="what sits at the same level as this?")
    sp.add_argument("target")
    sp.add_argument("--level", "-l", default="auto", choices=query.LEVELS,
                    help="how wide to look (default: auto — the containing "
                         "directory, or the containing file for a symbol)")
    sp.add_argument("--include-self", action="store_true")
    _add_filter_opts(sp)
    _add_output_opts(sp)
    sp.set_defaults(func=support.cmd_siblings)

    sp = add("ls", help="list what is inside a folder or file")
    sp.add_argument("target", nargs="?", default="")
    _add_filter_opts(sp)
    _add_output_opts(sp)
    sp.set_defaults(func=support.cmd_ls)

    sp = add("find", help="search for a symbol by name")
    sp.add_argument("pattern")
    match = sp.add_mutually_exclusive_group()
    match.add_argument("--exact", action="store_true",
                       help="match the complete, case-sensitive name")
    match.add_argument("--glob", action="store_true",
                       help="pattern is a glob (tcp_*)")
    match.add_argument("--prefix", action="store_true",
                       help="match a case-insensitive name prefix")
    _add_filter_opts(
        sp,
        kinds_help="symbol kinds to search (path kinds are not accepted)",
    )
    _add_output_opts(sp, limit_default=50)
    sp.set_defaults(func=support.cmd_find)

    sp = add("subsystems", help="list subsystems from MAINTAINERS")
    sp.add_argument("--grep", "-g", help="only names matching this regex")
    sp.add_argument("--sort", default="size",
                    choices=("size", "claimed", "primary", "name"),
                    help="sort key (default: size)")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=0,
                    help="max subsystems (default: all)")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_subsystems)

    sp = add("subsystem", help="detail for one subsystem")
    sp.add_argument("name")
    sp.add_argument("--files", action="store_true", help="also list every file")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=15,
                    help="max directory rows (default: 15; 0 = all); does not "
                         "limit the --files list")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_subsystem)

    sp = add("path", help="print the on-disk path of a folder, file or symbol")
    sp.add_argument("target")
    sp.add_argument("--line", action="store_true",
                    help="append :LINE for symbols")
    sp.set_defaults(func=support.cmd_path)

    sp = add("show", help="print the source of a symbol or file")
    sp.add_argument("target")
    show_range = sp.add_mutually_exclusive_group()
    show_range.add_argument("--context", "-C", type=_nonneg_int, default=0,
                            help="extra lines around a symbol")
    show_range.add_argument("--lines", "-L",
                            help="line range for a file, e.g. 100:140")
    sp.add_argument("--bare", action="store_true",
                    help="no header and no line numbers")
    sp.set_defaults(func=support.cmd_show)

    sp = add("tree", help="draw the directory tree")
    sp.add_argument("target", nargs="?", default="")
    sp.add_argument("--depth", "-d", type=_nonneg_int, default=2,
                    help="maximum directory depth (default: 2; 0 = target only)")
    sp.add_argument("--files", action="store_true", help="include files")
    sp.add_argument("--format", "-f", default="tree", choices=("tree", "json"))
    sp.set_defaults(func=support.cmd_tree)

    sp = add("web", help="print Elixir / git.kernel.org / GitHub / docs URLs")
    sp.add_argument("target")
    sp.add_argument("--url", choices=("elixir", "ident", "git", "github", "docs"),
                    help='print just this URL, for `open "$(ka web … '
                         '--url elixir)"`')
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_web)

    sp = add("docs", help="Documentation/ files related to a target")
    sp.add_argument("target")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=30,
                    help="maximum matching documents (default: 30; 0 = all)")
    sp.add_argument("--under", type=documentation_scope,
                    help="restrict to a Documentation path before ranking, "
                         "e.g. driver-api or Documentation/usb")
    sp.add_argument("--explain", action="store_true",
                    help="include the evidence used to rank each document")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_docs)

    sp = add("locate",
             help="resolve a target in every built index (compare versions)")
    sp.add_argument("target")
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_locate)

    sp = add("trace",
                        help="annotate a backtrace: which subsystem is each frame in?")
    sp.add_argument("frames", nargs="*",
                    help="frame names, or pipe an oops/ftrace log on stdin")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=100)
    sp.add_argument("--format", "-f", default="table", choices=("table", "json"))
    sp.set_defaults(func=support.cmd_trace)

    sp = add("calls", help="call graph (needs an index built --with-calls)")
    sp.add_argument("target")
    sp.add_argument("--callers", action="store_true",
                    help="show callers instead of callees")
    _add_filter_opts(
        sp,
        kinds_help="resolved result identities to keep: function,syscall",
    )
    _add_output_opts(sp, limit_default=200)
    sp.set_defaults(func=support.cmd_calls)

    sp = add(
        "relationships", aliases=["rels"],
        help="ownership overlap and call flow between subsystems")
    sp.add_argument(
        "target",
        help="a subsystem name or any folder, file, or symbol in that subsystem")
    sp.add_argument("--via", choices=("all", "ownership", "calls"), default="all",
                    help="relationship evidence to show (default: all)")
    sp.add_argument("--direction", choices=("both", "outgoing", "incoming"),
                    default="both", help="call-flow direction (default: both)")
    sp.add_argument("--include-internal", action="store_true",
                    help="include calls which stay inside the selected subsystem")
    sp.add_argument("--min-shared", type=_positive_int, default=1,
                    help="minimum shared files for an ownership row")
    sp.add_argument("--min-calls", type=_positive_int, default=1,
                    help="minimum resolved edges for a call-flow row")
    sp.add_argument("--limit", "-n", type=_nonneg_int, default=20,
                    help="max rows per ownership/direction group "
                         "(default: 20; 0 = all)")
    sp.add_argument("--format", "-f", default="table",
                    choices=("table", "json", "csv"))
    sp.set_defaults(func=support.cmd_relationships)

    return p
