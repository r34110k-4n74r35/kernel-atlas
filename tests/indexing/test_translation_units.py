"""Translation-unit roots, included sources, and independent linked programs."""

from __future__ import annotations

import pytest

from kernel_atlas.storage import db
from kernel_atlas.indexing import indexer

from .helpers import _tree


def test_call_build_resolves_quoted_c_members_in_the_same_unit(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "static int load_firmware(void) { return 1; }\n")
    (tree / "aggregate.c").write_text("""\
#include "member.c"
int aggregate(void) { return load_firmware(); }
""")
    other = tree / "drivers" / "other"
    other.mkdir(parents=True)
    (other / "firmware.c").write_text(
        "int load_firmware(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT parent.path,member.path,i.line FROM source_includes i"
        " JOIN files parent ON parent.id=i.includer_id"
        " JOIN files member ON member.id=i.included_id").fetchone()
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='aggregate'").fetchone()
    conn.close()

    assert tuple(include) == ("aggregate.c", "member.c", 1)
    assert tuple(call) == ("included_source", "member.c")


@pytest.mark.parametrize("directive", [
    '#include /* shared implementation */ "member.c"\n',
    '#include \\\n"member.c"\n',
])
def test_source_include_with_comments_or_continuations(tmp_path, directive):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "static int member_helper(void) { return 1; }\n")
    (tree / "caller.c").write_text(
        directive + "int caller(void) { return member_helper(); }\n")
    out = tmp_path / "index.db"
    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_includes").fetchone()[0] == 1
        call = conn.execute(
            "SELECT resolution FROM calls WHERE callee='member_helper'"
        ).fetchone()
        assert call["resolution"] == "included_source"
    finally:
        conn.close()


