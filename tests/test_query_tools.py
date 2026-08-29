"""
tests/test_query_tools.py — Unit tests for edgedash/query/tools.py.

Tests:
  - @_tool decorator registers tools in TOOLS dict
  - Every tool returns {"rows": list, "summary": str}
  - _clamp_int clamps at both bounds and handles bad input
  - Unknown skill returns empty rows, never raises
  - companies_hiring returns correct shape with mock data
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — in-memory DB with realistic data
# ---------------------------------------------------------------------------

_TEST_DB = ":memory:"


def _init_test_db(db_path: str = _TEST_DB) -> str:
    """
    Create an in-memory SQLite DB with enough schema and data for
    the query tools to work.  Returns the path for use with storage.
    """
    # We use a temp file instead of :memory: so storage functions can
    # re-open the same DB.
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = tmp.name

    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            url TEXT NOT NULL,
            description TEXT NOT NULL,
            source TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            fit_score INTEGER NULL,
            fit_reason TEXT NULL,
            scored_at TEXT NULL,
            components_json TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            records_touched INTEGER NOT NULL,
            status TEXT NOT NULL,
            notes TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS extraction_cache (
            description_hash TEXT PRIMARY KEY,
            extraction_json  TEXT NOT NULL,
            cached_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_gap_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            skill TEXT NOT NULL,
            rank INTEGER NOT NULL,
            listings_blocked INTEGER NOT NULL,
            opportunity_cost REAL NOT NULL,
            mean_score REAL NOT NULL,
            top_score INTEGER NOT NULL,
            sample_n INTEGER NOT NULL,
            low_confidence INTEGER NOT NULL,
            nice_to_have_count INTEGER NOT NULL DEFAULT 0,
            example_ids TEXT NOT NULL DEFAULT '[]'
        );
    """)

    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=6)).isoformat()

    # Insert a passing Orchestrator cycle
    conn.execute(
        "INSERT INTO cycle_log (agent, started_at, finished_at, records_touched, status, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Orchestrator", yesterday, yesterday, 10, "complete", "test cycle"),
    )

    # Insert listings from different companies
    import hashlib
    listings = [
        ("id-1", "Data Analyst", "Acme Corp", "Bengaluru", "http://a/1",
         "Desc for listing 1", "test", week_ago, yesterday, 85, "Good match", yesterday),
        ("id-2", "ML Engineer", "Acme Corp", "Bengaluru", "http://a/2",
         "Desc for listing 2", "test", week_ago, yesterday, 72, "Decent match", yesterday),
        ("id-3", "Backend Dev", "Beta Inc", "Mumbai", "http://b/1",
         "Desc for listing 3", "test", yesterday, yesterday, 60, "Partial match", yesterday),
        ("id-4", "SRE", "Gamma Ltd", "Hyderabad", "http://g/1",
         "Desc for listing 4", "test", yesterday, yesterday, None, None, None),
    ]
    for row in listings:
        conn.execute(
            "INSERT INTO listings (id, title, company, location, url, description, "
            "source, posted_at, fetched_at, fit_score, fit_reason, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    # Insert extraction cache entries
    for row in listings:
        desc = row[5]
        desc_hash = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        extraction = {
            "required_skills": ["python", "sql"],
            "nice_to_have": ["kubernetes"],
        }
        conn.execute(
            "INSERT OR IGNORE INTO extraction_cache (description_hash, extraction_json, cached_at) "
            "VALUES (?, ?, ?)",
            (desc_hash, json.dumps(extraction), yesterday),
        )

    # Insert gap snapshots
    run_id = now.isoformat()
    gaps = [
        (run_id, now.isoformat(), "kubernetes", 1, 5, 3.45, 78.0, 90, 5, 0, 2, json.dumps(["id-1", "id-2"])),
        (run_id, now.isoformat(), "terraform", 2, 3, 2.10, 65.0, 80, 3, 0, 0, json.dumps(["id-3"])),
        (run_id, now.isoformat(), "rust", 3, 1, 0.55, 55.0, 55, 1, 1, 0, json.dumps([])),
    ]
    for g in gaps:
        conn.execute(
            "INSERT INTO skill_gap_snapshots "
            "(run_id, computed_at, skill, rank, listings_blocked, opportunity_cost, "
            "mean_score, top_score, sample_n, low_confidence, nice_to_have_count, example_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            g,
        )

    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_db(tmp_path: Path) -> str:
    """Return the path to an initialised test database."""
    return _init_test_db()


@pytest.fixture(autouse=True)
def _patch_db_path(test_db: str):
    """Make all tools use the test database."""
    with patch("edgedash.query.tools._db_path", return_value=test_db):
        yield


@pytest.fixture(autouse=True)
def _patch_aliases():
    """Provide a stable alias map (no config.yaml dependency)."""
    aliases = {"k8s": "kubernetes", "postgresql": "postgres"}
    with patch("edgedash.query.tools._load_aliases", return_value=aliases):
        yield


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:

    def test_tools_populated(self) -> None:
        from edgedash.query.tools import TOOLS
        assert len(TOOLS) == 7

    def test_expected_names(self) -> None:
        from edgedash.query.tools import TOOLS
        expected = {
            "companies_hiring", "best_matches", "top_gaps", "gap_detail",
            "trend", "listing_count", "skill_demand",
        }
        assert set(TOOLS.keys()) == expected

    def test_get_tool_specs_serialisable(self) -> None:
        from edgedash.query.tools import get_tool_specs
        specs = get_tool_specs()
        # Must be JSON-serialisable
        dumped = json.dumps(specs)
        assert isinstance(json.loads(dumped), list)
        assert len(specs) == 7

    def test_every_spec_has_description(self) -> None:
        from edgedash.query.tools import get_tool_specs
        for spec in get_tool_specs():
            assert "description" in spec
            assert len(spec["description"]) > 20


# ---------------------------------------------------------------------------
# Clamping tests (rule 41)
# ---------------------------------------------------------------------------


class TestClamping:

    def test_clamp_low_bound(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int(0, 1, 90, 7) == 1

    def test_clamp_high_bound(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int(200, 1, 90, 7) == 90

    def test_clamp_normal(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int(30, 1, 90, 7) == 30

    def test_clamp_garbage_string(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int("not_a_number", 1, 90, 7) == 7

    def test_clamp_none(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int(None, 1, 90, 7) == 7

    def test_clamp_negative(self) -> None:
        from edgedash.query.tools import _clamp_int
        assert _clamp_int(-5, 1, 25, 10) == 1


# ---------------------------------------------------------------------------
# Tool output shape tests
# ---------------------------------------------------------------------------


class TestOutputShape:
    """Every tool must return {"rows": list, "summary": str}."""

    def _assert_shape(self, result: dict) -> None:
        assert isinstance(result, dict)
        assert "rows" in result
        assert "summary" in result
        assert isinstance(result["rows"], list)
        assert isinstance(result["summary"], str)

    def test_companies_hiring_shape(self) -> None:
        from edgedash.query.tools import companies_hiring
        self._assert_shape(companies_hiring(days=7))

    def test_best_matches_shape(self) -> None:
        from edgedash.query.tools import best_matches
        self._assert_shape(best_matches(n=5))

    def test_top_gaps_shape(self) -> None:
        from edgedash.query.tools import top_gaps
        self._assert_shape(top_gaps(n=3))

    def test_gap_detail_shape(self) -> None:
        from edgedash.query.tools import gap_detail
        self._assert_shape(gap_detail(skill="kubernetes"))

    def test_listing_count_shape(self) -> None:
        from edgedash.query.tools import listing_count
        self._assert_shape(listing_count())

    def test_skill_demand_shape(self) -> None:
        from edgedash.query.tools import skill_demand
        self._assert_shape(skill_demand(skill="python"))


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


class TestCompaniesHiring:

    def test_returns_companies(self) -> None:
        from edgedash.query.tools import companies_hiring
        result = companies_hiring(days=30)
        assert len(result["rows"]) > 0
        for row in result["rows"]:
            assert "company" in row
            assert "listings" in row
            assert isinstance(row["listings"], int)

    def test_clamping_at_boundary(self) -> None:
        from edgedash.query.tools import companies_hiring
        # days=0 should be clamped to 1
        result = companies_hiring(days=0)
        assert isinstance(result["rows"], list)

    def test_days_100_clamped_to_90(self) -> None:
        from edgedash.query.tools import companies_hiring
        # Should not raise, days clamped to 90
        result = companies_hiring(days=100)
        assert "90 days" in result["summary"]


class TestBestMatches:

    def test_returns_scored_listings(self) -> None:
        from edgedash.query.tools import best_matches
        result = best_matches(n=10)
        assert len(result["rows"]) > 0
        for row in result["rows"]:
            assert "score" in row
            assert "title" in row
            assert "company" in row

    def test_ordered_by_score(self) -> None:
        from edgedash.query.tools import best_matches
        result = best_matches(n=10)
        scores = [r["score"] for r in result["rows"]]
        assert scores == sorted(scores, reverse=True)


class TestGapDetail:

    def test_known_skill(self) -> None:
        from edgedash.query.tools import gap_detail
        result = gap_detail(skill="kubernetes")
        assert len(result["rows"]) > 0

    def test_unknown_skill_empty_not_raise(self) -> None:
        from edgedash.query.tools import gap_detail
        result = gap_detail(skill="nonexistent_skill_xyz_123")
        assert result["rows"] == []
        assert isinstance(result["summary"], str)

    def test_alias_resolution(self) -> None:
        """k8s should resolve to kubernetes via the alias map."""
        from edgedash.query.tools import gap_detail
        result = gap_detail(skill="k8s")
        # Should find the same gap as "kubernetes"
        assert len(result["rows"]) > 0


class TestListingCount:

    def test_returns_counts(self) -> None:
        from edgedash.query.tools import listing_count
        result = listing_count()
        row = result["rows"][0]
        assert row["total_listings"] == 4
        assert row["scored"] == 3
        assert row["unscored"] == 1


class TestSkillDemand:

    def test_known_skill(self) -> None:
        from edgedash.query.tools import skill_demand
        result = skill_demand(skill="python")
        assert len(result["rows"]) == 1
        assert result["rows"][0]["required_in"] > 0

    def test_unknown_skill_empty(self) -> None:
        from edgedash.query.tools import skill_demand
        result = skill_demand(skill="ancient_sumerian_pottery")
        assert result["rows"][0]["total_mentions"] == 0


# ---------------------------------------------------------------------------
# No-good-cycle guard (rule 46)
# ---------------------------------------------------------------------------


class TestNoGoodCycle:

    def test_all_tools_return_empty_on_no_cycle(self) -> None:
        from edgedash.query.tools import (
            companies_hiring, best_matches, top_gaps,
            gap_detail, listing_count, skill_demand,
        )
        with patch("edgedash.query.tools.storage.get_last_good_cycle", return_value=None):
            for fn, kwargs in [
                (companies_hiring, {"days": 7}),
                (best_matches, {"n": 5}),
                (top_gaps, {"n": 3}),
                (gap_detail, {"skill": "python"}),
                (listing_count, {}),
                (skill_demand, {"skill": "python"}),
            ]:
                result = fn(**kwargs)
                assert result["rows"] == [], f"{fn.__name__} should return empty rows"
                assert "No passing cycle" in result["summary"]
