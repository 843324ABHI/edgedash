"""
edgedash/orchestrator.py — State-driven cycle orchestrator (steering rules 28-33).

Responsibilities (rule 30 — no fetching, scoring, or analysis here):
  1. Read state via state.read_state.
  2. Build a plan via planning.build_plan.
  3. Print the rendered plan BEFORE executing (rule 31).
  4. Resolve each task's agent_name from the registry.
  5. Execute only runnable tasks, in order.
     - Wrap each in try/except; one failure does NOT stop remaining tasks (rule 32).
  6. Write exactly ONE cycle summary row (rule 33).
  7. Exit cleanly when all tasks are skipped — "nothing_to_do" is a SUCCESS (rule 28).
"""

from __future__ import annotations

import io
import sys
import traceback
from datetime import datetime, timezone

# Ensure the console can handle any Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.state import read_state, SystemState
from edgedash.planning import build_plan, apply_force_overrides, Plan, Task, StopConditions
from edgedash.agents.base import AgentResult


# ---------------------------------------------------------------------------
# Agent registry  (rule 7 — Orchestrator resolves by name, knows nothing else)
# ---------------------------------------------------------------------------


def _build_registry(config: Config) -> dict:
    """Return {agent_name: agent_instance}. Built fresh each cycle so config flags apply."""
    if config.use_mock_fetcher:
        from edgedash.agents.mock_fetcher import MockFetcher
        fetcher = MockFetcher()
    else:
        from edgedash.agents.fetcher import Fetcher
        fetcher = Fetcher()

    from edgedash.agents.scorer import Scorer
    from edgedash.agents.gap_analyzer import GapAnalyzer
    from edgedash.agents.verifier import Verifier

    agents = [fetcher, Scorer(), GapAnalyzer(), Verifier()]
    return {a.name: a for a in agents}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _divider(char: str = "-", width: int = 62) -> str:
    return char * width


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_ms(ms: int) -> str:
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _print_header() -> None:
    print()
    print(_divider("="))
    print("  EDGEDASH CYCLE")
    print(_divider("="))
    print()


def _print_state_summary(state) -> None:
    print(_divider("="))
    print("  STATE")
    print(_divider("="))
    print(f"  Last fetch        : {state.last_fetch_at or 'never'}")
    hrs = f"{state.hours_since_fetch:.1f}h ago" if state.hours_since_fetch is not None else "never"
    print(f"  Hours since fetch  : {hrs}")
    print(f"  Unscored count    : {state.unscored_count}")
    print(f"  Gaps computed at  : {state.gaps_computed_at or 'never'}")
    print(f"  Gaps stale        : {state.gaps_stale}")
    print(f"  Last cycle verdict: {state.last_cycle_verdict or 'none'}")
    print()


def _print_plan(plan: Plan) -> None:
    print(_divider("="))
    print("  PLAN")
    print(_divider("="))
    print(plan.render())
    print()


def _print_explain(state: SystemState, plan: Plan) -> None:
    """--explain: print every state value alongside the decision it drove."""
    print(_divider("="))
    print("  EXPLAIN — state values and the decisions they drove")
    print(_divider("="))
    rows = [
        ("last_fetch_at",      state.last_fetch_at or "never",
         "input to hours_since_fetch calculation"),
        ("hours_since_fetch",
         f"{state.hours_since_fetch:.2f}h" if state.hours_since_fetch is not None else "never",
         _explain_reason(plan, "Fetcher")),
        ("unscored_count",     str(state.unscored_count),
         _explain_reason(plan, "Scorer")),
        ("gaps_computed_at",   state.gaps_computed_at or "never",
         "input to gaps_stale calculation"),
        ("gaps_stale",         str(state.gaps_stale),
         _explain_reason(plan, "GapAnalyzer")),
        ("last_cycle_verdict", state.last_cycle_verdict or "none",
         "informational — not used in current planning rules"),
        ("last_cycle_at",      state.last_cycle_at or "never",
         "informational — not used in current planning rules"),
    ]
    col_w = max(len(r[0]) for r in rows) + 2
    for field_name, value, decision in rows:
        print(f"  {field_name:<{col_w}} {value:<30} → {decision}")
    print()


