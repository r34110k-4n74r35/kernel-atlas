import os

import pytest

from kernel_atlas.storage import config as storage, db
from kernel_atlas.indexing import indexer

from tests.support.kernel_tree import make_mini_kernel
from tests.support.indexes import (
    path_index as path_index,
    storage_roots as storage_roots,
    study_index as study_index,
)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Keep generated kernel fixtures/indexes local, with numbered pytest runs."""
    try:
        root = storage.project_root()
        assert root is not None
        temporary = storage.require_project_path(root / ".pytest_cache" / "tmp")
        if config.option.basetemp:
            # Pytest deletes --basetemp recursively. Only its dedicated scratch
            # subtree can be used, never application data or source directories.
            storage.require_project_path(config.option.basetemp).relative_to(temporary)
        temporary.mkdir(parents=True, exist_ok=True)
        os.environ["PYTEST_DEBUG_TEMPROOT"] = str(temporary)
    except (ValueError, OSError) as exc:
        raise pytest.UsageError(f"invalid project test temporary directory: {exc}") from exc


@pytest.fixture(autouse=True)
def isolated_application_home(tmp_path, monkeypatch):
    """Tests must not create application locks or metadata in the user's data."""
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "app-data"))


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
