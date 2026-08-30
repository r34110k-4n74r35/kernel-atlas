import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fixture import make_mini_kernel  # noqa: E402

from kernel_atlas import db, indexer  # noqa: E402


@pytest.fixture(scope="session")
def mini_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("linux-6.12.104")
    return make_mini_kernel(root)


@pytest.fixture(scope="session")
def mini_index(mini_tree, tmp_path_factory):
    out = tmp_path_factory.mktemp("index") / "test.db"
    indexer.build(
        mini_tree, out, "6.12.104", want_calls=True, jobs=2, quiet=True,
        source=("https://cdn.kernel.org/pub/linux/kernel/v6.x/"
                "linux-6.12.104.tar.xz"),
    )
    return out


@pytest.fixture()
def conn(mini_index):
    c = db.connect(mini_index, readonly=True)
    yield c
    c.close()
