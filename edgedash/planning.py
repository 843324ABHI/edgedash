"""
edgedash/planning.py — Pure, deterministic plan builder.

`build_plan(state, config) -> Plan`
  * Pure function: no I/O, no datetime.now(), no LLM.
  * All thresholds come from config, never hardcoded here.
  * Skipped agents appear in the plan WITH a reason (steering rule 31).
  * Decision logic is arithmetic on timestamps and counts only.

`Plan.render() -> str`
  * One line per agent: RUN or SKIP, goal, stop conditions, reason.
  * Printed by the Orchestrator before it executes (steering rule 31).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edgedash.config import Config
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopConditions:
    """Explicit upper bounds the Orchestrator passes to a sub-agent (rule 29)."""
    max_items: int | None = None        # listings, pages, rows — agent-specific
    max_pages: int | None = None        # only meaningful for Fetcher
    max_seconds: int | None = None      # wall-clock budget
    widen_spread: bool = False          # Verifier retry hint: Scorer must widen score distribution


@dataclass(frozen=True)
class Task:
    agent_name: str
    goal: str
    stop_conditions: StopConditions
    reason: str                         # human-readable state value that caused decision
    should_run: bool                    # False → SKIP entry (still visible in plan, rule 31)


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def runnable(self) -> list[Task]:
        return [t for t in self.tasks if t.should_run]

    @property
    def skipped(self) -> list[Task]:
        return [t for t in self.tasks if not t.should_run]

    # ------------------------------------------------------------------
    # Rendering  (steering rule 31)
    # ------------------------------------------------------------------

    def render(self) -> str:
        """
        Return a compact, human-readable plan string.
        One line per agent.  Example:

          [RUN ] Fetcher       goal=fetch new listings  pages≤5 listings≤200 secs≤300  hours_since_fetch=7.3
          [SKIP] Scorer        goal=score unscored rows  items≤25 secs≤300              skipped: unscored_count=0
          [RUN ] GapAnalyzer   goal=compute gap snapshot  secs≤120                      gaps_stale=True
        """
        lines: list[str] = []
        for task in self.tasks:
            tag = "RUN " if task.should_run else "SKIP"
            stop = _format_stop(task.stop_conditions)
            lines.append(
                f"  [{tag}] {task.agent_name:<14} goal={task.goal:<26} {stop:<30} {task.reason}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_stop(sc: StopConditions) -> str:
    parts: list[str] = []
    if sc.max_pages is not None:
        parts.append(f"pages≤{sc.max_pages}")
    if sc.max_items is not None:
        parts.append(f"items≤{sc.max_items}")
    if sc.max_seconds is not None:
        parts.append(f"secs≤{sc.max_seconds}")
    return " ".join(parts) if parts else "(no limit)"


def _decide_fetch(state: SystemState, config: Config) -> Task:
    """Run Fetcher when data is absent or stale (hours_since_fetch >= threshold)."""
    threshold = config.fetch_interval_hours

    if state.hours_since_fetch is None:
        reason = "hours_since_fetch=never (no data)"
        run = True
    elif state.hours_since_fetch >= threshold:
        reason = f"hours_since_fetch={state.hours_since_fetch:.1f} >= {threshold}"
        run = True
    else:
        reason = f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} < {threshold}"
        run = False

    return Task(
        agent_name="Fetcher",
        goal="fetch new listings",
        stop_conditions=StopConditions(
            max_pages=config.fetch_max_pages,
            max_items=config.fetch_max_listings,
            max_seconds=None,
        ),
        reason=reason,
        should_run=run,
    )


def _decide_score(state: SystemState, config: Config) -> Task:
    """Run Scorer when there are unscored listings waiting."""
    if state.unscored_count > 0:
        reason = f"unscored_count={state.unscored_count}"
        run = True
    else:
        reason = f"skipped: unscored_count=0"
        run = False

    return Task(
        agent_name="Scorer",
        goal="score unscored rows",
        stop_conditions=StopConditions(
            max_items=config.llm_batch_size,
            max_seconds=config.score_max_seconds,
        ),
        reason=reason,
        should_run=run,
    )


def _decide_analyse(state: SystemState, config: Config) -> Task:
    """Run GapAnalyzer when gaps are missing or stale (any score newer than snapshot)."""
    if state.gaps_computed_at is None:
        reason = "gaps_computed_at=None (no snapshot)"
        run = True
    elif state.gaps_stale:
        reason = f"gaps_stale=True (scores newer than {state.gaps_computed_at})"
        run = True
    else:
        reason = f"skipped: gaps_stale=False, gaps_computed_at={state.gaps_computed_at}"
        run = False

    return Task(
        agent_name="GapAnalyzer",
        goal="compute gap snapshot",
        stop_conditions=StopConditions(
            max_seconds=config.analyse_max_seconds,
        ),
        reason=reason,
        should_run=run,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_plan(state: SystemState, config: Config) -> Plan:
    """
    Build an ordered plan from the current system state and config.

    Pure function: no I/O, no side effects, deterministic.
    Skipped agents are represented as Task(should_run=False) so that the
    Orchestrator can log them by reason (steering rule 31).

    Agent order is fixed: Fetcher → Scorer → GapAnalyzer.
    The Orchestrator decides which of these to actually run.
    """
    return Plan(tasks=[
        _decide_fetch(state, config),
        _decide_score(state, config),
        _decide_analyse(state, config),
    ])


def apply_force_overrides(plan: Plan, force_names: list[str]) -> Plan:
    """
    Return a new Plan with every agent in *force_names* promoted to
    should_run=True, regardless of what the planner decided.

    Pure function — does NOT alter build_plan's rules or any Task's
    stop_conditions.  The operator override is recorded in the reason
    string so it is visible in the log (rule 33).

    Agents not present in the original plan are silently ignored
    (the registry lookup in the Orchestrator handles that case).
    """
    if not force_names:
        return plan

    force_set = {n.strip() for n in force_names}
    new_tasks: list[Task] = []
    for task in plan.tasks:
        if task.agent_name in force_set and not task.should_run:
            # Replace the skipped task with an identical one that will run,
            # keeping the original reason for audit and prepending the override.
            new_tasks.append(Task(
                agent_name=task.agent_name,
                goal=task.goal,
                stop_conditions=task.stop_conditions,
                reason=f"forced by operator (was: {task.reason})",
                should_run=True,
            ))
        else:
            new_tasks.append(task)

    return Plan(tasks=new_tasks)

