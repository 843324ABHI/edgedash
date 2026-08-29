"""
edgedash/agents/gap_analyzer.py — Fit-weighted gap analysis (steering rules 22-27).

NO LLM calls. NO network. Pure deterministic arithmetic.

Algorithm
---------
For each scored listing that has cached extraction facts:
  - Canonicalise required_skills via edgedash.skills.canonical + alias map.
  - Any canonical required skill NOT in my (canonicalised) skill set is a gap
    attributable to that listing.

For each canonical missing skill:
  opportunity_cost = Σ (listing.fit_score / 100)   ← rule 24 ranking key
                       for every listing that requires it and I lack it

  A listing scored 85 contributes 0.85; a listing scored 20 contributes 0.20.
  Raw frequency is NEVER the ranking key.

The top 10 gaps by opportunity_cost are written as a timestamped snapshot
(rule 25) and returned in AgentResult.notes.

Public API:
    GapAnalyzer  (implements Agent protocol)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash import storage
from edgedash.skills import canonical

logger = logging.getLogger(__name__)

_TOP_N = 10          # gaps reported per run
_EXAMPLE_IDS_CAP = 5  # listing IDs stored per gap (rule 26)
_LOW_CONFIDENCE_THRESHOLD = 3  # fewer listings → flagged (rule 27)


# ---------------------------------------------------------------------------
# Internal gap accumulator
# ---------------------------------------------------------------------------


@dataclass
class _GapAccum:
    """Mutable accumulator built while iterating over listings."""
    listings_blocked: int = 0
    opportunity_cost: float = 0.0   # Σ (score/100) — rule 24 ranking key
    score_sum: float = 0.0          # for mean_score
    top_score: int = 0
    example_ids: list[str] = field(default_factory=list)  # rule 26
    nice_to_have_count: int = 0     # tracked SEPARATELY, never merged


# ---------------------------------------------------------------------------
# opportunity_cost computation  (isolated — the user asked to see this first)
# ---------------------------------------------------------------------------


def _accumulate_gap(
    accum: _GapAccum,
    listing_id: str,
    fit_score: int,
) -> None:
    """
    Record one listing's contribution to a skill gap.

    opportunity_cost  +=  fit_score / 100

    This is the ranking key (rule 24).  A listing scored 85 contributes 0.85;
    a listing scored 20 contributes 0.20.  A gap blocking ten 85-point
    listings (cost 8.5) beats a gap blocking twenty 20-point listings (cost
    4.0) — raw frequency would invert that.

    Mutates *accum* in place.
    """
    accum.listings_blocked += 1
    accum.opportunity_cost += fit_score / 100.0   # ← the ranking key
    accum.score_sum += fit_score
    if fit_score > accum.top_score:
        accum.top_score = fit_score
    if len(accum.example_ids) < _EXAMPLE_IDS_CAP:
        accum.example_ids.append(listing_id)


# ---------------------------------------------------------------------------
# Storage helpers  (gap snapshot — rule 25 / 26)
# ---------------------------------------------------------------------------


def _migrate_gap_snapshots(db_path: str) -> None:
    """
    Idempotent migration: ensure skill_gap_snapshots table exists.
    Never touches the legacy skill_gaps table.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skill_gap_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id           TEXT    NOT NULL,
                computed_at      TEXT    NOT NULL,
                skill            TEXT    NOT NULL,
                rank             INTEGER NOT NULL,
                listings_blocked INTEGER NOT NULL,
                opportunity_cost REAL    NOT NULL,
                mean_score       REAL    NOT NULL,
                top_score        INTEGER NOT NULL,
                sample_n         INTEGER NOT NULL,
                low_confidence   INTEGER NOT NULL,  -- 0 | 1
                nice_to_have_count INTEGER NOT NULL,
                example_ids      TEXT    NOT NULL   -- JSON array of listing IDs
            )
        """)
        conn.commit()


def _write_snapshot(
    run_id: str,
    computed_at: str,
    ranked_gaps: list[dict[str, Any]],
    db_path: str,
) -> None:
    """
    Write the ranked gap list as a new snapshot row set.
    NEVER overwrites previous runs (rule 25).
    """
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO skill_gap_snapshots (
                run_id, computed_at, skill, rank,
                listings_blocked, opportunity_cost,
                mean_score, top_score, sample_n,
                low_confidence, nice_to_have_count, example_ids
            ) VALUES (
                :run_id, :computed_at, :skill, :rank,
                :listings_blocked, :opportunity_cost,
                :mean_score, :top_score, :sample_n,
                :low_confidence, :nice_to_have_count, :example_ids
            )
            """,
            ranked_gaps,
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _build_alias_map(config: Config) -> dict[str, str]:
    """
    Load the skill alias map from config.yaml via config.

    Config doesn't currently carry skill_aliases, so we read config.yaml
    directly via yaml.  Graceful fallback to empty dict if missing.
    """
    import yaml
    from pathlib import Path

    # walk up from cwd to find config.yaml — same heuristic as load_config
    for candidate in (Path("config.yaml"), Path("../config.yaml")):
        if candidate.is_file():
            with open(candidate, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            raw: dict = data.get("skill_aliases", {})
            aliases: dict[str, str] = {}
            for canon, alias_entry in raw.items():
                target = str(canon).strip().lower()
                if isinstance(alias_entry, list):
                    for a in alias_entry:
                        aliases[str(a).strip().lower()] = target
                else:
                    aliases[str(alias_entry).strip().lower()] = target
            return aliases
    logger.warning("gap_analyzer: config.yaml not found — alias map is empty")
    return {}


def _my_skills_canonical(
    my_skills: list[str],
    aliases: dict[str, str],
) -> frozenset[str]:
    """Return the canonicalised set of skills I already have."""
    return frozenset(canonical(s, aliases) for s in my_skills if s)


def _analyse(
    listings: list[dict[str, Any]],
    cache_rows: dict[str, dict[str, Any]],
    my_skills: frozenset[str],
    aliases: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """
    Core deterministic analysis.

    Returns (ranked_gap_dicts, analysed_count).
    ranked_gap_dicts are sorted by opportunity_cost descending, top _TOP_N.
    """
    gap_map: dict[str, _GapAccum] = defaultdict(_GapAccum)
    # Track nice_to_have separately per skill  (rule spec: never merged)
    nth_counts: dict[str, int] = defaultdict(int)

    analysed = 0

    for listing in listings:
        listing_id: str = listing.get("id", "")
        fit_score: int = listing.get("fit_score") or 0

        facts = cache_rows.get(listing_id)
        if facts is None:
            continue  # no cached facts — extractor hasn't run yet
        if not listing_id or listing.get("fit_score") is None:
            continue  # skip unscored

        analysed += 1

        required: list[str] = facts.get("required_skills") or []
        nice_to_have: list[str] = facts.get("nice_to_have") or []

        canon_required = [canonical(s, aliases) for s in required if s]
        canon_nth = [canonical(s, aliases) for s in nice_to_have if s]

        for skill in canon_required:
            if not skill:
                continue
            if skill not in my_skills:
                # This listing requires a skill I don't have → gap
                # The example_ids list keeps IDs in order of insertion
                # (already highest-first because we sorted listings by score).
                _accumulate_gap(gap_map[skill], listing_id, fit_score)

        for skill in canon_nth:
            if skill and skill not in my_skills:
                nth_counts[skill] += 1

    if not gap_map:
        return [], analysed

    # Sort by opportunity_cost descending (rule 24), then skill name for
    # stability.
    sorted_gaps = sorted(
        gap_map.items(),
        key=lambda kv: (-kv[1].opportunity_cost, kv[0]),
    )

    result: list[dict[str, Any]] = []
    for rank, (skill, accum) in enumerate(sorted_gaps[:_TOP_N], start=1):
        n = accum.listings_blocked
        mean_score = round(accum.score_sum / n, 1) if n else 0.0
        low_confidence = 1 if n < _LOW_CONFIDENCE_THRESHOLD else 0  # rule 27

        result.append({
            "skill": skill,
            "rank": rank,
            "listings_blocked": n,
            "opportunity_cost": round(accum.opportunity_cost, 3),
            "mean_score": mean_score,
            "top_score": accum.top_score,
            "sample_n": n,           # rule 27 — always reported
            "low_confidence": low_confidence,
            "nice_to_have_count": nth_counts.get(skill, 0),
            "example_ids": json.dumps(accum.example_ids),  # rule 26
        })

    return result, analysed


# ---------------------------------------------------------------------------
# GapAnalyzer agent
# ---------------------------------------------------------------------------


class GapAnalyzer:
    name: str = "GapAnalyzer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions = StopConditions(),
    ) -> AgentResult:
        # Honour max_seconds wall-clock budget (rule 29)
        deadline: float | None = (
            time.monotonic() + stop_conditions.max_seconds
            if stop_conditions.max_seconds is not None
            else None
        )

        _migrate_gap_snapshots(db_path)
        storage.migrate_score_columns(db_path)
        storage.migrate_extraction_cache(db_path)

        # --- load alias map (rule 23) ---
        aliases = _build_alias_map(config)
        my_skills = _my_skills_canonical(config.my_skills, aliases)

        # --- fetch scored listings, sorted by score DESC so example_ids are
        #     naturally the highest-scoring ones (rule 26) ---
        all_listings = _get_scored_listings_sorted(db_path)
        if not all_listings:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no scored listings yet — nothing to analyse",
            )

        # --- load extraction cache keyed by listing_id ---
        cache_rows = _load_cache_by_listing_id(all_listings, db_path)

        # --- deterministic analysis ---
        # Check deadline before the analysis pass (heavy for large datasets)
        if deadline is not None and time.monotonic() > deadline:
            return AgentResult(
                agent=self.name,
                status="partial",
                records_touched=0,
                notes="stopped: max_seconds exceeded before analysis",
            )

        ranked_gaps, analysed = _analyse(
            all_listings, cache_rows, my_skills, aliases
        )

        if not ranked_gaps:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=analysed,
                notes=(
                    f"no gaps found · {analysed} listings analysed "
                    f"· {len(all_listings)} total scored"
                ),
            )

        # --- write timestamped snapshot (rule 25) ---
        computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = computed_at  # one run = one timestamp; stable, unique, sortable

        snapshot_rows = [
            {**gap, "run_id": run_id, "computed_at": computed_at}
            for gap in ranked_gaps
        ]
        _write_snapshot(run_id, computed_at, snapshot_rows, db_path)

        # --- build AgentResult.notes (rule 27: include sample sizes) ---
        top = ranked_gaps[0]
        low_conf = sum(1 for g in ranked_gaps if g["low_confidence"])
        notes = (
            f"{len(ranked_gaps)} gaps · "
            f"top: {top['skill']} "
            f"({top['listings_blocked']} listings, "
            f"cost {top['opportunity_cost']:.1f}) · "
            f"{analysed} listings analysed"
            + (f" · {low_conf} low-confidence gaps (n<{_LOW_CONFIDENCE_THRESHOLD})" if low_conf else "")
        )

        logger.info(notes)
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(ranked_gaps),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Storage helpers (read side)
# ---------------------------------------------------------------------------


