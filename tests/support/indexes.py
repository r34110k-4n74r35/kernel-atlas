"""Shared index fixtures for query, command, and storage-boundary tests."""

from contextlib import closing

import pytest

from kernel_atlas.indexing import indexer
from kernel_atlas.storage import config, db


@pytest.fixture(scope="module")
def study_index(tmp_path_factory):
    root = tmp_path_factory.mktemp("documentation-study")
    tree = root / "linux"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text(
        "USB STUDY\nF: include/linux/usb.h\nF: Documentation/usb/\n"
        "F: Documentation/driver-api/usb/\n"
        "F: Documentation/devicetree/bindings/usb/\n",
        encoding="utf-8",
    )
    contents = {
        "include/linux/usb.h": "struct usb_device { int address; };\n",
        "core.c": "struct packet_queue { int length; };\n",
        "Documentation/usb/index.rst": "USB guide\n",
        "Documentation/driver-api/usb/device.rst": "USB device API\n",
        "Documentation/devicetree/bindings/usb/usb-device.yaml": "title: USB device\n",
        "Documentation/devicetree/bindings/usb/vendor.txt": "USB binding\n",
        "Documentation/usb/Makefile": "# build instructions\n",
        "Documentation/networking/packet_queue.rst": "Queue design\n",
        "Documentation/io_uring/overview.rst": "Literal underscore\n",
        "Documentation/ioxuring/overview.rst": "Different directory\n",
        "Documentation/100%/index.rst": "Literal percent\n",
        "Documentation/100more/index.rst": "Different directory\n",
    }
    for path, content in contents.items():
        full = tree / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    out = root / "study.db"
    indexer.build(tree, out, "9.9", jobs=1, quiet=True)
    return out


@pytest.fixture
def path_index(mini_index, tmp_path):
    out = tmp_path / "paths.db"
    with closing(db.connect(mini_index)) as source, closing(
        db.connect(out, readonly=False)
    ) as conn:
        source.backup(conn)
        root_id = conn.execute("SELECT id FROM dirs WHERE path=''").fetchone()[0]
        conn.execute("UPDATE dirs SET name='000-root' WHERE id=?", (root_id,))
        for path in ("Area", "area", "a[bc]*?_%", "abcXYZ"):
            cursor = conn.execute(
                "INSERT INTO dirs(path,parent_id,name,depth) VALUES (?,?,?,1)",
                (path, root_id, path),
            )
            directory = cursor.lastrowid
            cursor = conn.execute(
                "INSERT INTO files(path,dir_id,name,ext) VALUES (?,?,?,'.c')",
                (f"{path}/unit.c", directory, "unit.c"),
            )
            conn.execute(
                "INSERT INTO symbols(file_id,name,kind,start_line,end_line)"
                " VALUES (?,'unit','function',1,1)", (cursor.lastrowid,),
            )
            conn.execute(
                "INSERT INTO dirs(path,parent_id,name,depth) VALUES (?,?,?,2)",
                (f"{path}/child", directory, "child"),
            )
        conn.commit()
    return out


@pytest.fixture
def storage_roots(tmp_path, monkeypatch):
    # Both locations are real project-local test fixtures.  Only the first is
    # the simulated checkout, so rejection tests never write outside the repo.
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.setattr(config, "project_root", lambda: project)
    return project, outside
