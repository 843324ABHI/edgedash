"""
edgedash/state.py — Read system state from the database.

Rule 2  : all storage access goes through edgedash.storage — no sqlite3 here.
Testable: `now` is always a parameter, never datetime.now() inside.
Cheap   : only MAX(timestamp) and COUNT queries — no full table loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.config import Config


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemState:
    # Fetch staleness
    last_fetch_at: str | None       # ISO-8601 UTC string or None
    hours_since_fetch: float | None # None when never fetched

    # Scoring backlog
    unscored_count: int

    # Gap analysis freshness
    gaps_computed_at: str | None    # ISO-8601 UTC string or None
    gaps_stale: bool                # True when any score is newer than the gap snapshot

    # Last cycle outcome
    last_cycle_verdict: str | None  # cycle_log.status of the most recent row
    last_cycle_at: str | None       # cycle_log.finished_at of the most recent row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC string into an aware datetime, returning None on failure."""
    if ts is None:
        return None
    try:
        # Handle both "Z" suffix and "+00:00" offset
        cleaned = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _hours_between(earlier: datetime | None, later: datetime) -> float | None:
    """Return fractional hours from *earlier* to *later*, or None if *earlier* is None."""
    if earlier is None:
        return None
    delta = later - earlier
    return delta.total_seconds() / 3600.0


def _is_gaps_stale(gaps_computed_at: str | None, last_scored_at_ts: str | None) -> bool:
    """
    Return True when the gap snapshot is missing entirely or when any listing
    was scored after the most recent gap snapshot was computed.
    """
    if gaps_computed_at is None:
        return True  # no snapshot at all → definitely stale

    gap_dt = _parse_iso(gaps_computed_at)
    scored_dt = _parse_iso(last_scored_at_ts)

    if gap_dt is None:
        return True  # unparseable → treat as stale

    if scored_dt is None:
        return False  # nothing scored yet → gap is as fresh as it can be

    return scored_dt > gap_dt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_state(config: Config, now: datetime) -> SystemState:
    """
    Read the current system state from the database and return a SystemState.

    Parameters
    ----------
    config : Config
        Loaded project configuration (supplies db_path).
    now : datetime
        The reference instant for all age calculations.
        Callers must pass an aware UTC datetime.
        Never call datetime.now() inside this function — doing so would make
        the function untestable without mocking.
    """
    db = config.db_path

    last_fetch_ts: str | None = storage.last_fetch_time(db)
    last_fetch_dt = _parse_iso(last_fetch_ts)
    hours_since = _hours_between(last_fetch_dt, now)

    unscored: int = storage.count_unscored(db)

    gaps_ts: str | None = storage.last_gap_computed_at(db)
    scored_ts: str | None = storage.last_scored_at(db)
    stale: bool = _is_gaps_stale(gaps_ts, scored_ts)

    cycle_row: dict | None = storage.last_cycle_row(db)
    last_verdict: str | None = cycle_row["status"] if cycle_row else None
    last_cycle_at: str | None = cycle_row["finished_at"] if cycle_row else None

    return SystemState(
        last_fetch_at=last_fetch_ts,
        hours_since_fetch=hours_since,
        unscored_count=unscored,
        gaps_computed_at=gaps_ts,
        gaps_stale=stale,
        last_cycle_verdict=last_verdict,
        last_cycle_at=last_cycle_at,
    )
