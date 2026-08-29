"""
edgedash/rescore.py — Manual re-scoring escape hatch.

Rule 18 says the automated cycle must never re-score a listing that already
has a score. This command is the deliberate, human-initiated exception.

The extraction cache is NEVER cleared — re-scoring costs zero API calls.

Usage:
    python -m edgedash.rescore --all
    python -m edgedash.rescore --id <listing_id>
"""

from __future__ import annotations

import argparse
import sys

from edgedash.config import load_config
from edgedash import storage


def _confirm_all() -> bool:
    """Prompt for explicit confirmation before wiping every score."""
    print(
        "\n!! WARNING: This will clear ALL scores from the database.\n"
        "   The extraction cache is untouched, so re-scoring costs no API calls.\n"
        "   The next cycle will re-score everything from scratch.\n"
    )
    try:
        answer = input("   Type 'yes' to continue, anything else to cancel: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False
    return answer.lower() == "yes"


def _run(args: argparse.Namespace) -> int:
    """Main logic. Returns an exit code."""
    cfg = load_config()
    db_path = cfg.db_path

    if args.id:
        cleared = storage.clear_score(args.id, db_path=db_path)
        if cleared == 0:
            print(
                f"No listing found with id '{args.id}'. "
                "Check the ID and try again."
            )
            return 1
        print(
            f"Cleared score for 1 listing  ->  id: {args.id}\n"
            "Extraction cache untouched -- re-scoring will cost 0 API calls.\n"
            "Run the cycle to re-score:  python run_cycle.py"
        )
        return 0

    if args.all:
        if not _confirm_all():
            print("Cancelled - no changes made.")
            return 0
        cleared = storage.clear_all_scores(db_path=db_path)
        print(
            f"\nCleared scores for {cleared} listing(s).\n"
            "Extraction cache untouched -- re-scoring will cost 0 API calls.\n"
            "Run the cycle to re-score:  python run_cycle.py"
        )
        return 0

    # Neither flag supplied — print help
    print(
        "edgedash.rescore: manual re-scoring escape hatch.\n\n"
        "  --all          clear every score (asks for confirmation)\n"
        "  --id <id>      clear the score for one listing\n\n"
        "The extraction cache is never cleared; re-scoring costs zero API calls."
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.rescore",
        description=(
            "Manual escape hatch to clear scores so the next cycle re-scores them. "
            "The extraction cache is never touched — re-scoring costs zero API calls."
        ),
        add_help=True,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--all",
        action="store_true",
        help="Clear every score in the database (asks for confirmation).",
    )
    group.add_argument(
        "--id",
        metavar="LISTING_ID",
        help="Clear the score for a single listing by its ID.",
    )
    args = parser.parse_args()
    sys.exit(_run(args))


if __name__ == "__main__":
    main()
