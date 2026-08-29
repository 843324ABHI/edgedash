"""
tests/test_planning.py — Unit tests for edgedash/planning.py.

build_plan is a pure function: no mocks, no DB, no network, no datetime.now().
All tests are deterministic.

Scenarios covered:
  1. everything_stale   — all three agents RUN
  2. nothing_to_do      — all three agents SKIP
  3. only_unscored      — only Scorer RUNs
  4. gaps_stale_no_unscored — Scorer SKIPs, GapAnalyzer RUNs
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from edgedash.config import Config
from edgedash.state import SystemState
from edgedash.planning import build_plan, Plan, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> Config:
    """Build a Config with sensible orchestration defaults for testing."""
    defaults = dict(
        fetch_interval_hours=6.0,
        fetch_max_pages=5,
        fetch_max_listings=200,
        llm_batch_size=25,
        score_max_seconds=300,
        analyse_max_seconds=120,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _now() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(
    *,
    hours_ago_fetch: float | None = None,
    unscored: int = 0,
    gaps_computed_hours_ago: float | None = None,
    gaps_stale: bool = False,
    last_verdict: str | None = "ok",
) -> SystemState:
    """
    Construct a SystemState relative to _now() so test intent is readable.

    hours_ago_fetch=None  → last_fetch_at=None (never fetched)
    gaps_computed_hours_ago=None → gaps_computed_at=None (never run)
    """
    now = _now()

    if hours_ago_fetch is None:
        last_fetch_at = None
        hours_since_fetch = None
    else:
        fetch_dt = now - timedelta(hours=hours_ago_fetch)
        last_fetch_at = _iso(fetch_dt)
        hours_since_fetch = hours_ago_fetch

    if gaps_computed_hours_ago is None:
        gaps_computed_at = None
    else:
        gap_dt = now - timedelta(hours=gaps_computed_hours_ago)
        gaps_computed_at = _iso(gap_dt)

    return SystemState(
        last_fetch_at=last_fetch_at,
        hours_since_fetch=hours_since_fetch,
        unscored_count=unscored,
        gaps_computed_at=gaps_computed_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=last_verdict,
        last_cycle_at=_iso(now - timedelta(hours=1)) if last_verdict else None,
    )


# ---------------------------------------------------------------------------
# 1. Everything stale — all three agents RUN
# ---------------------------------------------------------------------------


def test_everything_stale_all_agents_run():
    """
    Last fetch was 8 h ago (> 6 h threshold).
    There are 41 unscored listings.
    The gap snapshot is older than the most recent score.
    Expected: Fetcher=RUN, Scorer=RUN, GapAnalyzer=RUN
    """
    state = _state(hours_ago_fetch=8.0, unscored=41, gaps_computed_hours_ago=10.0, gaps_stale=True)
    plan = build_plan(state, _cfg())

    assert len(plan.tasks) == 3
    by_name = {t.agent_name: t for t in plan.tasks}

    assert by_name["Fetcher"].should_run is True,    "Fetcher must RUN when stale"
    assert by_name["Scorer"].should_run is True,     "Scorer must RUN when unscored > 0"
    assert by_name["GapAnalyzer"].should_run is True, "GapAnalyzer must RUN when gaps stale"

    # Reasons must quote the deciding state value
    assert "8.0" in by_name["Fetcher"].reason
    assert "41" in by_name["Scorer"].reason
    assert "gaps_stale=True" in by_name["GapAnalyzer"].reason


# ---------------------------------------------------------------------------
# 2. Nothing to do — all three agents SKIP
# ---------------------------------------------------------------------------


def test_nothing_to_do_all_agents_skip():
    """
    Last fetch was 2 h ago (< 6 h threshold).
    No unscored listings.
    Gap snapshot is fresh (not stale).
    Expected: all three SKIP with explanatory reasons.
    """
    state = _state(hours_ago_fetch=2.0, unscored=0, gaps_computed_hours_ago=1.0, gaps_stale=False)
    plan = build_plan(state, _cfg())

    assert len(plan.tasks) == 3
    assert plan.runnable == [], "No agents should run when system is fully up to date"
    assert len(plan.skipped) == 3

    by_name = {t.agent_name: t for t in plan.tasks}
    assert by_name["Fetcher"].should_run is False
    assert by_name["Scorer"].should_run is False
    assert by_name["GapAnalyzer"].should_run is False

    # All skip reasons must start with "skipped:"
    for task in plan.tasks:
        assert task.reason.startswith("skipped:"), (
            f"{task.agent_name} reason should start with 'skipped:' but got: {task.reason!r}"
        )


# ---------------------------------------------------------------------------
# 3. Only unscored listings — only Scorer runs
# ---------------------------------------------------------------------------


def test_only_unscored_listings():
    """
    Last fetch was 1 h ago (fresh), 15 unscored rows, gap snapshot fresh.
    Expected: Fetcher=SKIP, Scorer=RUN, GapAnalyzer=SKIP
    """
    state = _state(hours_ago_fetch=1.0, unscored=15, gaps_computed_hours_ago=0.5, gaps_stale=False)
    plan = build_plan(state, _cfg())

    by_name = {t.agent_name: t for t in plan.tasks}

    assert by_name["Fetcher"].should_run is False
    assert by_name["Scorer"].should_run is True
    assert by_name["GapAnalyzer"].should_run is False

    assert "15" in by_name["Scorer"].reason
    assert len(plan.runnable) == 1
    assert plan.runnable[0].agent_name == "Scorer"


# ---------------------------------------------------------------------------
# 4. Gaps stale, nothing unscored — only GapAnalyzer runs
# ---------------------------------------------------------------------------


def test_gaps_stale_nothing_unscored():
    """
    Fetch is fresh, no unscored rows, but gap snapshot is stale.
    Expected: Fetcher=SKIP, Scorer=SKIP, GapAnalyzer=RUN
    """
    state = _state(hours_ago_fetch=1.0, unscored=0, gaps_computed_hours_ago=5.0, gaps_stale=True)
    plan = build_plan(state, _cfg())

    by_name = {t.agent_name: t for t in plan.tasks}

    assert by_name["Fetcher"].should_run is False
    assert by_name["Scorer"].should_run is False
    assert by_name["GapAnalyzer"].should_run is True

    assert "gaps_stale=True" in by_name["GapAnalyzer"].reason
    assert len(plan.runnable) == 1
    assert plan.runnable[0].agent_name == "GapAnalyzer"


# ---------------------------------------------------------------------------
# 5. Never fetched — Fetcher always runs
# ---------------------------------------------------------------------------


def test_never_fetched_fetcher_runs():
    """hours_since_fetch=None means never fetched — Fetcher must always run."""
    state = _state(hours_ago_fetch=None, unscored=0, gaps_computed_hours_ago=None)
    plan = build_plan(state, _cfg())

    by_name = {t.agent_name: t for t in plan.tasks}
    assert by_name["Fetcher"].should_run is True
    assert "never" in by_name["Fetcher"].reason


# ---------------------------------------------------------------------------
# 6. Stop conditions always come from config — never None for bounded agents
# ---------------------------------------------------------------------------


def test_stop_conditions_come_from_config():
    """Verify stop conditions are wired from config, not hardcoded."""
    cfg = _cfg(
        fetch_max_pages=3,
        fetch_max_listings=99,
        llm_batch_size=10,
        score_max_seconds=60,
        analyse_max_seconds=45,
    )
    state = _state(hours_ago_fetch=10.0, unscored=5, gaps_computed_hours_ago=None)
    plan = build_plan(state, cfg)

    by_name = {t.agent_name: t for t in plan.tasks}

    assert by_name["Fetcher"].stop_conditions.max_pages == 3
    assert by_name["Fetcher"].stop_conditions.max_items == 99
    assert by_name["Scorer"].stop_conditions.max_items == 10
    assert by_name["Scorer"].stop_conditions.max_seconds == 60
    assert by_name["GapAnalyzer"].stop_conditions.max_seconds == 45


# ---------------------------------------------------------------------------
# 7. Plan.render() produces correct shape for "nothing to do"
# ---------------------------------------------------------------------------


def test_render_nothing_to_do_shape():
    """render() must produce one line per agent and mark each as SKIP."""
    state = _state(hours_ago_fetch=2.0, unscored=0, gaps_computed_hours_ago=1.0, gaps_stale=False)
    plan = build_plan(state, _cfg())
    rendered = plan.render()

    lines = [l for l in rendered.splitlines() if l.strip()]
    assert len(lines) == 3, "Exactly one line per agent"

    for line in lines:
        assert "[SKIP]" in line, f"Expected [SKIP] in: {line!r}"

    assert "Fetcher" in rendered
    assert "Scorer" in rendered
    assert "GapAnalyzer" in rendered


# ---------------------------------------------------------------------------
# 8. Agent order is fixed: Fetcher → Scorer → GapAnalyzer
# ---------------------------------------------------------------------------


def test_agent_order_is_fixed():
    """Tasks must always be in the canonical pipeline order."""
    state = _state(hours_ago_fetch=10.0, unscored=5, gaps_computed_hours_ago=None)
    plan = build_plan(state, _cfg())

    names = [t.agent_name for t in plan.tasks]
    assert names == ["Fetcher", "Scorer", "GapAnalyzer"]
