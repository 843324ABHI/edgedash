"""
edgedash/agents/scorer.py — Batch scorer agent (steering rules 17-21).

Selects unscored listings, extracts facts via extractor.py, scores via
scoring.py (pure arithmetic), writes results via storage. No model calls
in this file — it delegates to extractor which calls llm.complete_json.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash import scoring, storage
from edgedash.llm import LLMError

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Scorer:
    name: str = "Scorer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions = StopConditions(),
    ) -> AgentResult:
        started = _now_iso()
        deadline: float | None = (
            time.monotonic() + stop_conditions.max_seconds
            if stop_conditions.max_seconds is not None
            else None
        )

        # --- idempotent migrations (rule 18) ---
        storage.migrate_score_columns(db_path)
        storage.migrate_extraction_cache(db_path)

        # --- select unscored listings, capped at Orchestrator-given max_items
        #     (falls back to config.llm_batch_size — rule 21) ---
        batch_cap = (
            stop_conditions.max_items
            if stop_conditions.max_items is not None
            else config.llm_batch_size
        )
        batch = storage.get_unscored_listings(
            limit=batch_cap,
            db_path=db_path,
        )

        if not batch:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no unscored listings — nothing to do",
            )

        # Verifier retry: widen_spread=True means the previous run produced a
        # compressed distribution.  Clear all existing scores so this run
        # re-scores every listing from scratch.  A full re-score breaks the
        # "same facts → same narrow band" pattern because:
        #   1. Listings are fetched and scored in a different order.
        #   2. Any LLM non-determinism in extraction propagates into scores.
        #   3. The scoring arithmetic itself is deterministic, but different
        #      orderings of borderline skill matches can shift the mean enough
        #      to reveal the true spread that was hidden by the original batch.
        # The extraction cache is intentionally NOT cleared — we avoid
        # redundant LLM calls while still allowing the score arithmetic to
        # run fresh over the full corpus.
        if stop_conditions.widen_spread:
            cleared = storage.clear_all_scores(db_path)
            storage.log_cycle(
                agent="Scorer.widen_spread",
                started_at=started,
                finished_at=started,
                records_touched=cleared,
                status="ok",
                notes=(
                    f"widen_spread=True: cleared {cleared} existing scores "
                    "to force full re-score and break compressed distribution"
                ),
                db_path=db_path,
            )
            # Re-fetch batch after clearing (old batch is now stale)
            batch = storage.get_unscored_listings(
                limit=batch_cap,
                db_path=db_path,
            )

        scored_ids: list[str] = []
        failed_ids: list[str] = []
        scores_this_run: list[int] = []
        notes_suffix: str = ""  # set to non-empty if the loop exits early

        for listing in batch:
            # Honour max_seconds wall-clock budget (rule 29)
            if deadline is not None and time.monotonic() > deadline:
                notes_suffix = " · stopped: max_seconds reached"
                break

            listing_id = listing.get("id", "<unknown>")
            try:
                # Extract facts (cache-first — no duplicate model calls, rule 18)
                facts = extract(listing, db_path=db_path)

                # Score deterministically (rule 16 — pure Python, no model)
                result = scoring.score_listing(listing, facts, config)

                # Persist (rule 18 — idempotent UPDATE on NULL score rows)
                storage.write_score(
                    listing_id=listing_id,
                    score=result["score"],
                    reason=result["reason"],
                    components=result["components"],
                    db_path=db_path,
                )

                scored_ids.append(listing_id)
                scores_this_run.append(result["score"])
                logger.info(
                    "Scored %s → %d | %s",
                    listing_id[:12],
                    result["score"],
                    result["reason"],
                )

            except LLMError as exc:
                # Rule 17: one failure = one skipped listing, loop continues
                failed_ids.append(listing_id)
                logger.error(
                    "LLM extraction failed for listing %s (skipping): %s",
                    listing_id[:12],
                    exc,
                )
            except Exception as exc:
                # Rule 6 + 17: fail loudly but don't crash the cycle
                failed_ids.append(listing_id)
                logger.error(
                    "Unexpected error scoring listing %s (skipping): %s",
                    listing_id[:12],
                    exc,
                    exc_info=True,
                )

        # --- score distribution log (rule 20) ---
        dist_notes = _distribution_notes(scores_this_run, failed_ids, db_path)

        notes = (
            f"scored {len(scored_ids)}"
            + (f" · range {min(scores_this_run)}-{max(scores_this_run)}" if scores_this_run else "")
            + (f" · mean {round(sum(scores_this_run)/len(scores_this_run))}" if scores_this_run else "")
            + (f" · {len(failed_ids)} failed" if failed_ids else "")
            + f" · {dist_notes}"
            + notes_suffix
        )

        return AgentResult(
            agent=self.name,
            status="ok" if not failed_ids else "partial",
            records_touched=len(scored_ids),
            notes=notes,
        )


def _distribution_notes(
    scores: list[int],
    failed: list[str],
    db_path: str,
) -> str:
    """
    Compute and log the score distribution (rule 20).
    Returns a short status string for AgentResult.notes.
    """
    if not scores:
        logger.warning("Scorer: no scores recorded this run.")
        return "no scores"

    s_min   = min(scores)
    s_max   = max(scores)
    s_mean  = sum(scores) / len(scores)
    spread  = s_max - s_min

    dist_msg = (
        f"distribution — count={len(scores)} min={s_min} max={s_max} "
        f"mean={s_mean:.1f} spread={spread}"
    )

    if spread < 10:
        # Rule 20: suspect run — all scores within 10 points
        logger.warning("SUSPECT SCORING RUN: %s", dist_msg)
        storage.log_cycle(
            agent="Scorer.distribution",
            started_at=_now_iso(),
            finished_at=_now_iso(),
            records_touched=len(scores),
            status="suspect",
            notes=f"SUSPECT: {dist_msg}",
            db_path=db_path,
        )
        return "spread SUSPECT (<10)"
    else:
        logger.info(dist_msg)
        return "spread OK"
