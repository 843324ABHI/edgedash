"""
edgedash/verification.py — Plausibility checks for agent output.

Design rules (steering §Verification):
- Every check is a pure function: no LLM, no clock, no network, no DB.
- Thresholds come from Config (config.yaml), never hardcoded here.
- A CheckResult carries the observed value so cycle_log can record it
  verbatim — never just "failed" (rule 37).
- Verifier returns verdicts; the Orchestrator decides what to do (rule 34).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from edgedash.config import Config


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Verdict for a single plausibility check."""

    name: str
    passed: bool
    observed: object       # the raw value that was tested — logged verbatim
    threshold: object      # the threshold it was tested against
    message: str           # human-readable explanation, always non-empty


@dataclass(frozen=True)
class Verdict:
    """Aggregate result returned by run_all_checks."""

    passed: bool
    failed_checks: list[CheckResult]
    summary: str


# ---------------------------------------------------------------------------
# 1. check_score_spread
# ---------------------------------------------------------------------------


def check_score_spread(scores: Sequence[float], config: Config) -> CheckResult:
    """
    Assert that the score distribution is not artificially compressed.

    Catches the inflation failure mode (steering rule 35): a run where
    every job scores 70-80 is suspicious — spread should be visible.

    Passes trivially when fewer than 5 scores (not enough data to judge).
    """
    name = "score_spread"

    if len(scores) < 5:
        return CheckResult(
            name=name,
            passed=True,
            observed=len(scores),
            threshold=5,
            message=(
                f"Only {len(scores)} score(s) — fewer than 5. "
                "Spread check skipped; too few data points to judge distribution."
            ),
        )

    spread = max(scores) - min(scores)
    stdev = statistics.stdev(scores)

    if spread < config.verification_min_score_spread:
        return CheckResult(
            name=name,
            passed=False,
            observed=round(spread, 2),
            threshold=config.verification_min_score_spread,
            message=(
                f"Score spread {spread:.2f} is below the minimum "
                f"{config.verification_min_score_spread}. "
                "Scores are suspiciously compressed — possible scoring inflation."
            ),
        )

    if stdev < config.verification_min_score_stdev:
        return CheckResult(
            name=name,
            passed=False,
            observed=round(stdev, 2),
            threshold=config.verification_min_score_stdev,
            message=(
                f"Score stdev {stdev:.2f} is below the minimum "
                f"{config.verification_min_score_stdev}. "
                "Distribution is too narrow — possible scoring inflation."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed={"spread": round(spread, 2), "stdev": round(stdev, 2)},
        threshold={
            "min_spread": config.verification_min_score_spread,
            "min_stdev": config.verification_min_score_stdev,
        },
        message=(
            f"Score spread {spread:.2f} and stdev {stdev:.2f} "
            "are within acceptable bounds."
        ),
    )


# ---------------------------------------------------------------------------
# 2. check_extraction_sanity
# ---------------------------------------------------------------------------


def check_extraction_sanity(
    facts_list: Sequence[dict], config: Config
) -> CheckResult:
    """
    Assert that the extractor produced plausible output.

    Catches two failure modes (steering rule 35):
    - Too many empty required_skills lists -> broken extractor.
    - A listing with an absurd number of skills -> model returned a sentence.
    """
    name = "extraction_sanity"

    if not facts_list:
        return CheckResult(
            name=name,
            passed=True,
            observed=0,
            threshold=None,
            message="No extractions to check.",
        )

    empty_count = sum(
        1 for f in facts_list if not f.get("required_skills")
    )
    empty_pct = (empty_count / len(facts_list)) * 100

    if empty_pct > config.verification_max_empty_extraction_pct:
        return CheckResult(
            name=name,
            passed=False,
            observed=round(empty_pct, 1),
            threshold=config.verification_max_empty_extraction_pct,
            message=(
                f"{empty_pct:.1f}% of listings have an empty required_skills "
                f"list (threshold: {config.verification_max_empty_extraction_pct}%). "
                "Possible broken extractor or model returning empty JSON."
            ),
        )

    bloated = [
        i
        for i, f in enumerate(facts_list)
        if len(f.get("required_skills") or []) > config.verification_max_skills_per_listing
    ]

    if bloated:
        worst = max(
            len(facts_list[i].get("required_skills") or []) for i in bloated
        )
        return CheckResult(
            name=name,
            passed=False,
            observed=worst,
            threshold=config.verification_max_skills_per_listing,
            message=(
                f"{len(bloated)} listing(s) have more than "
                f"{config.verification_max_skills_per_listing} required skills "
                f"(worst: {worst}). "
                "Possible model hallucination — a whole sentence parsed as skills."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed={
            "total": len(facts_list),
            "empty_pct": round(empty_pct, 1),
        },
        threshold={
            "max_empty_pct": config.verification_max_empty_extraction_pct,
            "max_skills": config.verification_max_skills_per_listing,
        },
        message=(
            f"Extraction looks sane: {empty_pct:.1f}% empty, "
            "no bloated skill lists."
        ),
    )


# ---------------------------------------------------------------------------
# 3. check_gap_sample_size
# ---------------------------------------------------------------------------


def check_gap_sample_size(gaps: Sequence[dict], config: Config) -> CheckResult:
    """
    Assert the top-ranked gap has enough backing listings.

    Catches "ranking a rumour" (steering rule 35): a gap seen in one
    listing should not appear at rank 1.

    Each gap dict must have a 'listing_ids' key (list of IDs it came from)
    and a 'rank' key (lower = higher priority, 1-based).
    """
    name = "gap_sample_size"

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed=0,
            threshold=config.verification_min_gap_sample,
            message="No gaps to check.",
        )

    top_gap = min(gaps, key=lambda g: g.get("rank", float("inf")))
    sample = len(top_gap.get("listing_ids") or [])

    if sample < config.verification_min_gap_sample:
        return CheckResult(
            name=name,
            passed=False,
            observed=sample,
            threshold=config.verification_min_gap_sample,
            message=(
                f"Top-ranked gap '{top_gap.get('skill', '?')}' is backed by "
                f"only {sample} listing(s) "
                f"(minimum: {config.verification_min_gap_sample}). "
                "Gap ranking may be a statistical rumour."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=sample,
        threshold=config.verification_min_gap_sample,
        message=(
            f"Top-ranked gap has {sample} backing listing(s) — "
            "sufficient for ranking."
        ),
    )


# ---------------------------------------------------------------------------
# 4. check_freshness
# ---------------------------------------------------------------------------


def check_freshness(
    latest_fetch_at: datetime | None,
    config: Config,
    now: datetime,
) -> CheckResult:
    """
    Assert the most recent fetched data is not stale.

    `now` is a parameter — never datetime.now() inside this function —
    so every call is deterministic and fully testable (steering rule 35).

    Catches the "dashboard shows week-old data" failure mode.
    """
    name = "freshness"

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed=None,
            threshold=config.verification_max_data_age_days,
            message=(
                "No fetch timestamp found. "
                "Data has never been fetched or the record is missing."
            ),
        )

    # Normalise both timestamps to UTC-aware for safe subtraction.
    if latest_fetch_at.tzinfo is None:
        latest_fetch_at = latest_fetch_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_days = (now - latest_fetch_at).total_seconds() / 86_400

    if age_days > config.verification_max_data_age_days:
        return CheckResult(
            name=name,
            passed=False,
            observed=round(age_days, 2),
            threshold=config.verification_max_data_age_days,
            message=(
                f"Newest listing is {age_days:.2f} day(s) old "
                f"(maximum allowed: {config.verification_max_data_age_days}). "
                "Data is stale — fetcher may be failing silently."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=round(age_days, 2),
        threshold=config.verification_max_data_age_days,
        message=f"Data is fresh: {age_days:.2f} day(s) old.",
    )


# ---------------------------------------------------------------------------
# 5. run_all_checks
# ---------------------------------------------------------------------------


def run_all_checks(
    scores: Sequence[float],
    facts_list: Sequence[dict],
    gaps: Sequence[dict],
    latest_fetch_at: datetime | None,
    config: Config,
    now: datetime,
) -> Verdict:
    """
    Run every plausibility check and return a single Verdict.

    Passes only if ALL checks pass (steering rule 38).
    The Orchestrator, not this function, decides what to do with failures.
    """
    results = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    all_passed = len(failed) == 0

    if all_passed:
        summary = "All verification checks passed."
    else:
        failed_names = ", ".join(r.name for r in failed)
        summary = (
            f"{len(failed)} check(s) failed: {failed_names}. "
            "Cycle must not overwrite last known-good data."
        )

    return Verdict(passed=all_passed, failed_checks=failed, summary=summary)
