"""Stored call and documentation evidence remains valid and compatible."""

from contextlib import closing

import pytest

from kernel_atlas.indexing import indexer
from kernel_atlas.storage import db, evidence
from tests.support.kernel_tree import make_mini_kernel


@pytest.fixture
def recorded(tmp_path):
    tree = make_mini_kernel(tmp_path / "linux")
    out = tmp_path / "index.db"
    indexer.build(tree, out, "6.12.104", jobs=1, quiet=True, want_calls=True)
    return tree, out


def test_legacy_index_readable_without_optional_evidence(recorded):
    _, out = recorded
    with closing(db.connect(out, readonly=False)) as conn:
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        for flag, (table, _) in evidence.CAPABILITY_TABLES.items():
            conn.execute("DELETE FROM meta WHERE key=?", (flag,))
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        meta = db.validate_schema(conn, deep=True)
        assert meta["schema_version"] == "6"


@pytest.mark.parametrize(
    "field,expression", [("byte_offset", "999999999"), ("line", "-1")]
)
def test_call_locations_must_fit_indexed_source(recorded, field, expression):
    _, out = recorded
    with closing(db.connect(out, readonly=False)) as conn:
        conn.execute(f"UPDATE call_sites SET {field}={expression}")
        with pytest.raises(db.SchemaError, match="call-site"):
            db.validate_schema(conn, deep=True)


def test_duplicate_call_sites_fail_occurrence_audit(recorded):
    _, out = recorded
    with closing(db.connect(out, readonly=False)) as conn:
        conn.execute("INSERT INTO call_sites SELECT * FROM call_sites LIMIT 1")
        with pytest.raises(db.SchemaError, match="occurrence counts"):
            db.validate_schema(conn, deep=True)


def test_build_stores_only_retained_feature_evidence(recorded):
    _, out = recorded
    with closing(db.connect(out)) as conn:
        meta = db.validate_schema(conn, deep=True)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"call_sites", "function_docs", "document_text"} <= tables
        assert not {"file_fingerprints", "parse_cache"} & tables
        assert (
            not {
                "has_source_fingerprints",
                "has_parse_cache",
                "source_fingerprint",
                "parse_cache_key",
                "n_parse_reused",
                "parser_version",
                "indexer_version",
            }
            & meta.keys()
        )
        assert all(meta[flag] == "1" for flag in evidence.CAPABILITY_TABLES)