def test_commented_source_include_does_not_invent_a_translation_unit(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "static int member_helper(void) { return 1; }\n")
    (tree / "caller.c").write_text("""\
/* This is an example, not a preprocessing directive:
#include "member.c"
 */
int caller(void) { return member_helper(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute("SELECT COUNT(*) FROM source_includes").fetchone()[0]
    resolution = conn.execute(
        "SELECT c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='member_helper'"
    ).fetchone()[0]
    conn.close()

    assert includes == 0
    assert resolution == "unresolved"


def test_global_in_quoted_member_is_visible_through_root_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text(
        "int member_global(void) { return 1; }\n")
    (tree / "aggregate.c").write_text('#include "member.c"\n')
    (tree / "outside.c").write_text(
        "int outside(void) { return member_global(); }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='outside' AND c.callee='member_global'"
    ).fetchone()
    conn.close()

    assert tuple(row) == ("unique_global", "member.c")


def test_call_build_resolves_tree_root_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    member = tree / "lib" / "vdso" / "getrandom.c"
    member.parent.mkdir(parents=True)
    member.write_text(
        "static int __cvdso_getrandom(void) { return 1; }\n")
    wrapper = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    wrapper.mkdir(parents=True)
    (wrapper / "vgetrandom.c").write_text("""\
#include "lib/vdso/getrandom.c"
int __vdso_getrandom(void) { return __cvdso_getrandom(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    include = conn.execute(
        "SELECT parent.path,member.path FROM source_includes i"
        " JOIN files parent ON parent.id=i.includer_id"
        " JOIN files member ON member.id=i.included_id").fetchone()
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='__vdso_getrandom'").fetchone()
    conn.close()

    assert tuple(include) == (
        "arch/x86/entry/vdso/vdso64/vgetrandom.c",
        "lib/vdso/getrandom.c",
    )
    assert tuple(call) == ("included_source", "lib/vdso/getrandom.c")


def test_multi_root_member_requires_every_translation_unit_to_agree(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "common.c").write_text(
        "static int common_call(void) { return helper(); }\n")
    (tree / "a.c").write_text("""\
static int helper(void) { return 1; }
#include "common.c"
""")
    (tree / "b.c").write_text('#include "common.c"\n')
    (tree / "global.c").write_text(
        "int helper(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='common_call' AND c.callee='helper'"
    ).fetchone()
    conn.close()

    # a.c binds its static helper; b.c binds the global one.  The source row
    # represents both instantiations and must not claim either identity.
    assert tuple(row) == (None, "ambiguous")


def test_kbuild_object_remains_a_root_when_also_included(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    with (tree / "Makefile").open("a") as makefile:
        makefile.write("obj-y += dual.o helper.o caller.o\n")
    (tree / "dual.c").write_text(
        "int exported_dual(void) { return helper(); }\n")
    (tree / "helper.c").write_text(
        "int helper(void) { return 1; }\n")
    (tree / "caller.c").write_text(
        "int outside(void) { return exported_dual(); }\n")
    wrapper = tree / "tools" / "testing"
    wrapper.mkdir(parents=True)
    (wrapper / "wrapper.c").write_text('#include "dual.c"\n')
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        (row["caller"], row["callee"]):
            (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT caller.name AS caller,c.callee,c.callee_id,c.resolution"
            " FROM calls c JOIN symbols caller ON caller.id=c.caller_id")
    }
    is_root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id WHERE f.path='dual.c'"
    ).fetchone()
    conn.close()

    assert is_root is not None
    # The call written in dual.c has different kernel/tools contexts.
    assert rows[("exported_dual", "helper")] == (None, "ambiguous")
    # Other kernel sources may still bind dual.c's standalone global identity.
    assert rows[("outside", "exported_dual")][1] == "unique_global"


def test_non_build_make_object_reference_does_not_create_a_root(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    with (tree / "Makefile").open("a") as makefile:
        makefile.write("clean-files += member.o\n")
    (tree / "member.c").write_text(
        "int image_only(void) { return 1; }\n")
    image = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    image.mkdir(parents=True)
    (image / "wrapper.c").write_text('#include "member.c"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "caller.c").write_text(
        "int kernel_call(void) { return image_only(); }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='kernel_call' AND c.callee='image_only'"
    ).fetchone()
    false_root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id WHERE f.path='member.c'"
    ).fetchone()
    conn.close()

    assert false_root is None
    assert tuple(row) == (None, "unresolved")


def test_tools_build_object_list_records_a_translation_unit_root(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    library = tree / "tools" / "lib" / "bpf"
    library.mkdir(parents=True)
    (library / "Build").write_text("libbpf-y += member.o\n")
    (library / "member.c").write_text(
        "int tools_member(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    root = conn.execute(
        "SELECT 1 FROM translation_unit_roots root JOIN files f"
        " ON f.id=root.file_id"
        " WHERE f.path='tools/lib/bpf/member.c'"
    ).fetchone()
    conn.close()

    assert root is not None


def test_literal_make_compile_and_link_rules_record_translation_unit_roots(
        tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    ring = tree / "tools" / "virtio" / "ringtest"
    ring.mkdir(parents=True)
    (ring / "Makefile").write_text("""\
dual.o: dual.c main.h
standalone: dual.o
poll.o: poll.c dual.c main.h
poll: poll.o
""")
    (ring / "main.h").write_text("\n")
    (ring / "dual.c").write_text(
        "int dual_source(void) { return 1; }\n")
    (ring / "poll.c").write_text('#include "dual.c"\n')
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    roots = {
        row[0] for row in conn.execute(
            "SELECT f.path FROM translation_unit_roots root"
            " JOIN files f ON f.id=root.file_id")
    }
    conn.close()

    assert "tools/virtio/ringtest/dual.c" in roots
    assert "tools/virtio/ringtest/poll.c" in roots


def test_included_member_uses_its_root_translation_unit_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    member = tree / "lib" / "vdso" / "member.c"
    member.parent.mkdir(parents=True)
    member.write_text(
        "static int member_call(void) { return kernel_only(); }\n")
    wrapper = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    wrapper.mkdir(parents=True)
    (wrapper / "wrapper.c").write_text(
        '#include "lib/vdso/member.c"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "helper.c").write_text(
        "int kernel_only(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='member_call' AND c.callee='kernel_only'"
    ).fetchone()
    domain = conn.execute(
        "SELECT call_domain FROM files"
        " WHERE path='arch/x86/entry/vdso/vdso64/wrapper.c'"
    ).fetchone()[0]
    conn.close()

    assert domain.startswith("image:")
    assert tuple(row) == (None, "unresolved")


def test_call_build_ignores_unparseable_quoted_c_members(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_bytes(b"\0binary input\n")
    (tree / "aggregate.c").write_text("""\
#include "member.c"
int aggregate(void) { return missing_member(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    includes = conn.execute("SELECT COUNT(*) FROM source_includes").fetchone()[0]
    call = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='aggregate' AND c.callee='missing_member'"
    ).fetchone()
    status = conn.execute(
        "SELECT index_status FROM files WHERE path='member.c'").fetchone()[0]
    conn.close()

    assert includes == 0
    assert tuple(call) == (None, "unresolved")
    assert status == "skipped_binary"


def test_call_domains_follow_independently_linked_kbuild_programs(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    sample = tree / "samples" / "bpf"
    sample.mkdir(parents=True)
    (sample / "Makefile").write_text("""\
tprogs-y := alpha beta
alpha-objs := alpha_main.o alpha_helper.o shared.o
beta-objs := beta_main.o shared.o
""")
    (sample / "alpha_main.c").write_text("""\
int alpha_helper(void);
int fprintf(void);
int alpha_entry(void) { return alpha_helper() + fprintf(); }
""")
    (sample / "alpha_helper.c").write_text(
        "int alpha_helper(void) { return 1; }\n")
    (sample / "beta_main.c").write_text(
        "int beta_entry(void) { return 2; }\n")
    (sample / "shared.c").write_text(
        "int shared_helper(void) { return 3; }\n")
    (sample / "trace_kern.c").write_text(
        "int bpf_program(void) { return 4; }\n")
    (sample / "kernel_piece.c").write_text(
        "int kernel_piece(void) { return 5; }\n")
    hid = tree / "samples" / "hid"
    hid.mkdir()
    (hid / "mouse.bpf.c").write_text(
        "int hid_bpf_program(void) { return 7; }\n")
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "print.c").write_text(
        "int fprintf(void) { return 6; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'samples/%'"))
    calls = {row["callee"]: (row["callee_id"], row["resolution"])
             for row in conn.execute(
                 "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
                 " JOIN symbols s ON s.id=c.caller_id"
                 " WHERE s.name='alpha_entry'")}
    conn.close()

    assert domains["samples/bpf/alpha_main.c"] == \
        "program:samples/bpf:alpha"
    assert domains["samples/bpf/alpha_helper.c"] == \
        "program:samples/bpf:alpha"
    assert domains["samples/bpf/shared.c"] == "isolated:samples/bpf/shared.c"
    assert domains["samples/bpf/trace_kern.c"] == \
        "isolated:samples/bpf/trace_kern.c"
    assert domains["samples/bpf/kernel_piece.c"] == \
        "isolated:samples/bpf/kernel_piece.c"
    assert domains["samples/hid/mouse.bpf.c"] == \
        "isolated:samples/hid/mouse.bpf.c"
    assert calls["alpha_helper"][1] == "unique_global"
    assert calls["alpha_helper"][0] is not None
    assert calls["fprintf"] == (None, "unresolved")


def test_kbuild_program_header_bindings_block_false_global_targets(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    program = tree / "scripts" / "host-tool"
    program.mkdir(parents=True)
    (program / "Makefile").write_text("""\
hostprogs := analyzer
analyzer-objs := main.o helpers.o
""")
    (program / "bindings.h").write_text("""\
static int header_helper(void) { return 1; }
#define header_macro() 2
static int (*header_pointer)(void);
""")
    (program / "main.c").write_text("""\
#include "bindings.h"
int analyze(void)
{
    return header_helper() + header_macro() + header_pointer();
}
""")
    (program / "helpers.c").write_text("""\
int header_helper(void) { return 10; }
int header_macro(void) { return 20; }
int header_pointer(void) { return 30; }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'scripts/host-tool/%'"))
    calls = {row["callee"]: (row["callee_id"], row["resolution"])
             for row in conn.execute(
                 "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
                 " JOIN symbols caller ON caller.id=c.caller_id"
                 " WHERE caller.name='analyze'")}
    conn.close()

    domain = "program:scripts/host-tool:analyzer"
    assert domains["scripts/host-tool/main.c"] == domain
    assert domains["scripts/host-tool/helpers.c"] == domain
    assert calls == {
        "header_helper": (None, "ambiguous"),
        "header_macro": (None, "ambiguous"),
        "header_pointer": (None, "ambiguous"),
    }


def test_boot_image_companions_share_a_domain_separate_from_vmlinux(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    boot = tree / "arch" / "x86" / "boot"
    boot.mkdir(parents=True)
    (boot / "main.c").write_text("""\
int detect_memory(void);
int main(void) { return detect_memory(); }
""")
    (boot / "memory.c").write_text(
        "int detect_memory(void) { return 1; }\n")
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "memory.c").write_text(
        "int detect_memory(void) { return 2; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    domains = dict(conn.execute(
        "SELECT path,call_domain FROM files WHERE path LIKE 'arch/x86/boot/%'"))
    call = conn.execute(
        "SELECT c.resolution,target_file.path FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " JOIN files target_file ON target_file.id=target.file_id"
        " WHERE caller.name='main' AND c.callee='detect_memory'").fetchone()
    conn.close()

    assert domains["arch/x86/boot/main.c"] == "image:arch:x86:boot"
    assert domains["arch/x86/boot/memory.c"] == "image:arch:x86:boot"
    assert tuple(call) == ("unique_global", "arch/x86/boot/memory.c")
