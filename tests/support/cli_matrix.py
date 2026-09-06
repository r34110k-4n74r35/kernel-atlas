"""Registered CLI commands and representative arguments for format coverage."""

from kernel_atlas.commands import cli

QUERY_ARGS = {
    "stats": [],
    "check": [],
    "doctor": [],
    "info": ["fs/ext4"],
    "struct": ["ext4_sb_info"],
    "structure": ["ext4_sb_info"],
    "siblings": ["ext4_bmap"],
    "sib": ["ext4_bmap"],
    "ls": ["fs/ext4"],
    "find": ["ext4_*", "--glob"],
    "subsystems": [],
    "subsystem": ["EXT4 FILE SYSTEM", "--files"],
    "path": ["ext4_bmap", "--line"],
    "show": ["ext4_bmap", "--bare"],
    "tree": ["fs", "--files"],
    "web": ["ext4_bmap"],
    "docs": ["ext4_bmap", "--under", "filesystems", "--explain"],
    "locate": ["ext4_bmap"],
    "trace": [],
    "calls": ["ext4_bmap"],
    "relationships": ["EXT4 FILE SYSTEM", "--include-internal"],
    "rels": ["EXT4 FILE SYSTEM", "--include-internal"],
}
LIFECYCLE_COMMANDS = {"versions", "build", "indexes", "use", "remove", "rm"}
COMMANDS = next(
    action.choices for action in cli.build_parser()._actions
    if action.dest == "command"
)


def _formats(parser):
    return next(
        (action.choices for action in parser._actions if action.dest == "format"),
        [None],
    )
