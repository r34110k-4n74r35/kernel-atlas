"""Index publication stays within project paths without changing alias targets."""

import pytest

from kernel_atlas.indexing import indexer


@pytest.mark.parametrize("redirect_parent", [False, True])
def test_build_rejects_external_output_before_creating_files(
        storage_roots, mini_tree, redirect_parent):
    project, outside = storage_roots
    if redirect_parent:
        parent = project / "redirected"
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent = outside
    out = parent / "new-directory" / "index.db"

    with pytest.raises(ValueError, match="project"):
        indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True)

    assert not (outside / "new-directory").exists()


def test_build_replaces_local_leaf_alias_without_changing_target(storage_roots, mini_tree):
    project, outside = storage_roots
    target = outside / "keep.db"
    target.write_bytes(b"keep this file")
    out = project / "index.db"
    out.symlink_to(target)

    stats = indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True)

    assert stats.files > 0
    assert target.read_bytes() == b"keep this file"
    assert not out.is_symlink()
    assert not list(project.glob("*.building"))


def test_build_rechecks_parent_before_publication(storage_roots, mini_tree):
    project, outside = storage_roots
    parent = project / "output"
    parent.mkdir()
    out = parent / "index.db"
    parked = project / "parked"
    outside_scratch = []

    def redirect_parent():
        # Keep the open database inside the simulated project, but redirect
        # future path operations and plant a same-name file outside it.
        scratch = next(parent.glob("*.building"))
        sentinel = outside / scratch.name
        sentinel.write_bytes(b"keep external scratch")
        outside_scratch.append(sentinel)
        parent.rename(parked)
        parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="project"):
        indexer.build(mini_tree, out, "9.9", jobs=1, quiet=True,
                      pre_publish=redirect_parent)

    assert not (outside / "index.db").exists()
    assert outside_scratch[0].read_bytes() == b"keep external scratch"
    assert len(list(parked.glob("*.building"))) == 1
