import pytest

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


@pytest.mark.parametrize("version", ["../outside", "/tmp/outside", "a/b", "a\\b", " x"])
def test_version_paths_reject_unsafe_components(monkeypatch, tmp_path, version):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="unsafe kernel version"):
        config.index_path(version)
    with pytest.raises(ValueError, match="unsafe kernel version"):
        config.source_path(version)


def test_vendor_version_is_filename_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert (config.index_path("6.6.12-acme+debug")
            == tmp_path / "indexes" / "6.6.12-acme+debug.db")


def test_list_indexes_excludes_directories_and_dangling_symlinks(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    indexes = config.index_dir()
    indexes.mkdir()
    regular = indexes / "7.2.db"
    regular.write_bytes(b"not necessarily valid SQLite")
    (indexes / "directory.db").mkdir()
    dangling = indexes / "dangling.db"
    try:
        dangling.symlink_to(tmp_path / "missing.db")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert config.list_indexes() == [regular]


def test_absent_pin_is_the_only_no_pin_state(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    assert config.get_default_version() is None


@pytest.mark.parametrize("contents", ["", "\n", "/tmp/not-an-index\n"])
def test_invalid_hand_edited_pin_is_reported(monkeypatch, tmp_path, contents):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    pin = config.default_version_file()
    pin.parent.mkdir(parents=True)
    pin.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="default version pin"):
        config.get_default_version()


def test_non_utf8_pin_is_reported_without_echoing_contents(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    pin = config.default_version_file()
    pin.parent.mkdir(parents=True)
    pin.write_bytes(b"sensitive-marker:\xff")

    with pytest.raises(ValueError, match="not valid UTF-8") as raised:
        config.get_default_version()
    assert "sensitive-marker" not in str(raised.value)


def test_pin_read_io_errors_are_not_treated_as_no_pin(monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    pin = config.default_version_file()
    pin.mkdir(parents=True)

    with pytest.raises(IsADirectoryError):
        config.get_default_version()


def test_set_default_version_replaces_a_leaf_symlink_without_writing_target(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path / "home"))
    pin = config.default_version_file()
    pin.parent.mkdir(parents=True)
    victim = tmp_path / "personal-notes"
    victim.write_text("do not overwrite\n", encoding="utf-8")
    try:
        pin.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    config.set_default_version("6.12.104")

    assert victim.read_text(encoding="utf-8") == "do not overwrite\n"
    assert not pin.is_symlink()
    assert config.get_default_version() == "6.12.104"
    assert not list(pin.parent.glob("..default-version.*.tmp"))


def test_set_default_version_failure_preserves_complete_old_pin_and_cleans_temp(
        monkeypatch, tmp_path):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    config.set_default_version("6.12.104")
    pin = config.default_version_file()

    def interrupted_replace(self, target):
        assert target == pin
        raise OSError("simulated publication failure")

    monkeypatch.setattr(type(pin), "replace", interrupted_replace)
    with pytest.raises(OSError, match="publication failure"):
        config.set_default_version("7.2")

    assert pin.read_text(encoding="utf-8") == "6.12.104\n"
    assert not list(pin.parent.glob("..default-version.*.tmp"))


def test_project_root_does_not_claim_an_unrelated_src_layout_project(
        monkeypatch, tmp_path):
    host = tmp_path / "unrelated-app"
    (host / "src").mkdir(parents=True)
    (host / "pyproject.toml").write_text("[project]\nname='unrelated'\n")
    installed = host / ".venv/lib/python3.12/site-packages/kernel_atlas/config.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(installed))
    assert config.project_root() is None
