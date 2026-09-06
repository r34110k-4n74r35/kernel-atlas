"""Documentation relevance, scope filtering, and query evidence."""

from __future__ import annotations

from contextlib import closing

import pytest

from kernel_atlas.storage import db
from kernel_atlas.queries import query
from kernel_atlas.queries.documentation import documentation_scope


def test_structure_study_prefers_api_guides_to_bindings(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "usb_device").target
        matches = query.documentation_matches(conn, target, limit=2)
        assert [m.entry.path for m in matches] == [
            "Documentation/driver-api/usb/device.rst",
            "Documentation/usb/index.rst",
        ]
        assert "prose guide preferred for code study" in matches[0].reasons
        assert "claimed by target owner: USB STUDY" in matches[0].reasons
        assert query.documentation_for(conn, target, 2) == [m.entry for m in matches]


def test_symbol_name_supplies_evidence_without_a_maintainers_owner(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "packet_queue").target
        assert [e.path for e in query.documentation_for(conn, target)] == [
            "Documentation/networking/packet_queue.rst"
        ]


def test_scope_is_applied_before_limit_and_keeps_bindings_available(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve_structure(conn, "usb_device").target
        matches = query.documentation_matches(
            conn, target, 1, under="devicetree/bindings"
        )
        assert matches[0].entry.path.endswith("usb-device.yaml")
        exact = query.resolve(conn, matches[0].entry.path).target
        assert query.documentation_matches(conn, exact, 1)[0].entry.path == exact.path


@pytest.mark.parametrize("scope", ["io_uring", "100%"])
def test_scope_uses_literal_sql_prefix_and_zero_means_all(study_index, scope):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        entries = query.documentation_for(conn, target, 0, under=scope)
        assert len(entries) == 1
        assert entries[0].path.startswith(f"Documentation/{scope}/")


def test_scope_is_case_sensitive_and_accepts_an_exact_file(study_index):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        assert query.documentation_for(conn, target, under="USB") == []
        exact = "Documentation/usb/index.rst"
        assert [e.path for e in query.documentation_for(conn, target, under=exact)] == [
            exact
        ]


@pytest.mark.parametrize(
    "scope", ["", "../usb", "/tmp/usb", "usb/../net", "usb\\net", "C:usb"]
)
def test_unsafe_or_empty_scopes_are_rejected(scope):
    with pytest.raises(ValueError):
        documentation_scope(scope)


@pytest.mark.parametrize("limit", [-1, True, 1.5, "2"])
def test_library_rejects_invalid_limits(study_index, limit):
    with closing(db.connect(study_index)) as conn:
        target = query.resolve(conn, "Documentation").target
        with pytest.raises(ValueError, match="non-negative integer"):
            query.documentation_for(conn, target, limit)
