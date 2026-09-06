"""Minimal valid database metadata and source trees for storage tests."""

from pathlib import Path

from kernel_atlas.storage import db


def _metadata(**overrides):
    values = {
        "schema_version": db.SCHEMA_VERSION,
        "kernel_version": "9.9",
        "source": "test",
        "tree_path": "/tmp/linux-9.9",
        "built_at": "2026-01-01T00:00:00",
        "kinds": "function",
        "has_calls": "0",
        "n_dirs": "1",
        "n_files": "0",
        "n_symbols": "0",
        "n_type_aliases": "0",
        "n_type_members": "0",
        "n_subsystems": "0",
        "n_calls": "0",
        "n_call_occurrences": "0",
        "n_calls_resolved": "0",
        "n_calls_ambiguous": "0",
        "n_calls_macro": "0",
        "n_calls_indirect": "0",
        "n_calls_unresolved": "0",
        "n_parse_skipped": "0",
        "n_parse_failed": "0",
        "n_oversize": "0",
        "n_symlinks": "0",
        "build_seconds": "0.0",
    }
    values.update(overrides)
    if "n_call_occurrences" not in overrides:
        values["n_call_occurrences"] = values["n_calls"]
    return list(values.items())


def _make_tree(path: Path, version=("6", "12", "104", "")) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    major, patch, sublevel, extra = version
    (path / "MAINTAINERS").write_text("TEST\nM: A <a@example.com>\nF: *\n")
    (path / "Makefile").write_text(
        f"VERSION = {major}\nPATCHLEVEL = {patch}\nSUBLEVEL = {sublevel}\n"
        f"EXTRAVERSION = {extra}\n",
        encoding="utf-8",
    )
    return path


def _identity_index(tmp_path, resolution: str, *, target_static: int = 0,
                    target_name: str = "target"):
    conn = db.create(tmp_path / f"bad-{resolution}.db")
    conn.execute(
        "INSERT INTO dirs(id,path,parent_id,name,depth,n_files,n_files_recursive)"
        " VALUES (1,'',NULL,'linux',0,2,2)")
    conn.executemany(
        "INSERT INTO files(id,path,dir_id,name,ext,lines,n_symbols,index_status)"
        " VALUES (?,?,1,?,'.c',1,1,'parsed')",
        [(1, "one.c", "one.c"), (2, "two.c", "two.c")])
    conn.executemany(
        "INSERT INTO symbols(id,file_id,name,kind,start_line,end_line,is_static)"
        " VALUES (?,?,?,?,1,1,?)",
        [(1, 1, "caller", "function", 0),
         (2, 2, target_name, "function", target_static)])
    conn.execute(
        "INSERT INTO calls(caller_id,callee,callee_id,resolution)"
        " VALUES (1,'target',2,?)", (resolution,))
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES (?,?)",
        _metadata(has_calls="1", n_files="2", n_symbols="2", n_calls="1",
                  n_calls_resolved="1",
                  kinds="function,syscall,macro,variable"))
    conn.commit()
    return conn
