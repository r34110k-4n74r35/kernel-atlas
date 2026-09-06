"""CLI builds, safe output publication, and source identity checks."""

from __future__ import annotations

import pytest

from kernel_atlas.commands import cli
from kernel_atlas.storage import config, kernelsrc

from .helpers import _fake_index


def test_local_build_rejects_an_output_inside_the_source_tree(
        mini_tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit):
        cli.main(["build", "--src", str(mini_tree),
                  "--output", str(mini_tree / "atlas.db"), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
    assert not (mini_tree / "atlas.db").exists()


def test_local_build_rejects_download_alias_as_a_literal_version(
        mini_tree, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main([
            "build", "lts", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "does not apply with --src" in capsys.readouterr().err


def test_local_build_requires_detectable_or_explicit_version(tmp_path, capsys):
    tree = tmp_path / "kernel-tree"
    tree.mkdir()
    (tree / "MAINTAINERS").write_text("TEST\nF: *\n")
    (tree / "Makefile").write_text("not a kernel version\n")

    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    assert "could not detect a kernel version" in capsys.readouterr().err


def test_build_reports_expected_indexer_failures_without_a_traceback(
        mini_tree, tmp_path, monkeypatch, capsys):
    # Even a failed build creates a publication lock. It must stay with the
    # test data, not accumulate in the user's real indexes/.lifecycle-locks/.
    isolated_home = config.data_root()
    assert isolated_home.is_relative_to(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("cannot scan protected directory")

    monkeypatch.setattr(cli.indexer, "build", fail)
    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(mini_tree),
            "--output", str(tmp_path / "index.db"), "--quiet",
        ])
    err = capsys.readouterr().err
    assert "could not build index: cannot scan protected directory" in err
    assert "Traceback" not in err
    locks = kernelsrc._output_lock_paths(tmp_path / "index.db")
    assert all(lock.is_relative_to(isolated_home) and lock.is_file() for lock in locks)


def test_build_rejects_external_output_even_with_force(
        mini_tree, tmp_path, monkeypatch, capsys):
    # Both sides of this simulated boundary are in pytest's local scratch area.
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "personal-notes.db"
    outside.write_bytes(b"preserve this file")
    monkeypatch.setattr(config, "project_root", lambda: project)
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(project))

    with pytest.raises(SystemExit):
        cli.main(["build", "--src", str(mini_tree), "--output", str(outside),
                  "--force", "--quiet"])

    assert "outside the project" in capsys.readouterr().err
    assert outside.read_bytes() == b"preserve this file"
    assert list(project.iterdir()) == []


def test_build_rechecks_output_existence_under_its_publication_lock(
        mini_tree, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from kernel_atlas.storage import kernelsrc

    output = tmp_path / "study.db"

    @contextmanager
    def racing_output_lock(path):
        assert path == output
        path.write_bytes(b"published by another build")
        yield

    monkeypatch.setattr(kernelsrc, "output_lock", racing_output_lock)
    monkeypatch.setattr(
        cli.indexer, "build",
        lambda *args, **kwargs: pytest.fail("existing output must not be rebuilt"),
    )

    with pytest.raises(SystemExit):
        cli.main([
            "build", "--src", str(mini_tree), "--output", str(output),
            "--quiet",
        ])

    assert output.read_bytes() == b"published by another build"
    assert "index already exists" in capsys.readouterr().err


def test_managed_build_holds_source_then_output_locks_through_publication(
        mini_tree, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from kernel_atlas.indexing import indexer
    from kernel_atlas.storage import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    output = tmp_path / "study.db"
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    state = {"source": False, "output": False}
    events = []

    @contextmanager
    def source_lock(version):
        assert version == "6.12.104"
        assert not state["output"]
        state["source"] = True
        events.append("source+")
        try:
            yield
        finally:
            events.append("source-")
            state["source"] = False

    @contextmanager
    def output_lock(path):
        assert state["source"]
        assert path == output
        state["output"] = True
        events.append("output+")
        try:
            yield
        finally:
            events.append("output-")
            state["output"] = False

    def ensure_source(version, **kwargs):
        assert state == {"source": True, "output": True}
        events.append("source-ready")
        return mini_tree

    def build(tree, out, version, **kwargs):
        assert state == {"source": True, "output": True}
        events.append("published")
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(kernelsrc, "output_lock", output_lock)
    monkeypatch.setattr(kernelsrc, "ensure_source", ensure_source)
    monkeypatch.setattr(cli.indexer, "build", build)

    assert cli.main([
        "build", "lts", "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert events == [
        "source+", "output+", "source-ready", "published", "output-", "source-",
    ]


def test_custom_build_of_a_managed_cache_path_also_holds_its_source_lock(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil
    from contextlib import contextmanager

    from kernel_atlas.indexing import indexer
    from kernel_atlas.storage import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    output = tmp_path / "study.db"
    locked = False

    @contextmanager
    def source_lock(version):
        nonlocal locked
        assert version == "6.12.104"
        locked = True
        try:
            yield
        finally:
            locked = False

    def build(tree, out, version, **kwargs):
        assert locked
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(cli.indexer, "build", build)

    assert cli.main([
        "build", "local-study", "--src", str(managed),
        "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert not locked


def test_custom_build_symlink_alias_locks_the_canonical_managed_tree(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil
    from contextlib import contextmanager

    from kernel_atlas.indexing import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    alias = home / "kernels" / "linux-study"
    try:
        alias.symlink_to(managed, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    seen = []

    @contextmanager
    def source_lock(version):
        seen.append(version)
        yield

    def build(tree, out, version, **kwargs):
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "source_lock", source_lock)
    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "study.db"
    assert cli.main([
        "build", "--src", str(alias), "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert seen == ["6.12.104"]


def test_output_containment_checks_the_symlink_entry_not_its_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    output = source / "atlas.db"
    try:
        output.symlink_to(tmp_path / "outside.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert cli._path_inside(output, source)


def test_custom_build_output_is_not_blocked_by_the_managed_index(
        mini_tree, tmp_path, monkeypatch, capsys):
    from kernel_atlas.indexing import indexer
    from kernel_atlas.storage import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    _fake_index(home, "6.12.104")
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release("longterm", "6.12.104", source_url, None),
    )
    seen = {}

    def fake_source(version, **kwargs):
        seen["source_url"] = kwargs["source_url"]
        return mini_tree

    def fake_build(tree, out, version, **kwargs):
        seen["metadata_source"] = kwargs["source"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(kernelsrc, "ensure_source", fake_source)
    monkeypatch.setattr(cli.indexer, "build", fake_build)
    custom = tmp_path / "custom.db"
    assert cli.main(["build", "lts", "--output", str(custom), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert custom.read_bytes() == b"index"
    assert seen == {
        "source_url": source_url,
        "metadata_source": str(mini_tree),
    }
    assert f"{cli.PROG} --db {custom} info mm" in out
    assert f"{cli.PROG} --db {custom} siblings mm/page_alloc.c" in out


def test_modified_managed_cache_is_recorded_as_local_source(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil

    from kernel_atlas.indexing import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    kernelsrc._write_source_identity(
        "6.12.104", managed, source_url, authoritative=True)
    (managed / "README.local").write_text("study edit\n")
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    monkeypatch.setattr(kernelsrc, "ensure_source", lambda *a, **kw: managed)
    seen = {}

    def build(tree, out, version, **kwargs):
        seen.update(kwargs)
        out.write_bytes(b"index")
        return indexer.BuildStats()

    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "modified.db"
    assert cli.main([
        "build", "6.12.104", "--output", str(output), "--quiet",
    ]) == 0
    capsys.readouterr()
    assert seen["source"] == str(managed)
    assert seen["managed_tree_identity"] is None


def test_managed_source_change_during_build_prevents_publication(
        mini_tree, tmp_path, monkeypatch, capsys):
    import shutil

    from kernel_atlas.indexing import indexer

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    managed = config.source_path("6.12.104")
    shutil.copytree(mini_tree, managed)
    source_url = "https://cdn.kernel.org/example/linux-6.12.104.tar.xz"
    kernelsrc._write_source_identity(
        "6.12.104", managed, source_url, authoritative=True)
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104", source_url, None),
    )
    monkeypatch.setattr(kernelsrc, "ensure_source", lambda *a, **kw: managed)

    def build(tree, out, version, **kwargs):
        (tree / "README.changed").write_text("changed during build\n")
        kwargs["pre_publish"]()
        out.write_bytes(b"must not publish")
        return indexer.BuildStats()

    monkeypatch.setattr(cli.indexer, "build", build)
    output = tmp_path / "changed.db"
    with pytest.raises(SystemExit):
        cli.main([
            "build", "6.12.104", "--output", str(output), "--quiet",
        ])

    assert not output.exists()
    assert "managed source changed while the index was built" in (
        capsys.readouterr().err)


def test_downloaded_build_rejects_output_inside_managed_source_before_fetch(
        tmp_path, monkeypatch, capsys):
    from kernel_atlas.storage import kernelsrc

    home = tmp_path / "home"
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(home))
    monkeypatch.setattr(
        kernelsrc, "resolve_version",
        lambda spec: kernelsrc.Release(
            "longterm", "6.12.104",
            "https://cdn.kernel.org/example/linux-6.12.104.tar.xz", None),
    )
    monkeypatch.setattr(
        kernelsrc, "ensure_source",
        lambda *a, **kw: pytest.fail("unsafe output should fail before download"),
    )
    out = home / "kernels" / "linux-6.12.104" / "atlas.db"
    with pytest.raises(SystemExit):
        cli.main(["build", "lts", "--output", str(out), "--quiet"])
    assert "inside the source tree" in capsys.readouterr().err
