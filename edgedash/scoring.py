"""
edgedash/scoring.py — Deterministic scoring arithmetic (steering rules 16-19).

NO model calls. NO network. NO imports from llm.py.
All functions are pure: same inputs → same outputs, every time.

Public API:
    score_listing(listing, facts, config) -> dict
    build_reason(components, facts, config) -> str
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Seniority band ordering  (rule 16 – pure arithmetic, no model)
# ---------------------------------------------------------------------------

_SENIORITY_ORDER: list[str] = ["junior", "mid", "senior", "lead"]

# Scores for distance between seniority bands: 0 apart, 1, 2, 3+
_SENIORITY_BAND_SCORES: list[float] = [1.0, 0.6, 0.25, 0.0]


def _seniority_score(facts_seniority: str, target_seniority: str) -> float:
    """
    Return 0.0-1.0 based on how many ordered bands apart the two levels are.

    "unknown" (from facts) or missing target → 0.5 (neutral, not penalised).
    """
    if not target_seniority:
        return 0.5

    fs = facts_seniority.strip().lower()
    ts = target_seniority.strip().lower()

    if fs == "unknown" or fs not in _SENIORITY_ORDER:
        return 0.5  # listing didn't state it; don't penalise

    if ts not in _SENIORITY_ORDER:
        return 0.5  # config not set meaningfully

    distance = abs(_SENIORITY_ORDER.index(fs) - _SENIORITY_ORDER.index(ts))
    idx = min(distance, len(_SENIORITY_BAND_SCORES) - 1)
    return _SENIORITY_BAND_SCORES[idx]


# ---------------------------------------------------------------------------
# Skill match  (rule 16 – fraction, no model)
# ---------------------------------------------------------------------------


def _skill_match_score(
    required: list[str],
    nice_to_have: list[str],
    my_skills: list[str],
) -> tuple[float, list[str]]:
    """
    Return (score 0.0-1.0, missing_required_skills).

    - Empty required_skills → 0.5 (neutral, explicit zero-division guard).
    - nice_to_have counts at 1/3 the weight of required.
    - All comparisons case-insensitive.
    """
    my = {s.strip().lower() for s in my_skills}

    req_lower = [s.strip().lower() for s in required]
    nth_lower = [s.strip().lower() for s in nice_to_have]

    missing: list[str] = [s for s in req_lower if s not in my]

    if not req_lower and not nth_lower:
        return 0.5, []

    # Weighted numerator/denominator
    req_weight = 1.0
    nth_weight = 1.0 / 3.0

    numerator = 0.0
    denominator = 0.0

    for skill in req_lower:
        denominator += req_weight
        if skill in my:
            numerator += req_weight

    for skill in nth_lower:
        denominator += nth_weight
        if skill in my:
            numerator += nth_weight

    # Guard: if somehow denominator is 0 (only possible if both lists empty,
    # caught above, but belt-and-suspenders)
    if denominator == 0.0:
        return 0.5, []

    return numerator / denominator, missing


# ---------------------------------------------------------------------------
# Location fit  (rule 16 – pure string/bool logic)
# ---------------------------------------------------------------------------


def _location_score(
    remote_ok: bool | None,
    listing_location: str | None,
    target_city: str,
) -> float:
    """
    Return 0.0-1.0 based on remote flag and city match.

    remote_ok True → 1.0 (works anywhere)
    city match → 1.0
    remote_ok None / unknown location → 0.5
    clearly elsewhere and not remote → 0.1
    """
    if remote_ok is True:
        return 1.0

    location = (listing_location or "").strip().lower()
    city = target_city.strip().lower()

    if city and location and city in location:
        return 1.0

    if remote_ok is None or not location:
        return 0.5

    # remote_ok is False and location doesn't match
    return 0.1


# ---------------------------------------------------------------------------
# Recency  (rule 16 – time arithmetic only)
# ---------------------------------------------------------------------------


def _recency_score(posted_at: str | None) -> tuple[float, int | None]:
    """
    Return (score 0.0-1.0, age_days | None).

    Today → 1.0, decaying linearly to 0.0 at 30 days.
    None or unparseable → 0.5, age_days = None.
    """
    if not posted_at:
        return 0.5, None

    formats = (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    )
    parsed: datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(posted_at, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue

    if parsed is None:
        return 0.5, None

    now = datetime.now(timezone.utc)
    age_days = max(0, (now - parsed).days)
    score = max(0.0, 1.0 - age_days / 30.0)
    return score, age_days


# ---------------------------------------------------------------------------
# Top-level scorer  (rule 16)
# ---------------------------------------------------------------------------


def score_listing(
    listing: dict[str, Any],
    facts: dict[str, Any],
    config: Config,
) -> dict[str, Any]:
    """
    Compute a deterministic fit score for one listing.

    Parameters
    ----------
    listing : normalised listing dict (needs 'location', 'posted_at').
    facts   : extraction dict from extractor.extract().
    config  : Config — carries my_skills, target_city, scoring weights.

    Returns
    -------
    {
        "score":      int 0-100,
        "reason":     str (human-readable, assembled from numbers),
        "components": {
            "skill_match":   float 0-1,
            "seniority_fit": float 0-1,
            "location_fit":  float 0-1,
            "recency":       float 0-1,
        },
        # internal fields used by build_reason / Gap Analyzer:
        "_missing_skills": list[str],
        "_age_days":       int | None,
    }

    No model calls. No network. Pure arithmetic.
    """
    # --- weights (read from config, default constants below) ---
    w_skill   = float(getattr(config, "weight_skill_match",   0.45))
    w_senior  = float(getattr(config, "weight_seniority_fit", 0.25))
    w_loc     = float(getattr(config, "weight_location_fit",  0.15))
    w_recency = float(getattr(config, "weight_recency",       0.15))

    # --- components ---
    skill_score, missing = _skill_match_score(
        required=facts.get("required_skills") or [],
        nice_to_have=facts.get("nice_to_have") or [],
        my_skills=config.my_skills,
    )

    senior_score = _seniority_score(
        facts_seniority=facts.get("seniority") or "unknown",
        target_seniority=getattr(config, "target_seniority", ""),
    )

    loc_score = _location_score(
        remote_ok=facts.get("remote_ok"),
        listing_location=listing.get("location"),
        target_city=config.target_city,
    )

    recency_score, age_days = _recency_score(listing.get("posted_at"))

    components: dict[str, float] = {
        "skill_match":   round(skill_score,   4),
        "seniority_fit": round(senior_score,  4),
        "location_fit":  round(loc_score,     4),
        "recency":       round(recency_score, 4),
    }

    raw = (
        w_skill   * skill_score
        + w_senior  * senior_score
        + w_loc     * loc_score
        + w_recency * recency_score
    )
    score = max(0, min(100, round(raw * 100)))

    reason = build_reason(components, facts, config, missing=missing, age_days=age_days)

    return {
        "score":           score,
        "reason":          reason,
        "components":      components,
        "_missing_skills": missing,
        "_age_days":       age_days,
    }


# ---------------------------------------------------------------------------
# Reason builder  (rule 19 – assembled from numbers, never from the model)
# ---------------------------------------------------------------------------


def build_reason(
    components: dict[str, float],
    facts: dict[str, Any],
    config: Config,
    *,
    missing: list[str] | None = None,
    age_days: int | None = None,
) -> str:
    """
    Produce a compact human-readable reason string from score components.

    Example:
        "4/6 required skills · seniority fits · remote · posted 2d ago · gap: kubernetes, spark"

    Rule 19: every token comes from the numbers and facts, never from the model.
    """
    parts: list[str] = []

    # --- skill match ---
    required: list[str] = [s.strip().lower() for s in (facts.get("required_skills") or [])]
    my: set[str] = {s.strip().lower() for s in config.my_skills}
    matched = len(required) - len([s for s in required if s not in my])

    if required:
        parts.append(f"{matched}/{len(required)} required skills")
    else:
        parts.append("no required skills listed")

    # --- seniority ---
    seniority = (facts.get("seniority") or "unknown").lower()
    target_s  = getattr(config, "target_seniority", "").strip().lower()
    senior_val = components.get("seniority_fit", 0.5)
    if seniority == "unknown":
        parts.append("seniority unknown")
    elif not target_s:
        parts.append(f"seniority: {seniority}")
    elif senior_val >= 0.9:
        parts.append("seniority fits")
    elif senior_val >= 0.5:
        parts.append(f"seniority close ({seniority} vs {target_s})")
    else:
        parts.append(f"seniority mismatch ({seniority} vs {target_s})")

    # --- location ---
    remote_ok = facts.get("remote_ok")
    loc_val   = components.get("location_fit", 0.5)
    if remote_ok is True:
        parts.append("remote")
    elif loc_val >= 0.9:
        parts.append(f"in {config.target_city}")
    elif loc_val >= 0.4:
        parts.append("location unclear")
    else:
        parts.append("not local / not remote")

    # --- recency ---
    if age_days is None:
        parts.append("posted date unknown")
    elif age_days == 0:
        parts.append("posted today")
    elif age_days == 1:
        parts.append("posted 1d ago")
    else:
        parts.append(f"posted {age_days}d ago")

    # --- skill gaps (most useful signal for Gap Analyzer) ---
    gaps = missing if missing is not None else []
    if gaps:
        gap_str = ", ".join(gaps[:5])  # cap at 5 for readability
        if len(gaps) > 5:
            gap_str += f" (+{len(gaps) - 5} more)"
        parts.append(f"gap: {gap_str}")

    return " · ".join(parts)