def _get_scored_listings_sorted(db_path: str) -> list[dict[str, Any]]:
    """
    Return all listings with a non-NULL fit_score, sorted by fit_score DESC.
    Highest-scoring listings first so example_ids naturally picks the best
    ones (rule 26).
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, fit_score, description FROM listings "
            "WHERE fit_score IS NOT NULL "
            "ORDER BY fit_score DESC"
        )
        return [dict(row) for row in cursor.fetchall()]


def _load_cache_by_listing_id(
    listings: list[dict[str, Any]],
    db_path: str,
) -> dict[str, dict[str, Any]]:
    """
    For each listing, look up its extraction_cache entry by hashing the
    description text (same key used in extractor.py).  Returns a dict
    mapping listing_id → facts dict.
    """
    import hashlib

    result: dict[str, dict[str, Any]] = {}
    if not listings:
        return result

    # Build hash → listing_id map
    hash_to_id: dict[str, str] = {}
    for listing in listings:
        desc = (listing.get("description") or "").strip()
        if desc:
            h = hashlib.sha256(desc.encode("utf-8")).hexdigest()
            hash_to_id[h] = listing["id"]

    if not hash_to_id:
        return result

    placeholders = ",".join("?" * len(hash_to_id))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT description_hash, extraction_json "
            f"FROM extraction_cache "
            f"WHERE description_hash IN ({placeholders})",
            list(hash_to_id.keys()),
        ).fetchall()

    for row in rows:
        listing_id = hash_to_id.get(row["description_hash"])
        if listing_id:
            try:
                result[listing_id] = json.loads(row["extraction_json"])
            except (json.JSONDecodeError, TypeError):
                pass

    return result