def _explain_reason(plan: Plan, agent_name: str) -> str:
    """Return the reason string for *agent_name* from the plan, or 'not in plan'."""
    for task in plan.tasks:
        if task.agent_name == agent_name:
            return task.reason
    return "not in plan"


def _print_agent_result(result: AgentResult, duration_ms: int) -> None:
    icon = "[ok]" if result.status == "ok" else "[!!]"
    print(f"  {icon} {result.agent:<20} records={result.records_touched:<5} ({_fmt_ms(duration_ms)})")
    print(f"    {result.notes}")
    print()


def _print_skipped(task: Task) -> None:
    print(f"  [--] {task.agent_name:<20} SKIPPED — {task.reason}")
    print()


def _print_summary(
    plan: Plan,
    run_results: list[tuple[Task, AgentResult, int]],  # (task, result, ms)
    outcome: str,
    total_ms: int,
) -> None:
    print(_divider("="))
    print("  CYCLE SUMMARY")
    print(_divider("="))
    print(f"  Outcome : {outcome}")
    print(f"  Duration: {_fmt_ms(total_ms)}")
    print()
    print(f"  {'Agent':<22} {'Status':<10} {'Records':>7} {'Duration':>9}  Reason")
    print(f"  {_divider('-', 62)}")

    # Ran agents
    ran_map = {task.agent_name: (result, ms) for task, result, ms in run_results}
    for task in plan.tasks:
        if task.should_run:
            result, ms = ran_map[task.agent_name]
            print(f"  {task.agent_name:<22} {result.status:<10} {result.records_touched:>7} {_fmt_ms(ms):>9}  {task.reason}")
        else:
            print(f"  {task.agent_name:<22} {'skipped':<10} {'':>7} {'':>9}  {task.reason}")

    print(f"  {_divider('-', 62)}")
    total_records = sum(r.records_touched for _, r, _ in run_results)
    ok_count = sum(1 for _, r, _ in run_results if r.status in ("ok", "partial"))
    ran_count = len(run_results)
    print(f"  {'TOTAL':<22} {ok_count}/{ran_count} ok  {total_records:>7} records")
    print(_divider())
    print()


# ---------------------------------------------------------------------------
# Summary row builder  (rule 33 — exactly one row per cycle)
# ---------------------------------------------------------------------------


