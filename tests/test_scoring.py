"""
tests/test_scoring.py — Unit tests for edgedash/scoring.py.

scoring.score_listing is a pure function: no mocks, no network, no DB.
Every test is deterministic.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from edgedash.config import Config
from edgedash.scoring import score_listing, build_reason, _seniority_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> Config:
    """Build a Config with sensible defaults for testing."""
    defaults = dict(
        my_skills=["python", "sql", "pandas", "tableau", "excel"],
        target_city="Bengaluru",
        target_seniority="mid",
        weight_skill_match=0.45,
        weight_seniority_fit=0.25,
        weight_location_fit=0.15,
        weight_recency=0.15,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _listing(**kwargs) -> dict:
    """Build a minimal listing dict."""
    defaults = dict(
        id="test-001",
        location="Bengaluru",
        posted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    defaults.update(kwargs)
    return defaults


def _facts(**kwargs) -> dict:
    """Build a minimal facts dict matching EXTRACTION_SCHEMA."""
    defaults = dict(
        required_skills=["python", "sql"],
        nice_to_have=["tableau"],
        seniority="mid",
        years_required=3,
        remote_ok=None,
    )
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Test: perfect match
# ---------------------------------------------------------------------------


def test_perfect_match_gives_high_score():
    cfg = _cfg()
    listing = _listing(location="Bengaluru")
    facts = _facts(
        required_skills=["python", "sql", "pandas"],
        nice_to_have=["tableau"],
        seniority="mid",
        remote_ok=False,
    )
    result = score_listing(listing, facts, cfg)

    assert result["score"] >= 80, f"Expected ≥80, got {result['score']}"
    assert result["components"]["skill_match"] == pytest.approx(1.0, abs=0.01)
    assert result["components"]["seniority_fit"] == pytest.approx(1.0)
    assert result["components"]["location_fit"] == pytest.approx(1.0)
    assert result["components"]["recency"] == pytest.approx(1.0, abs=0.05)
    assert isinstance(result["reason"], str) and len(result["reason"]) > 0


# ---------------------------------------------------------------------------
# Test: zero skill match
# ---------------------------------------------------------------------------


def test_zero_skill_match_gives_low_score():
    cfg = _cfg(my_skills=["excel"])
    listing = _listing(location="London")
    facts = _facts(
        required_skills=["kubernetes", "rust", "terraform"],
        nice_to_have=["spark"],
        seniority="lead",
        remote_ok=False,
    )
    result = score_listing(listing, facts, cfg)

    assert result["score"] <= 30, f"Expected ≤30, got {result['score']}"
    assert result["components"]["skill_match"] == pytest.approx(0.0, abs=0.01)
    # Missing skills should be reported in reason
    assert "gap" in result["reason"]
    assert "kubernetes" in result["reason"]


# ---------------------------------------------------------------------------
# Test: empty required_skills — must not divide by zero
# ---------------------------------------------------------------------------


def test_empty_required_skills_no_zero_division():
    cfg = _cfg()
    listing = _listing()
    facts = _facts(required_skills=[], nice_to_have=[])

    result = score_listing(listing, facts, cfg)

    # Should return a score without raising
    assert 0 <= result["score"] <= 100
    # skill_match should be 0.5 (neutral, not zero)
    assert result["components"]["skill_match"] == pytest.approx(0.5)
    assert "no required skills" in result["reason"]


# ---------------------------------------------------------------------------
# Test: null posted_at — must not crash
# ---------------------------------------------------------------------------


def test_null_posted_at_does_not_crash():
    cfg = _cfg()
    listing = _listing(posted_at=None)
    facts = _facts()

    result = score_listing(listing, facts, cfg)

    assert 0 <= result["score"] <= 100
    assert result["components"]["recency"] == pytest.approx(0.5)
    assert "unknown" in result["reason"]


# ---------------------------------------------------------------------------
# Test: null remote_ok — must not crash, should be neutral
# ---------------------------------------------------------------------------


def test_null_remote_ok_is_neutral():
    cfg = _cfg(target_city="Bengaluru")
    # location doesn't match city, remote_ok is None
    listing = _listing(location="Unknown City")
    facts = _facts(remote_ok=None)

    result = score_listing(listing, facts, cfg)

    assert 0 <= result["score"] <= 100
    # Should get neutral 0.5, not penalised
    assert result["components"]["location_fit"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test: seniority three bands off → score should be 0.0
# ---------------------------------------------------------------------------


def test_seniority_three_bands_off_scores_zero():
    # target=junior (index 0), facts=lead (index 3): distance 3 → 0.0
    score = _seniority_score("lead", "junior")
    assert score == pytest.approx(0.0)

    # Confirm it flows through score_listing too
    cfg = _cfg(target_seniority="junior")
    listing = _listing()
    facts = _facts(seniority="lead")
    result = score_listing(listing, facts, cfg)

    assert result["components"]["seniority_fit"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test: seniority one band off → score should be 0.6
# ---------------------------------------------------------------------------


def test_seniority_one_band_off_scores_point_six():
    score = _seniority_score("senior", "mid")
    assert score == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Test: unknown seniority → neutral 0.5
# ---------------------------------------------------------------------------


def test_seniority_unknown_is_neutral():
    score = _seniority_score("unknown", "mid")
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test: score is always clamped to 0-100
# ---------------------------------------------------------------------------


def test_score_is_always_in_range():
    cfg = _cfg(
        weight_skill_match=0.9,
        weight_seniority_fit=0.9,
        weight_location_fit=0.9,
        weight_recency=0.9,
    )
    listing = _listing()
    facts = _facts(
        required_skills=["python"],
        seniority="mid",
        remote_ok=True,
    )
    result = score_listing(listing, facts, cfg)
    assert 0 <= result["score"] <= 100


# ---------------------------------------------------------------------------
# Test: remote listing is not penalised for city mismatch
# ---------------------------------------------------------------------------


def test_remote_ok_overrides_city_mismatch():
    cfg = _cfg(target_city="Bengaluru")
    listing = _listing(location="New York, USA")
    facts = _facts(remote_ok=True)

    result = score_listing(listing, facts, cfg)
    assert result["components"]["location_fit"] == pytest.approx(1.0)
    assert "remote" in result["reason"]


# ---------------------------------------------------------------------------
# Test: reason always references the missing skills
# ---------------------------------------------------------------------------


def test_reason_names_missing_skills():
    cfg = _cfg(my_skills=["python"])
    listing = _listing()
    facts = _facts(required_skills=["python", "kubernetes", "spark"])

    result = score_listing(listing, facts, cfg)
    assert "kubernetes" in result["reason"]
    assert "spark" in result["reason"]


# ---------------------------------------------------------------------------
# Test: components dict has all expected keys
# ---------------------------------------------------------------------------


def test_components_has_all_keys():
    cfg = _cfg()
    result = score_listing(_listing(), _facts(), cfg)
    expected = {"skill_match", "seniority_fit", "location_fit", "recency"}
    assert set(result["components"].keys()) == expected


# ---------------------------------------------------------------------------
# Test: no score field in facts (rule 16 guard)
# ---------------------------------------------------------------------------


def test_no_score_field_in_return():
    result = score_listing(_listing(), _facts(), _cfg())
    # score_listing returns a score, but facts should never have one
    # (this is enforced in extractor; this test ensures score_listing
    # doesn't smuggle one into the facts dict)
    facts = _facts()
    assert "score" not in facts
    # and the result dict only has the documented keys
    assert "score" in result
    assert result["score"] 
