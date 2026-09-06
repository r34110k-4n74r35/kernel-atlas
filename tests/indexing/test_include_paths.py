"""Kbuild include-directory order, generated paths, and source boundaries."""

from __future__ import annotations

import pytest

from kernel_atlas.storage import db
from kernel_atlas.indexing import indexer

from .helpers import _tree


def test_kbuild_include_paths_resolve_angle_and_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    shared = tree / "shared"
    shared.mkdir()
    (shared / "angle_member.c").write_text(
        "static int angle_helper(void) { return 1; }\n")
    (shared / "quote_member.c").write_text(
        "static int quote_helper(void) { return 1; }\n")
    probes = tree / "tools" / "probes"
    probes.mkdir(parents=True)
    (probes / "Makefile").write_text("""\
hostprogs := angle quote
HOSTCFLAGS_angle.o := -I$(srctree)/shared/
HOSTCFLAGS_quote.o := -I $(srctree)/shared/
""")
    (probes / "angle.c").write_text("""\
#include <angle_member.c>
int angle(void) { return angle_helper(); }
""")
    (probes / "quote.c").write_text("""\
#include "quote_member.c"
int quote(void) { return quote_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = {
        (row["parent"], row["member"])
        for row in conn.execute(
            "SELECT parent.path AS parent,member.path AS member"
            " FROM source_includes edge"
            " JOIN files parent ON parent.id=edge.includer_id"
            " JOIN files member ON member.id=edge.included_id")
    }
    resolutions = {
        row["caller"]: row["resolution"]
        for row in conn.execute(
            "SELECT caller.name AS caller,c.resolution FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id")
    }
    conn.close()

    assert includes == {
        ("tools/probes/angle.c", "shared/angle_member.c"),
        ("tools/probes/quote.c", "shared/quote_member.c"),
    }
    assert resolutions["angle"] == "included_source"
    assert resolutions["quote"] == "included_source"


def test_target_include_order_distinguishes_srctree_from_objtree(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    tools_lib = tree / "tools" / "arch" / "x86" / "lib"
    arch_lib = tree / "arch" / "x86" / "lib"
    probe_dir = tree / "arch" / "x86" / "tools"
    tools_lib.mkdir(parents=True)
    arch_lib.mkdir(parents=True)
    probe_dir.mkdir(parents=True)
    (tools_lib / "insn.c").write_text(
        "static int decode_insn(void) { return 1; }\n")
    (arch_lib / "insn.c").write_text(
        "static int decode_insn(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
HOSTCFLAGS_probe.o := -I$(srctree)/tools/arch/x86/lib/ \\
                      -I$(objtree)/arch/x86/lib/
""")
    (probe_dir / "probe.c").write_text("""\
#include <insn.c>
int probe(void) { return decode_insn(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='arch/x86/tools/probe.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='probe' AND c.callee='decode_insn'"
    ).fetchone()
    conn.close()

    assert include[0] == "tools/arch/x86/lib/insn.c"
    assert tuple(resolution) == (
        "included_source", "tools/arch/x86/lib/insn.c")


def test_general_include_directory_precedes_target_specific_directory(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    general = tree / "general"
    specific = tree / "specific"
    probe_dir = tree / "tools" / "probe"
    general.mkdir()
    specific.mkdir()
    probe_dir.mkdir(parents=True)
    (general / "member.c").write_text(
        "static int selected_helper(void) { return 1; }\n")
    (specific / "member.c").write_text(
        "static int selected_helper(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
ccflags-y := -I$(srctree)/general
CFLAGS_probe.o := -I$(srctree)/specific
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='tools/probe/probe.c'"
    ).fetchone()
    conn.close()

    assert include[0] == "general/member.c"


def test_c_include_flag_categories_follow_makefile_lib_order(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    probe_dir = tree / "drivers" / "probe"
    probe_dir.mkdir(parents=True)
    for dirname in ("cpp", "cflags", "subdir", "local", "target", "linker"):
        include_dir = tree / dirname
        include_dir.mkdir()
        (include_dir / "member.c").write_text(
            f"static int selected_helper(void) {{ return {len(dirname)}; }}\n")
    # Deliberately list the variables in reverse semantic order. LDFLAGS is not
    # part of C compilation at all; the remaining order comes from Makefile.lib.
    (probe_dir / "Makefile").write_text("""\
LDFLAGS_probe.o := -I$(srctree)/linker
CFLAGS_probe.o := -I$(srctree)/target
ccflags-y := -I$(srctree)/local
subdir-ccflags-y := -I$(srctree)/subdir
KBUILD_CFLAGS := -I$(srctree)/cflags
KBUILD_CPPFLAGS := -I$(srctree)/cpp
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT member.path FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " JOIN files member ON member.id=edge.included_id"
        " WHERE parent.path='drivers/probe/probe.c'"
    ).fetchone()
    conn.close()

    assert include[0] == "cpp/member.c"


def test_opaque_include_directory_blocks_a_later_source_guess(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    specific = tree / "specific"
    probe_dir = tree / "tools" / "probe"
    specific.mkdir()
    probe_dir.mkdir(parents=True)
    (specific / "member.c").write_text(
        "static int selected_helper(void) { return 2; }\n")
    (probe_dir / "Makefile").write_text("""\
hostprogs := probe
CFLAGS_probe.o := -I$(objtree)/generated -I$(srctree)/specific
""")
    (probe_dir / "probe.c").write_text("""\
#include <member.c>
int probe(void) { return selected_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT 1 FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " WHERE parent.path='tools/probe/probe.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='probe' AND c.callee='selected_helper'"
    ).fetchone()[0]
    conn.close()

    assert include is None
    assert resolution == "unresolved"


@pytest.mark.parametrize("obj", ["$(obj)", "${obj}"])
def test_obj_include_paths_do_not_alias_generated_output_to_sources(
        tmp_path, obj):
    tree = _tree(tmp_path / "linux-9.9")
    probe_dir = tree / "drivers" / "probe"
    generated = probe_dir / "generated"
    generated.mkdir(parents=True)
    (generated / "member.c").write_text(
        "static int generated_helper(void) { return 1; }\n")
    (probe_dir / "Makefile").write_text(
        f"ccflags-y := -I{obj}/generated\n")
    (probe_dir / "caller.c").write_text("""\
#include <member.c>
int caller(void) { return generated_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT 1 FROM source_includes edge"
        " JOIN files parent ON parent.id=edge.includer_id"
        " WHERE parent.path='drivers/probe/caller.c'"
    ).fetchone()
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='generated_helper'"
    ).fetchone()[0]
    conn.close()

    assert include is None
    assert resolution == "unresolved"


def test_directory_local_include_flags_do_not_leak_into_children(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    shared = tree / "shared"
    shared.mkdir()
    (shared / "member.c").write_text(
        "static int member_helper(void) { return 1; }\n")
    parent = tree / "tools" / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "Makefile").write_text(
        "ccflags-y := -I$(srctree)/shared\n")
    (child / "caller.c").write_text("""\
#include <member.c>
int caller(void) { return member_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute(
        "SELECT COUNT(*) FROM source_includes").fetchone()[0]
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='member_helper'"
    ).fetchone()[0]
    conn.close()

    assert includes == 0
    assert resolution == "unresolved"
