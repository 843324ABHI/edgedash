"""
edgedash/agents/verifier.py — Plausibility verifier (steering rules 34-38).

Responsibilities:
  - Read current cycle data from storage (scores, facts, gaps, fetch time).
  - Call run_all_checks — pure, deterministic, no LLM.
  - Write the verdict to cycle_log ONLY. No other data is written (rule 34).
  - Return an AgentResult carrying the Verdict in notes.

The Verifier never repairs, rewrites, or adjusts data.
The Orchestrator decides what to do about a failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.verification import run_all_checks, Verdict
import edgedash.storage as storage


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC string into an aware datetime, or return None."""
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _verdict_notes(verdict: Verdict) -> str:
    """
    Build the cycle_log notes string for a verdict.

    Rule 37: every logged entry names the check that failed AND the observed
    value — never just "failed".
    """
    if verdict.passed:
        return "VERDICT: pass — all checks passed"

    parts = ["VERDICT: fail"]
    for check in verdict.failed_checks:
        parts.append(
            f"{check.name} observed={check.observed!r} threshold={check.threshold!r}"
        )
    return " | ".join(parts)


class Verifier:
    name: str = "Verifier"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions = StopConditions(),
    ) -> AgentResult:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── 1. Gather data from storage (read-only) ──────────────────────────
        data = storage.get_verification_data(db_path)

        latest_fetch_dt = _parse_ts(data["latest_fetch_at"])
        now = datetime.now(timezone.utc)

        # ── 2. Run all plausibility checks (pure functions, no LLM) ─────────
        verdict = run_all_checks(
            scores=data["scores"],
            facts_list=data["facts_list"],
            gaps=data["gaps"],
            latest_fetch_at=latest_fetch_dt,
            config=config,
            now=now,
        )

        # ── 3. Log the verdict to cycle_log (ONLY write, rule 34) ───────────
        notes = _verdict_notes(verdict)
        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        storage.log_cycle(
            agent="Verifier",
            started_at=started,
            finished_at=finished,
            records_touched=0,   # Verifier writes no data rows — verdict only
            status="ok" if verdict.passed else "failed",
            notes=notes,
            db_path=db_path,
        )

        # Print to terminal so the operator sees the verdict inline
        icon = "[ok]" if verdict.passed else "[!!]"
        print(f"  {icon} Verifier              {notes}")
        if not verdict.passed:
            for check in verdict.failed_checks:
                print(f"      — {check.name}: {check.message}")
        print()

        return AgentResult(
            agent=self.name,
            status="ok" if verdict.passed else "failed",
            records_touched=0,
            notes=notes,
            verdict=verdict,
        )
