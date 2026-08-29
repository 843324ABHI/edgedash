"""Entry point. Run one full EdgeDash cycle.

Usage:
    python run_cycle.py [--dry-run] [--force AGENT]... [--explain]

Flags:
    --dry-run          Read state, build plan, print it, then EXIT.
                       No writes, no API calls. Exit code 0.
                       Use this to check what a cycle would do before running it.

    --force AGENT      Add the named agent to the plan even if state says skip it.
                       Repeatable: --force Fetcher --force Scorer
                       A clear WARNING is printed and the override is recorded in
                       the cycle summary row.

    --explain          After printing the state block, show every state value next
                       to the exact decision it drove. The debugging tool for
                       "why did it skip that?"
"""
import argparse
import sys

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_cycle.py",
        description="Run one EdgeDash cycle (state-driven, rules 28-33).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the plan and exit without executing anything. No writes, no API calls.",
    )
    parser.add_argument(
        "--force",
        metavar="AGENT",
        action="append",
        default=[],
        help=(
            "Force an agent to run even if state says skip. "
            "Repeatable: --force Fetcher --force Scorer. "
            "Valid names: Fetcher, MockFetcher, Scorer, GapAnalyzer."
        ),
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        default=False,
        help=(
            "Print each SystemState value alongside the decision it drove. "
            "Use this to understand why an agent was skipped."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    config = load_config("config.yaml")
    run_cycle(
        config,
        dry_run=args.dry_run,
        force=args.force,
        explain=args.explain,
    )
