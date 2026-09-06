"""Fixtures shared by command behavior tests."""

import pytest

from .helpers import _fake_index


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ATLAS_HOME", str(tmp_path))
    _fake_index(tmp_path, "6.18.45")
    _fake_index(tmp_path, "7.2")
    return tmp_path
