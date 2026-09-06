"""Call queries, identity ambiguity, and backtrace frame extraction."""

import pytest

from kernel_atlas import db, query
from kernel_atlas.cli import _frames_from_text

from .helpers import names


def test_syscall_definitions_have_call_edges(conn):
    """Regression: the DEFINEn body is a sibling node and used to be skipped."""
    t = query.resolve(conn, "sys_open").target
    assert "do_sys_open" in query.callees(conn, t.id)


def test_call_graph(conn):
    t = query.resolve(conn, "fs/ext4/inode.c:ext4_bmap").target
    assert "ext4_get_block" in query.callees(conn, t.id)
    assert "ext4_bmap" in names(query.callers(conn, "ext4_get_block"))

    callee = query.resolve(conn, "fs/ext4/inode.c:ext4_get_block").target
    inbound = query.callers(conn, callee.id)
    assert inbound[0].resolution == "same_file"


def test_call_queries_preserve_mixed_occurrence_evidence(mini_index, tmp_path):
    import shutil

    copied = tmp_path / "mixed-call-evidence.db"
    shutil.copy(mini_index, copied)
    writer = db.connect(copied, readonly=False)
    writer.execute(
        "UPDATE calls SET direct_count=2,indirect_count=1,macro_count=1"
        " WHERE callee='ext4_get_block'"
    )
    writer.execute(
        "UPDATE meta SET value=(SELECT CAST(SUM(direct_count+indirect_count+"
        " macro_count) AS TEXT) FROM calls) WHERE key='n_call_occurrences'"
    )
    writer.commit()

    caller = query.resolve(writer, "ext4_bmap").target
    outgoing = query.callee_entries(writer, caller.id)
    edge = next(entry for entry in outgoing if entry.name == "ext4_get_block")
    assert (edge.direct_count, edge.indirect_count, edge.macro_count) == (2, 1, 1)

    callee = query.resolve(writer, "ext4_get_block").target
    incoming = query.callers(writer, callee.id)
    edge = next(entry for entry in incoming if entry.name == "ext4_bmap")
    assert (edge.direct_count, edge.indirect_count, edge.macro_count) == (2, 1, 1)
    writer.close()


def test_callers_string_api_rejects_ambiguous_callable_identity(
        mini_index, tmp_path):
    import shutil

    copied = tmp_path / "ambiguous-caller.db"
    shutil.copy(mini_index, copied)
    conn = db.connect(copied, readonly=False)
    row = conn.execute(
        "SELECT file_id,name,kind,start_line,end_line,signature,is_static,"
        " is_inline,is_exported FROM symbols WHERE name='ext4_get_block'"
    ).fetchone()
    conn.execute(
        "INSERT INTO symbols(file_id,name,kind,start_line,end_line,signature,"
        " is_static,is_inline,is_exported) VALUES (?,?,?,?,?,?,?,?,?)",
        (*row[:3], row[3] + 100, row[4] + 100, *row[5:]))
    conn.commit()

    with pytest.raises(ValueError, match="pass a concrete symbol id"):
        query.callers(conn, "ext4_get_block")
    conn.close()


def test_backtrace_frame_extraction():
    oops = """
    BUG: kernel NULL pointer dereference at 0000000000000000
    Call Trace:
     <TASK>
     ext4_bmap+0x12/0x40
     ? __alloc_pages+0x3f0/0x6c0
     tcp_sendmsg+0x59/0x110
     </TASK>
    """
    assert _frames_from_text(oops) == ["ext4_bmap", "__alloc_pages", "tcp_sendmsg"]


def test_backtrace_gdb_style():
    assert _frames_from_text("#3  0xffffffff81 in ext4_bmap (mapping=0x0)") == \
        ["ext4_bmap"]


def test_backtrace_preserves_repeated_short_and_optimized_frames():
    text = """
    x+0x1/0x2
    work.isra.0+0x10/0x20
    x+0x3/0x4
    helper.constprop.17
    """
    assert _frames_from_text(text) == ["x", "work", "x", "helper"]


def test_backtrace_accepts_mixed_case_gdb_and_bare_frames():
    text = "#3  0xffffffff81 in Proc_2 (x=0)\nBDEV_I\n"
    assert _frames_from_text(text) == ["Proc_2", "BDEV_I"]
