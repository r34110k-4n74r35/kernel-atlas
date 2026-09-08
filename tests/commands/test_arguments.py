"""CLI help, option validation, limits, and correction hints."""

from __future__ import annotations

import pytest

from kernel_atlas import __version__
from kernel_atlas.commands import cli


@pytest.mark.parametrize(
    "command", ["versions", "build", "indexes", "use", "remove"])
def test_lifecycle_help_does_not_advertise_index_selection(command, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--kernel" not in help_text
    assert "--db" not in help_text
    assert "--color" in help_text


def test_top_level_version_reports_the_installed_implementation(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"{cli.PROG} {__version__}"


@pytest.mark.parametrize("argv", [
    ["--db", "", "stats"],
    ["stats", "--db", "   "],
    ["--kernel", "", "stats"],
    ["stats", "--kernel", "\t"],
    ["build", ""],
    ["build", "--src", ""],
    ["build", "--output", "   "],
    ["build", "--kinds", "\t"],
])
def test_empty_selectors_and_build_values_are_rejected_by_argparse(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    assert "must not be empty" in capsys.readouterr().err


@pytest.mark.parametrize(("command", "phrases"), [
    ("build", ["keep a downloaded source archive", "progress bars", "final build summary"]),
    ("find", ["complete, case-sensitive name", "name prefix"]),
    ("subsystems", ["only names matching", "sort key", "max subsystems"]),
    ("subsystem", ["max directory rows", "does not limit the --files list"]),
    ("info", ["maximum ownership matches", "ambiguous target candidates"]),
    ("tree", ["maximum directory depth"]),
])
def test_high_use_command_help_explains_its_options(command, phrases, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([command, "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for phrase in phrases:
        assert phrase in help_text


def test_lifecycle_index_selection_after_subcommand_is_unrecognized(capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--db", "study.db"])
    assert "unrecognized arguments: --db" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "message"), [
    (["diff", "schedule", "--from", "before", "--to", "after"], "invalid choice: 'diff'"),
    (["check", "--source"], "unrecognized arguments: --source"),
    (["build", "--src", "linux", "--incremental"], "unrecognized arguments: --incremental"),
])
def test_removed_snapshot_and_reuse_features_are_rejected(argv, message, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.build_parser().parse_args(argv)
    assert stopped.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["build", "--src", "linux", "--force"],
    ["check"],
    ["show", "schedule", "--bare"],
    ["remove", "6.12", "--source"],
])
def test_existing_build_check_show_and_source_removal_options_remain_valid(argv):
    assert cli.build_parser().parse_args(argv).command == argv[0]


@pytest.mark.parametrize("argv", [
    ["--db", "one.db", "--kernel", "6.12", "stats"],
    ["--db", "one.db", "stats", "--kernel", "6.12"],
])
def test_index_selection_options_are_unambiguous(capsys, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "message"), [
    (["--kernel", "6.12", "indexes"], "does not apply"),
    (["indexes", "--db", "ignored.db"], "unrecognized arguments"),
    (["build", "--kernel", "6.12"], "unrecognized arguments"),
    (["--db", "ignored.db", "versions"], "does not apply"),
])
def test_index_selectors_are_rejected_where_they_have_no_effect(
        capsys, argv, message):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert message in capsys.readouterr().err


def test_negative_limit_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["siblings", "mm", "-n", "-1"])
    err = capsys.readouterr().err
    assert ">= 0" in err or "invalid" in err.lower()


@pytest.mark.parametrize("command", [
    ["ls", "fs", "-n"],
    ["find", "ext4", "-n"],
    ["tree", "fs", "-d"],
])
def test_sqlite_bound_counts_reject_unreasonably_large_values_cleanly(
        mini_index, capsys, command):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), *command,
            str(cli._MAX_CLI_COUNT + 1),
        ])
    error = capsys.readouterr().err
    assert f"<= {cli._MAX_CLI_COUNT}" in error
    assert "Traceback" not in error


def test_build_jobs_have_a_rational_upper_bound(capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--jobs", str(cli._MAX_JOBS + 1)])
    assert f"<= {cli._MAX_JOBS}" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "message"), [
    (",", "at least one"),
    ("function,function", "duplicate symbol kind"),
])
def test_build_rejects_empty_or_duplicate_kind_lists_before_fetching(
        value, message, capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "--kinds", value])
    assert message in capsys.readouterr().err


def test_huge_target_line_is_a_clean_error(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "info",
                  "fs/ext4/inode.c:" + "9" * 100])
    assert "too large" in capsys.readouterr().err


def test_find_rejects_size_sort_for_symbols(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "find", "ext4", "--sort", "size"])
    assert "use --sort lines" in capsys.readouterr().err


@pytest.mark.parametrize("command", [
    ["siblings", "ext4_bmap", "--sort", "size"],
    ["ls", "fs/ext4/inode.c", "--sort", "size"],
    ["calls", "fs/ext4/inode.c:ext4_bmap", "--sort", "size"],
])
def test_every_symbol_only_listing_rejects_size_sort(
        mini_index, capsys, command):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), *command])
    assert "use --sort lines" in capsys.readouterr().err


def test_mutually_exclusive_flags_are_rejected_by_parser(capsys):
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--exact", "--glob"])
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["find", "x", "--static-only", "--no-static"])
    assert "not allowed with argument" in capsys.readouterr().err


def test_linkage_filters_on_path_only_listings_are_rejected(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "mm", "--exported"])
    assert "only applies to symbols" in capsys.readouterr().err


def test_path_and_show_reject_target_inapplicable_flags(mini_index, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "path", "mm/page_alloc.c", "--line"])
    assert "only applies to symbols" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "show", "__alloc_pages", "--lines", "1"])
    assert "applies to files" in capsys.readouterr().err


def test_correction_hints_preserve_the_explicit_database_selector(
        mini_index, capsys):
    selected = str(mini_index.resolve())

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "ls", "ext4_bmap"])
    error = capsys.readouterr().err
    assert f"--db {selected}" in error
    assert "siblings fs/ext4/inode.c:" in error

    with pytest.raises(SystemExit):
        cli.main(["--db", str(mini_index), "show", "fs/ext4"])
    error = capsys.readouterr().err
    assert f"--db {selected}" in error
    assert " ls fs/ext4" in error


@pytest.mark.parametrize("line_range", ["0", "9" * 5000])
def test_show_rejects_invalid_numeric_line_ranges(
        mini_index, line_range, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "--db", str(mini_index), "show", "Makefile", "--lines", line_range,
        ])
    assert "--lines" in capsys.readouterr().err
