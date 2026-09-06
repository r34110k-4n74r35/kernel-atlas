"""Conservative call identity resolution, blockers, and occurrence evidence."""

from __future__ import annotations

import pytest

from kernel_atlas import db, indexer, query

from .helpers import _tree


def test_call_resolution_is_identity_aware_and_conservative(tmp_path):
    conn = db.create(tmp_path / "calls.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0), (2, "one", 1, "one", 1),
         (3, "two", 1, "two", 1), (4, "three", 1, "three", 1)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext) VALUES (?,?,?,?,?)",
        [(1, "one/a.c", 2, "a.c", ".c"),
         (2, "two/b.c", 3, "b.c", ".c"),
         (3, "three/c.c", 4, "c.c", ".c"),
         (4, "blockers.h", 1, "blockers.h", ".h")],
    )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,is_static) VALUES (?,?,?,?,?)",
        [
            (1, 1, "caller", "function", 0),
            (2, 1, "helper", "function", 1),
            (3, 2, "helper", "function", 1),
            (4, 2, "unique", "function", 0),
            (5, 2, "duplicate", "function", 0),
            (6, 3, "duplicate", "function", 0),
            (7, 1, "local_duplicate", "function", 1),
            (8, 1, "local_duplicate", "function", 1),
            (9, 4, "macro_only", "macro", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(1, name) for name in (
            "helper", "unique", "duplicate", "local_duplicate",
            "macro_only", "missing")],
    )
    db.finalize(conn)

    counts = indexer._resolve_calls(conn)
    rows = {
        row["callee"]: (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT callee,callee_id,resolution FROM calls ORDER BY callee")
    }
    outbound = query.callee_entries(conn, 1)
    local_callers = query.callers(conn, 2)
    unrelated_static_callers = query.callers(conn, 3)
    conn.close()

    assert rows == {
        "helper": (2, "same_file"),
        "unique": (4, "unique_global"),
        "duplicate": (None, "ambiguous"),
        "local_duplicate": (None, "ambiguous"),
        "macro_only": (None, "macro"),
        "missing": (None, "unresolved"),
    }
    assert counts == {
        "same_file": 1, "included_source": 0, "unique_global": 1,
        "ambiguous": 2, "macro": 1, "indirect": 0, "unresolved": 1,
    }
    assert next(row for row in outbound if row.name == "helper").ref_id == 2
    assert [row.name for row in local_callers] == ["caller"]
    assert unrelated_static_callers == []


def test_call_resolution_respects_domains_and_identity_blockers(tmp_path):
    conn = db.create(tmp_path / "call-domains.db")
    conn.executemany(
        "INSERT INTO dirs(id,path,parent_id,name,depth) VALUES (?,?,?,?,?)",
        [(1, "", None, "linux", 0)],
    )
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext) VALUES (?,?,?,?,?)",
        [
            (1, "block/a.c", 1, "a.c", ".c"),
            (2, "tools/testing/helper.c", 1, "helper.c", ".c"),
            (3, "include/linux/api.h", 1, "api.h", ".h"),
            (4, "arch/arm/kernel/a.c", 1, "a.c", ".c"),
            (5, "arch/alpha/kernel/b.c", 1, "b.c", ".c"),
            (6, "kernel/helper.c", 1, "helper.c", ".c"),
            (7, "tools/accounting/main.c", 1, "main.c", ".c"),
            (8, "tools/testing/selftests/arm64/signal.c", 1, "signal.c", ".c"),
            (9, "tools/accounting/helper.c", 1, "helper.c", ".c"),
            (10, "tools/testing/selftests/kvm/main.c", 1, "main.c", ".c"),
            (11, "drivers/misc/blockers.c", 1, "blockers.c", ".c"),
            (12, "arch/x86/lib/checksum.c", 1, "checksum.c", ".c"),
            (13, "arch/x86/tools/relocs.c", 1, "relocs.c", ".c"),
        ],
    )
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,is_static) VALUES (?,?,?,?,?)",
        [
            (1, 1, "kernel_caller", "function", 0),
            (2, 4, "arm_caller", "function", 0),
            (3, 2, "spin_lock", "function", 0),
            (4, 3, "spin_lock", "macro", 0),
            (5, 2, "tool_only", "function", 0),
            (6, 5, "alpha_only", "function", 0),
            (7, 6, "generic_ok", "function", 0),
            (8, 3, "inline_only", "function", 1),
            (9, 3, "callback", "variable", 1),
            (10, 6, "conflicted", "function", 0),
            (11, 3, "conflicted", "macro", 0),
            (12, 7, "accounting_caller", "function", 0),
            (13, 8, "sigaddset", "function", 0),
            (14, 9, "accounting_ok", "function", 0),
            (15, 10, "kvm_caller", "function", 0),
            (16, 7, "accounting_local", "function", 0),
            (17, 6, "device_add", "function", 0),
            (18, 6, "device_remove", "function", 0),
            (19, 11, "device_add", "macro", 0),
            (20, 11, "device_remove", "variable", 1),
            (21, 6, "csum_partial", "function", 0),
            (22, 12, "csum_partial", "function", 0),
            (23, 13, "relocs_caller", "function", 0),
            (24, 6, "fprintf", "function", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO calls(caller_id,callee) VALUES (?,?)",
        [(1, name) for name in (
            "spin_lock", "tool_only", "inline_only", "callback", "conflicted",
            "device_add", "device_remove", "csum_partial")]
        + [(2, "alpha_only"), (2, "generic_ok")]
        + [(12, "sigaddset"), (12, "accounting_ok"),
           (12, "accounting_local"), (15, "sigaddset"), (23, "fprintf")],
    )
    db.finalize(conn)

    indexer._resolve_calls(conn)
    rows = {
        (row["caller_id"], row["callee"]):
            (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT caller_id,callee,callee_id,resolution FROM calls")
    }
    conn.close()

    assert rows[(1, "spin_lock")] == (None, "macro")
    assert rows[(1, "tool_only")] == (None, "unresolved")
    assert rows[(1, "inline_only")] == (None, "ambiguous")
    assert rows[(1, "callback")] == (None, "ambiguous")
    assert rows[(1, "conflicted")] == (None, "ambiguous")
    assert rows[(1, "device_add")] == (17, "unique_global")
    assert rows[(1, "device_remove")] == (18, "unique_global")
    assert rows[(1, "csum_partial")] == (None, "ambiguous")
    assert rows[(2, "alpha_only")] == (None, "unresolved")
    assert rows[(2, "generic_ok")] == (7, "unique_global")
    assert rows[(12, "sigaddset")] == (None, "unresolved")
    assert rows[(12, "accounting_ok")] == (None, "unresolved")
    assert rows[(12, "accounting_local")] == (16, "same_file")
    assert rows[(15, "sigaddset")] == (None, "unresolved")
    assert rows[(23, "fprintf")] == (None, "unresolved")


def test_call_build_requires_identity_blocker_kinds(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    out = tmp_path / "index.db"

    with pytest.raises(ValueError, match="requires indexing: macro, variable"):
        indexer.build(
            tree, out, "9.9", kinds=("function",), want_calls=True,
            jobs=1, quiet=True,
        )


def test_call_build_retains_local_function_pointer_calls_as_indirect(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "callbacks.c").write_text("""
int callback(void) { return 1; }
int caller(int (*callback)(void)) { return callback(); }
""")
    out = tmp_path / "index.db"

    stats = indexer.build(
        tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee,c.callee_id,c.resolution FROM calls c "
        "JOIN symbols s ON s.id=c.caller_id WHERE s.name='caller'"
    ).fetchone()
    meta = db.validate_schema(conn)
    conn.close()

    assert tuple(row) == ("callback", None, "indirect")
    assert stats.calls_indirect == 1
    assert meta["n_calls_indirect"] == "1"


def test_file_scope_function_pointer_blocks_same_named_global(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "fp.c").write_text("""\
static int (*callback)(void);
int fp_caller(void) { return callback(); }
""")
    (tree / "other.c").write_text(
        "int callback(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='fp_caller' AND c.callee='callback'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "indirect")


def test_same_file_macro_blocks_same_named_global(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "macro.c").write_text("""\
#define callback() 1
int macro_caller(void) { return callback(); }
""")
    (tree / "other.c").write_text(
        "int callback(void) { return 7; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='macro_caller' AND c.callee='callback'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "macro")


def test_call_resolution_respects_same_file_macro_source_intervals(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "nfs.c").write_text("""\
static int rpc_call_sync(void) { return 1; }
static int before_define(void) { return rpc_call_sync(); }
#define rpc_call_sync() 2
static int while_defined(void) { return rpc_call_sync(); }
#undef rpc_call_sync
static int after_undef(void) { return rpc_call_sync(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["caller"]: (
            row["resolution"], row["direct_count"], row["macro_count"])
        for row in conn.execute(
            "SELECT caller.name AS caller,c.resolution,c.direct_count,"
            " c.macro_count FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE c.callee='rpc_call_sync'")
    }
    db.validate_schema(conn, deep=True)
    conn.close()

    assert rows == {
        "before_define": ("same_file", 1, 0),
        "while_defined": ("macro", 0, 1),
        "after_undef": ("same_file", 1, 0),
    }


@pytest.mark.parametrize("suffix", [".h", ".h_shipped"])
def test_future_macro_in_same_header_does_not_block_earlier_call(
        tmp_path, suffix):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / f"api{suffix}").write_text("""\
static int target(void) { return 1; }
static int caller(void) { return target(); }
#define target() 2
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.resolution,c.direct_count,c.macro_count FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='target'"
    ).fetchone()
    conn.close()

    assert tuple(row) == ("same_file", 1, 0)


def test_macro_from_included_c_source_remains_a_conservative_blocker(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "member.c").write_text("#define wrapped_call() 1\n")
    (tree / "wrapper.c").write_text("""\
#include "member.c"
int caller(void) { return wrapped_call(); }
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='caller' AND c.callee='wrapped_call'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "macro")


def test_call_edges_persist_mixed_and_expression_level_indirect_evidence(
        tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "calls.c").write_text("""\
static void target(void) { }
static void caller(struct ops *ops)
{
    target();
    {
        void (*target)(void) = 0;
        target();
    }
    ops->target();
    (*ops->finish)();
}
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["callee"]: (
            row["resolution"], row["direct_count"], row["indirect_count"])
        for row in conn.execute(
            "SELECT c.callee,c.resolution,c.direct_count,c.indirect_count"
            " FROM calls c JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE caller.name='caller'")
    }
    db.validate_schema(conn, deep=True)
    conn.close()

    assert rows == {
        "target": ("same_file", 1, 1),
        "ops->target": ("indirect", 0, 1),
        "*ops->finish": ("indirect", 0, 1),
    }


def test_header_call_sites_do_not_guess_a_linked_image_domain(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    header = tree / "include" / "linux" / "shared.h"
    header.parent.mkdir(parents=True)
    header.write_text("""\
static inline int header_local(void) { return 1; }
static inline int header_call(void)
{
    return header_local() + kernel_only();
}
""")
    image = tree / "arch" / "x86" / "entry" / "vdso" / "vdso64"
    image.mkdir(parents=True)
    (image / "wrapper.c").write_text(
        '#include "include/linux/shared.h"\n')
    kernel = tree / "kernel"
    kernel.mkdir()
    (kernel / "helper.c").write_text(
        "int kernel_only(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    rows = {
        row["callee"]: (row["callee_id"], row["resolution"])
        for row in conn.execute(
            "SELECT c.callee,c.callee_id,c.resolution FROM calls c"
            " JOIN symbols caller ON caller.id=c.caller_id"
            " WHERE caller.name='header_call'")
    }
    conn.close()

    assert rows["header_local"][1] == "same_file"
    assert rows["kernel_only"] == (None, "ambiguous")


def test_image_call_respects_shared_header_identity_blockers(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    header = tree / "include" / "linux" / "api.h"
    header.parent.mkdir(parents=True)
    header.write_text("#define shadowed() 0\n")
    image = tree / "arch" / "x86" / "boot"
    image.mkdir(parents=True)
    (image / "main.c").write_text("""\
#include <linux/api.h>
int image_call(void) { return shadowed(); }
""")
    (image / "helper.c").write_text(
        "int shadowed(void) { return 1; }\n")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    row = conn.execute(
        "SELECT c.callee_id,c.resolution FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " WHERE caller.name='image_call' AND c.callee='shadowed'"
    ).fetchone()
    conn.close()

    assert tuple(row) == (None, "ambiguous")


def test_sysfs_attribute_callback_does_not_make_call_ambiguous(tmp_path):
    tree = _tree(tmp_path / "linux-9.9")
    (tree / "dock.c").write_text("""\
static int undock(void) { return 0; }
static int handle_eject(void) { return undock(); }
static DEVICE_ATTR_WO(undock);
""")
    out = tmp_path / "index.db"

    indexer.build(tree, out, "9.9", want_calls=True, jobs=1, quiet=True)
    conn = db.connect(out)
    definitions = list(conn.execute(
        "SELECT name,kind FROM symbols WHERE name IN ('undock','dev_attr_undock')"
        " ORDER BY name,kind"))
    call = conn.execute(
        "SELECT c.resolution,target.name FROM calls c"
        " JOIN symbols caller ON caller.id=c.caller_id"
        " JOIN symbols target ON target.id=c.callee_id"
        " WHERE caller.name='handle_eject' AND c.callee='undock'"
    ).fetchone()
    conn.close()

    assert [tuple(row) for row in definitions] == [
        ("dev_attr_undock", "variable"), ("undock", "function")]
    assert tuple(call) == ("same_file", "undock")