def _build_summary_notes(
    plan: Plan,
    run_results: list[tuple[Task, AgentResult, int]],
    outcome: str,
    forced: list[str] | None = None,
    verdict_notes: str | None = None,
    retry_count: int = 0,
) -> str:
    """Build the single cycle_log notes string (rule 33)."""
    parts: list[str] = [f"outcome={outcome}"]

    if forced:
        parts.append(f"operator_forced={','.join(forced)}")

    if verdict_notes:
        parts.append(f"verification={verdict_notes}")

    if retry_count:
        parts.append(f"retry_count={retry_count}")

    for task in plan.tasks:
        if task.should_run:
            matched = [(r, ms) for t, r, ms in run_results if t.agent_name == task.agent_name]
            if matched:
                result, ms = matched[0]
                parts.append(
                    f"{task.agent_name}:status={result.status},"
                    f"records={result.records_touched},{_fmt_ms(ms)}"
                )
            else:
                parts.append(f"{task.agent_name}:status=failed,not_reached")
        else:
            parts.append(f"{task.agent_name}:skipped({task.reason})")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_cycle(
    config: Config,
    dry_run: bool = False,
    force: list[str] | None = None,
    explain: bool = False,
) -> None:
    """
    Execute one EdgeDash cycle, fully state-driven.

    Parameters
    ----------
    config   : loaded project configuration
    dry_run  : if True, print plan and exit — NO writes, NO API calls
    force    : agent names to run even when state says skip
    explain  : if True, print each state value next to the decision it drove

    Rules enforced:
      28 — reads state, decides which agents to run; skipping = success
      29 — stop_conditions passed explicitly to every agent
      30 — no fetching, scoring, or analysis logic in this function
      31 — prints PLAN before executing
      32 — one agent failure does not stop remaining tasks
      33 — exactly one summary row written after the cycle
    """
    db_path = config.db_path

    _print_header()

    # ── 1. Read state (read-only — safe before init_db) ─────────────────────
    now = datetime.now(timezone.utc)
    state = read_state(config, now)
    _print_state_summary(state)

    # ── 2. Build plan ────────────────────────────────────────────────────────
    plan = build_plan(state, config)

    # --explain: show every state value and the decision it drove
    if explain:
        _print_explain(state, plan)

    # --force: promote named agents before printing the plan
    forced: list[str] = force or []
    if forced:
        plan = apply_force_overrides(plan, forced)
        print(_divider("!"))
        print("  WARNING: plan manually overridden by operator")
        print(f"  Forced agents: {', '.join(forced)}")
        print(_divider("!"))
        print()

    # Print plan — always, including for dry-run (rule 31)
    _print_plan(plan)

    # --dry-run: stop here — no writes, no API calls, exit 0
    if dry_run:
        print("  DRY RUN — plan printed, nothing executed.")
        print()
        return

    # ── Schema migration (write path starts here) ────────────────────────
    storage.init_db(db_path)

    # ── 3. Nothing to do? ────────────────────────────────────────────────────
    if not plan.runnable:
        outcome = "nothing_to_do"
        notes = _build_summary_notes(plan, [], outcome, forced)
        started = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        finished = _now_iso()
        storage.log_cycle(
            agent="Orchestrator",
            started_at=started,
            finished_at=finished,
            records_touched=0,
            status="ok",
            notes=notes,
            db_path=db_path,
        )
        print("  Nothing to do — system is fully up to date.")
        print(f"  Logged cycle: {outcome}")
        print()
        return  # exit code 0, no warnings (rule 28)

    # ── 4. Resolve registry ──────────────────────────────────────────────────
    registry = _build_registry(config)

    # ── 5. Execute runnable tasks ────────────────────────────────────────────
    print(_divider("="))
    print("  RUNNING AGENTS")
    print(_divider("="))
    print()

    cycle_start = now
    run_results: list[tuple[Task, AgentResult, int]] = []
    any_failed = False

    for task in plan.runnable:
        agent = registry.get(task.agent_name)
        if agent is None:
            # Unknown agent — log and continue (rule 32)
            print(f"  [!!] {task.agent_name:<20} NOT IN REGISTRY — skipping")
            any_failed = True
            continue

        t0 = datetime.now(timezone.utc)
        try:
            result = agent.run(config, db_path, task.stop_conditions)
        except Exception as exc:
            # Rule 32: failure of one agent does not stop the cycle
            t1 = datetime.now(timezone.utc)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
            tb = traceback.format_exc()
            err_notes = f"{type(exc).__name__}: {exc}\n{tb}"
            print(f"  [!!] {task.agent_name:<20} RAISED {type(exc).__name__} — continuing cycle")
            print(f"    {str(exc)[:120]}")
            print()
            result = AgentResult(
                agent=task.agent_name,
                status="failed",
                records_touched=0,
                notes=err_notes[:400],
            )
            any_failed = True
            storage.log_cycle(
                agent=task.agent_name,
                started_at=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                finished_at=t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
                records_touched=0,
                status="failed",
                notes=err_notes[:400],
                db_path=db_path,
            )
            run_results.append((task, result, duration_ms))
            continue
        else:
            t1 = datetime.now(timezone.utc)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
            if result.status not in ("ok", "partial"):
                any_failed = True

        _print_agent_result(result, duration_ms)
        run_results.append((task, result, duration_ms))

    # Print skipped tasks so the terminal output is complete (rule 31)
    for task in plan.skipped:
        _print_skipped(task)

    # ── 6. Verify output + bounded retry (steering rules 34-36) ─────────────
    verdict_notes: str = ""
    retry_count: int = 0
    verifier = registry.get("Verifier")

    if verifier is not None and plan.runnable:
        print(_divider("="))
        print("  VERIFICATION")
        print(_divider("="))
        print()

        # First verification pass
        v_result = verifier.run(config, db_path, StopConditions())
        verdict = v_result.verdict
        verdict_notes = v_result.notes

        if verdict is not None and not verdict.passed:
            # ── One retry for the failing agent with adjusted context (rule 36)
            failed_check_names = {c.name for c in verdict.failed_checks}
            retry_agent_name: str | None = None
            retry_stop: StopConditions = StopConditions()

            if "score_spread" in failed_check_names:
                # Scorer produced a compressed distribution — re-score with
                # widen_spread=True which clears all scores and forces a fresh
                # full-corpus run (see scorer.py for the mechanism rationale).
                retry_agent_name = "Scorer"
                retry_stop = StopConditions(
                    max_items=config.llm_batch_size,
                    max_seconds=config.score_max_seconds,
                    widen_spread=True,
                )
            elif "freshness" in failed_check_names:
                retry_agent_name = "Fetcher"
                retry_stop = StopConditions(
                    max_pages=config.fetch_max_pages,
                    max_items=config.fetch_max_listings,
                )
            elif "extraction_sanity" in failed_check_names:
                retry_agent_name = "Scorer"
                retry_stop = StopConditions(
                    max_items=config.llm_batch_size,
                    max_seconds=config.score_max_seconds,
                )
            elif "gap_sample_size" in failed_check_names:
                retry_agent_name = "GapAnalyzer"
                retry_stop = StopConditions(
                    max_seconds=config.analyse_max_seconds,
                )

            if retry_agent_name is not None:
                retry_agent = registry.get(retry_agent_name)
                if retry_agent is not None:
                    retry_count = 1
                    print(f"  [..] RETRY {retry_agent_name} (failed checks: {', '.join(failed_check_names)})")
                    print()
                    t0 = datetime.now(timezone.utc)
                    try:
                        retry_result = retry_agent.run(config, db_path, retry_stop)
                    except Exception as exc:
                        tb = traceback.format_exc()
                        print(f"  [!!] RETRY {retry_agent_name} RAISED {type(exc).__name__} — marking degraded")
                        retry_result = AgentResult(
                            agent=retry_agent_name,
                            status="failed",
                            records_touched=0,
                            notes=f"{type(exc).__name__}: {exc}"[:200],
                        )
                    t1 = datetime.now(timezone.utc)
                    duration_ms = int((t1 - t0).total_seconds() * 1000)
                    _print_agent_result(retry_result, duration_ms)

                    # Second (final) verification pass after retry
                    print("  [..] Re-verifying after retry...")
                    print()
                    v2_result = verifier.run(config, db_path, StopConditions())
                    verdict = v2_result.verdict
                    verdict_notes = v2_result.notes

            # ── If still failing after retry: degrade and stop (rule 36) ────
            if verdict is not None and not verdict.passed:
                outcome = "degraded"
                cycle_end = datetime.now(timezone.utc)
                total_ms = int((cycle_end - cycle_start).total_seconds() * 1000)
                notes = _build_summary_notes(
                    plan, run_results, outcome, forced,
                    verdict_notes=verdict_notes, retry_count=retry_count,
                )
                storage.log_cycle(
                    agent="Orchestrator",
                    started_at=cycle_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    finished_at=cycle_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    records_touched=sum(r.records_touched for _, r, _ in run_results),
                    status=outcome,
                    notes=notes,
                    db_path=db_path,
                )
                print(_divider("!"))
                print(f"  CYCLE DEGRADED — verification failed after {retry_count} retry.")
                print(f"  {verdict_notes}")
                print("  Last known-good data is preserved (rule 38).")
                print(_divider("!"))
                print()
                _print_summary(plan, run_results, outcome, total_ms)
                return   # Stop — do NOT raise (rule 36)

    # ── 7. Determine outcome ─────────────────────────────────────────────────
    if any_failed:
        outcome = "partial"
    else:
        outcome = "complete"

    # ── 8. Write exactly one summary row (rule 33) ───────────────────────────
    cycle_end = datetime.now(timezone.utc)
    total_ms = int((cycle_end - cycle_start).total_seconds() * 1000)
    notes = _build_summary_notes(
        plan, run_results, outcome, forced,
        verdict_notes=verdict_notes, retry_count=retry_count,
    )

    storage.log_cycle(
        agent="Orchestrator",
        started_at=cycle_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        finished_at=cycle_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        records_touched=sum(r.records_touched for _, r, _ in run_results),
        status=outcome,
        notes=notes,
        db_path=db_path,
    )

    _print_summary(plan, run_results, outcome, total_ms)
