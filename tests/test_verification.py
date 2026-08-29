"""
tests/test_verification.py — Unit tests for edgedash/verification.py.

Every test is deterministic: pure functions, no clock, no network, no DB.
Pattern: one passing case, one failing case, per check.
check_score_spread also has a trivial-pass (<5 scores) case.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from edgedash.config import Config
from edgedash.verification import (
    CheckResult,
    Verdict,
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> Config:
    """Build a Config with verification thresholds set to known values."""
    defaults = dict(
        verification_min_score_spread=10.0,
        verification_min_score_stdev=5.0,
        verification_max_empty_extraction_pct=20.0,
        verification_max_skills_per_listing=20,
        verification_min_gap_sample=3,
        verification_max_data_age_days=3.0,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _now() -> datetime:
    return datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# check_score_spread
# ---------------------------------------------------------------------------


class TestCheckScoreSpread:
    def test_passes_with_good_spread(self):
        scores = [20.0, 40.0, 60.0, 75.0, 90.0]
        result = check_score_spread(scores, _cfg())
        assert result.passed
        assert result.name == "score_spread"

    def test_fails_when_spread_too_narrow(self):
        # spread = 5, stdev ~1.6 — both below thresholds
        scores = [70.0, 72.0, 73.0, 74.0, 75.0]
        result = check_score_spread(scores, _cfg())
        assert not result.passed
        assert result.observed == 5.0          # max - min = 5
        assert result.threshold == 10.0
        assert "spread" in result.message.lower()
        assert "5.0" in result.message         # observed value in message

    def test_fails_when_stdev_too_low_but_spread_ok(self):
        # spread = 30 (passes spread check) but stdev is tiny
        # Craft: [50, 50, 50, 50, 80] — spread=30, stdev~13 — that would pass.
        # Instead use spread=20 but force narrow stdev by tweaking threshold.
        scores = [50.0, 51.0, 51.0, 51.0, 70.0]
        # spread = 20 (> threshold 10 — passes spread check)
        # stdev ≈ 8.7  — > threshold 5 — also passes. Raise stdev threshold.
        cfg = _cfg(verification_min_score_stdev=15.0)
        result = check_score_spread(scores, cfg)
        assert not result.passed
        assert "stdev" in result.message.lower()
        assert result.threshold == 15.0

    def test_trivial_pass_fewer_than_5_scores(self):
        result = check_score_spread([80.0, 81.0], _cfg())
        assert result.passed
        assert result.observed == 2
        assert "fewer than 5" in result.message

    def test_trivial_pass_exactly_4_scores(self):
        result = check_score_spread([80.0, 80.0, 80.0, 80.0], _cfg())
        assert result.passed
        assert result.observed == 4

    def test_trivial_pass_empty_scores(self):
        result = check_score_spread([], _cfg())
        assert result.passed


# ---------------------------------------------------------------------------
# check_extraction_sanity
# ---------------------------------------------------------------------------


class TestCheckExtractionSanity:
    def test_passes_with_healthy_extractions(self):
        facts = [
            {"required_skills": ["python", "sql"]},
            {"required_skills": ["java", "kafka"]},
            {"required_skills": ["react", "typescript", "graphql"]},
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed
        assert result.name == "extraction_sanity"

    def test_fails_when_too_many_empty_skill_lists(self):
        # 3 out of 4 empty = 75% > threshold 20%
        facts = [
            {"required_skills": []},
            {"required_skills": None},
            {"required_skills": []},
            {"required_skills": ["python"]},
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert not result.passed
        assert result.observed == 75.0
        assert result.threshold == 20.0
        assert "75.0%" in result.message

    def test_fails_when_bloated_skill_list(self):
        # 21 skills in one listing — over the max-20 threshold
        bloated_skills = [f"skill_{i}" for i in range(21)]
        facts = [
            {"required_skills": ["python", "sql"]},
            {"required_skills": bloated_skills},
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert not result.passed
        assert result.observed == 21
        assert result.threshold == 20
        assert "21" in result.message

    def test_passes_exactly_at_max_skills(self):
        exactly_20 = [f"skill_{i}" for i in range(20)]
        facts = [{"required_skills": exactly_20}]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed

    def test_passes_empty_facts_list(self):
        result = check_extraction_sanity([], _cfg())
        assert result.passed
        assert "No extractions" in result.message


# ---------------------------------------------------------------------------
# check_gap_sample_size
# ---------------------------------------------------------------------------


class TestCheckGapSampleSize:
    def test_passes_with_sufficient_sample(self):
        gaps = [
            {"skill": "kubernetes", "rank": 1, "listing_ids": ["a", "b", "c", "d"]},
            {"skill": "rust",       "rank": 2, "listing_ids": ["a"]},
        ]
        result = check_gap_sample_size(gaps, _cfg())
        assert result.passed
        assert result.observed == 4

    def test_fails_when_top_gap_backed_by_too_few_listings(self):
        gaps = [
            {"skill": "terraform", "rank": 1, "listing_ids": ["x"]},  # only 1
            {"skill": "go",        "rank": 2, "listing_ids": ["x", "y", "z", "w"]},
        ]
        result = check_gap_sample_size(gaps, _cfg())
        assert not result.passed
        assert result.observed == 1
        assert result.threshold == 3
        assert "terraform" in result.message
        assert "1" in result.message

    def test_passes_with_no_gaps(self):
        result = check_gap_sample_size([], _cfg())
        assert result.passed
        assert "No gaps" in result.message


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------


class TestCheckFreshness:
    def test_passes_when_data_is_fresh(self):
        fetch_time = _now() - timedelta(hours=12)
        result = check_freshness(fetch_time, _cfg(), now=_now())
        assert result.passed
        assert result.observed == pytest.approx(0.5, abs=0.01)

    def test_fails_when_data_is_stale(self):
        fetch_time = _now() - timedelta(days=5)
        result = check_freshness(fetch_time, _cfg(), now=_now())
        assert not result.passed
        assert result.observed == 5.0
        assert result.threshold == 3.0
        assert "5.0" in result.message

    def test_fails_when_no_timestamp(self):
        result = check_freshness(None, _cfg(), now=_now())
        assert not result.passed
        assert result.observed is None
        assert "never been fetched" in result.message

    def test_handles_naive_timestamps(self):
        """Naive datetimes (no tzinfo) must not raise."""
        naive_fetch = datetime(2024, 6, 15, 0, 0, 0)   # 12h before naive now
        naive_now   = datetime(2024, 6, 15, 12, 0, 0)
        result = check_freshness(naive_fetch, _cfg(), now=naive_now)
        assert result.passed
        assert result.observed == pytest.approx(0.5, abs=0.01)

    def test_exactly_at_boundary_passes(self):
        fetch_time = _now() - timedelta(days=3)   # exactly 3.0 days
        result = check_freshness(fetch_time, _cfg(), now=_now())
        # age == threshold: not strictly greater-than, so passes
        assert result.passed


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    """Integration: Verdict aggregation, not check logic (already tested above)."""

    def _healthy_args(self):
        scores     = [20.0, 40.0, 60.0, 75.0, 90.0]
        facts_list = [{"required_skills": ["python", "sql"]}]
        gaps       = [{"skill": "k8s", "rank": 1, "listing_ids": ["a", "b", "c"]}]
        fetch_at   = _now() - timedelta(hours=6)
        return scores, facts_list, gaps, fetch_at

    def test_passes_when_all_checks_pass(self):
        scores, facts, gaps, fetch_at = self._healthy_args()
        verdict = run_all_checks(scores, facts, gaps, fetch_at, _cfg(), now=_now())
        assert verdict.passed
        assert verdict.failed_checks == []
        assert "passed" in verdict.summary.lower()

    def test_fails_when_one_check_fails(self):
        scores, facts, gaps, _ = self._healthy_args()
        stale_fetch = _now() - timedelta(days=10)
        verdict = run_all_checks(scores, facts, gaps, stale_fetch, _cfg(), now=_now())
        assert not verdict.passed
        assert len(verdict.failed_checks) == 1
        assert verdict.failed_checks[0].name == "freshness"
        assert "freshness" in verdict.summary

    def test_failed_checks_lists_all_failures(self):
        # trigger both freshness failure and score-spread failure
        scores     = [70.0, 71.0, 72.0, 73.0, 74.0]   # spread=4 < 10
        facts      = [{"required_skills": ["python"]}]
        gaps       = [{"skill": "k8s", "rank": 1, "listing_ids": ["a", "b", "c"]}]
        stale      = _now() - timedelta(days=10)
        verdict = run_all_checks(scores, facts, gaps, stale, _cfg(), now=_now())
        assert not verdict.passed
        failed_names = {r.name for r in verdict.failed_checks}
        assert "score_spread" in failed_names
        assert "freshness" in failed_names
