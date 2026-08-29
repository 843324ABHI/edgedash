"""
edgedash/gaps.py — Morning gap report from the latest snapshot.

Usage:
    python -m edgedash.gaps [--db PATH] [--all]
    python -m edgedash.gaps --trend [--db PATH]

Prints the most recent skill_gap_snapshots run as a readable terminal table.
Read-only.  Never writes to the database.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from pathlib import Path

# Ensure Unicode box-drawing characters render on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _latest_run_id(db_path: str) -> str | None:
    """Return the run_id of the most recent snapshot, or None."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT run_id FROM skill_gap_snapshots ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def _load_snapshot(run_id: str, db_path: str) -> list[dict]:
    """Return all rows for *run_id*, ordered by rank."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT rank, skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, sample_n, low_confidence,
                   nice_to_have_count, example_ids, computed_at
            FROM skill_gap_snapshots
            WHERE run_id = ?
            ORDER BY rank
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _list_all_runs(db_path: str) -> list[tuple[str, int]]:
    """Return [(run_id, row_count)] for every distinct run, newest first."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT run_id, COUNT(*) as n
            FROM skill_gap_snapshots
            GROUP BY run_id
            ORDER BY run_id DESC
            """
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BAR_WIDTH = 20  # characters for the opportunity_cost bar


def _bar(value: float, max_value: float) -> str:
    """ASCII progress bar scaled to max_value."""
    if max_value <= 0:
        return " " * _BAR_WIDTH
    filled = round((value / max_value) * _BAR_WIDTH)
    filled = max(0, min(_BAR_WIDTH, filled))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _print_snapshot(rows: list[dict]) -> None:
    if not rows:
        print("  (no gaps in this snapshot)")
        return

    computed_at = rows[0].get("computed_at", "unknown")
    max_cost = max(r["opportunity_cost"] for r in rows)

    # ── header ─────────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  EDGEDASH SKILL GAP REPORT")
    print(f"  Snapshot: {computed_at}")
    print("=" * 80)
    print()

    col_skill  = 22
    col_n      = 8
    col_cost   = 8
    col_mean   = 7
    col_top    = 6

    header = (
        f"  {'#':>2}  {'SKILL':<{col_skill}}  {'BLOCKED':>{col_n}}"
        f"  {'COST':>{col_cost}}  {'MEAN':>{col_mean}}  {'TOP':>{col_top}}"
        f"  {'OPPORTUNITY':^{_BAR_WIDTH}}"
    )
    print(header)
    print("  " + "─" * (len(header) - 2))

    for row in rows:
        rank     = row["rank"]
        skill    = row["skill"]
        n        = row["listings_blocked"]
        cost     = row["opportunity_cost"]
        mean_s   = row["mean_score"]
        top_s    = row["top_score"]
        sample_n = row["sample_n"]
        low_conf = row["low_confidence"]
        nth      = row["nice_to_have_count"]

        bar = _bar(cost, max_cost)

        # flag low-confidence gaps so they're visually distinct (rule 27)
        conf_flag = " ⚠ low-n" if low_conf else ""

        skill_display = skill[:col_skill]

        line = (
            f"  {rank:>2}  {skill_display:<{col_skill}}  {n:>{col_n}}"
            f"  {cost:>{col_cost}.2f}  {mean_s:>{col_mean}.1f}"
            f"  {top_s:>{col_top}}"
            f"  {bar}{conf_flag}"
        )
        print(line)

        # nice-to-have footnote
        if nth:
            print(f"      also nice-to-have in {nth} listing(s)")

        # example listing IDs for drill-down (rule 26)
        try:
            ids = json.loads(row.get("example_ids", "[]"))
        except (json.JSONDecodeError, TypeError):
            ids = []
        if ids:
            ids_str = "  ".join(i[:16] for i in ids)
            print(f"      from: {ids_str}")

    print()
    print(
        f"  BLOCKED = listings requiring this skill that I lack  |  "
        f"COST = Σ(score/100)  |  MEAN/TOP = fit scores"
    )
    print(
        f"  ⚠ low-n = fewer than 3 listings — treat as low confidence (rule 27)"
    )
    if sample_n:
        pass  # already shown per row
    print("=" * 80)
    print()


# ---------------------------------------------------------------------------
# Trend reporting
# ---------------------------------------------------------------------------


def _load_all_run_ids(db_path: str) -> list[str]:
    """Return every distinct run_id ordered oldest → newest."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM skill_gap_snapshots ORDER BY run_id ASC"
        ).fetchall()
    return [r[0] for r in rows]


