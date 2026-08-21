from kernel_atlas import config


def test_data_lives_inside_the_project_checkout():
    root = config.project_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert config.data_root() == root
    assert config.sources_dir() == root / "kernels"
    assert config.index_dir() == root / "indexes"


def test_env_var_overrides_the_location(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert config.data_root() == tmp_path
    assert config.source_path("6.12") == tmp_path / "kernels" / "linux-6.12"
    assert config.index_path("6.12") == tmp_path / "indexes" / "6.12.db"


def _fake_tree(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "MAINTAINERS").write_text("x")
    return path


def test_tree_for_prefers_current_layout_over_a_stale_recorded_path(
        monkeypatch, tmp_path):
    """An index built before the data directory moved must still find source."""
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    local = _fake_tree(tmp_path / "kernels" / "linux-9.9")
    assert config.tree_for("9.9", "/somewhere/that/moved") == local


def test_tree_for_falls_back_to_the_recorded_path(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "empty"))
    elsewhere = _fake_tree(tmp_path / "elsewhere")
    assert config.tree_for("9.9", str(elsewhere)) == elsewhere


def test_tree_for_returns_none_when_source_is_gone(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert config.tree_for("9.9", None) is None
    assert config.tree_for("9.9", "/nope") is None