def _load_snapshot_as_cost_map(run_id: str, db_path: str) -> dict[str, float]:
    """Return {skill: opportunity_cost} for one snapshot."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT skill, opportunity_cost FROM skill_gap_snapshots WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _print_trend(db_path: str) -> None:
    """
    Compare the earliest and latest snapshots.

    For each skill in the current top 10:
      - Show opportunity_cost at earliest and latest snapshot.
      - Show absolute change and percent change.
      - Mark skills that are NEW since the earliest snapshot.

    Also list skills that were in the earliest top 10 but have since
    DROPPED OUT of the current top 10.

    If there is only one snapshot: say so explicitly and report how many
    more days of runs are needed.  Never fabricate a trend.
    """
    run_ids = _load_all_run_ids(db_path)

    if not run_ids:
        print("\n  No snapshots yet. Run the cycle first: python run_cycle.py\n")
        return

    if len(run_ids) == 1:
        print()
        print("=" * 80)
        print("  EDGEDASH GAP TREND")
        print("=" * 80)
        print()
        print(f"  Only one snapshot exists ({run_ids[0]}).")
        print()
        print("  A trend requires at least 2 snapshots on different days.")
        print("  Run the cycle again tomorrow (or after new listings are scored)")
        print("  to see the first delta.")
        print()
        print("  Minimum snapshots needed to show a trend: 2")
        print("  You currently have                       : 1")
        print("  Runs still needed                        : 1")
        print()
        print("  No trend has been fabricated, interpolated, or extrapolated.")
        print("=" * 80)
        print()
        return

    earliest_id = run_ids[0]
    latest_id   = run_ids[-1]

    earliest_map  = _load_snapshot_as_cost_map(earliest_id, db_path)
    latest_rows   = _load_snapshot(latest_id, db_path)
    latest_map    = {r["skill"]: r["opportunity_cost"] for r in latest_rows}

    current_top10 = [r["skill"] for r in latest_rows]          # ordered by rank
    earliest_top10_map = _load_snapshot_as_cost_map(earliest_id, db_path)
    # earliest top 10 by opportunity_cost
    earliest_top10 = [
        s for s, _ in sorted(
            earliest_top10_map.items(), key=lambda kv: -kv[1]
        )[:10]
    ]

    dropped = [s for s in earliest_top10 if s not in current_top10]

    # ── header ────────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  EDGEDASH GAP TREND")
    print(f"  Comparing {len(run_ids)} snapshots")
    print(f"  Earliest : {earliest_id}")
    print(f"  Latest   : {latest_id}")
    print("=" * 80)
    print()

    col_skill  = 22
    col_cost   = 8
    col_change = 9
    col_pct    = 8

    header = (
        f"  {'#':>2}  {'SKILL':<{col_skill}}"
        f"  {'EARLIEST':>{col_cost}}  {'LATEST':>{col_cost}}"
        f"  {'CHANGE':>{col_change}}  {'PCT':>{col_pct}}  NOTE"
    )
    print(header)
    print("  " + "─" * (len(header) - 2))

    for i, row in enumerate(latest_rows, start=1):
        skill = row["skill"]
        latest_cost   = row["opportunity_cost"]
        earliest_cost = earliest_map.get(skill)   # None if skill is new

        if earliest_cost is None:
            # Skill did not exist in the earliest snapshot
            note = "NEW"
            earliest_str = "   —"
            change_str   = "   —"
            pct_str      = "   —"
        else:
            delta = latest_cost - earliest_cost
            # Guard against divide-by-zero on a zero-cost earliest entry
            if earliest_cost != 0.0:
                pct = (delta / earliest_cost) * 100.0
                pct_str = f"{pct:+.1f}%"
            else:
                pct_str = "  n/a"

            arrow = "▲" if delta > 0.005 else ("▼" if delta < -0.005 else "─")
            change_str   = f"{arrow} {abs(delta):.2f}" if delta != 0 else "  ─  "
            earliest_str = f"{earliest_cost:.2f}"
            note = ""

        skill_display = skill[:col_skill]
        latest_str    = f"{latest_cost:.2f}"

        print(
            f"  {i:>2}  {skill_display:<{col_skill}}"
            f"  {earliest_str:>{col_cost}}  {latest_str:>{col_cost}}"
            f"  {change_str:>{col_change}}  {pct_str:>{col_pct}}  {note}"
        )

    if dropped:
        print()
        print("  DROPPED OUT of top 10 since earliest snapshot:")
        for skill in dropped:
            old_cost = earliest_top10_map.get(skill, 0.0)
            print(f"    {skill}  (was cost {old_cost:.2f})")

    print()
    print(
        "  CHANGE = latest − earliest opportunity_cost  |  "
        "▲ rising  ▼ falling  ─ flat"
    )
    print("  NEW = skill not present in earliest snapshot")
    print("=" * 80)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Print the latest EdgeDash skill gap report."
    )
    parser.add_argument(
        "--db",
        default="edgedash.db",
        metavar="PATH",
        help="Path to the SQLite database (default: edgedash.db).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="List all historical snapshot run IDs instead of the latest.",
    )
    parser.add_argument(
        "--run",
        metavar="RUN_ID",
        default=None,
        help="Print a specific historical snapshot by run_id.",
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help="Compare earliest vs latest snapshot to show opportunity_cost movement.",
    )
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"[ERROR] Database not found at '{args.db}'.", file=sys.stderr)
        sys.exit(1)

    # Check table exists
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.execute("SELECT 1 FROM skill_gap_snapshots LIMIT 1")
    except sqlite3.OperationalError:
        print(
            "No gap snapshots found yet. Run the cycle first:\n"
            "  python run_cycle.py",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.trend:
        _print_trend(str(db))
        return

    if args.all:
        runs = _list_all_runs(str(db))
        if not runs:
            print("No snapshots recorded yet.")
            return
        print(f"\n  {'RUN ID (timestamp)':<30}  GAPS")
        print("  " + "─" * 36)
        for run_id, count in runs:
            print(f"  {run_id:<30}  {count}")
        print()
        return

    run_id = args.run or _latest_run_id(str(db))
    if not run_id:
        print("No snapshots found. Run the cycle first.")
        return

    rows = _load_snapshot(run_id, str(db))
    _print_snapshot(rows)


if __name__ == "__main__":
    _main()
