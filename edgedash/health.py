"""
edgedash/health.py — Lightweight health checks (read-only, steering rule 50 compliance).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
import logging

from edgedash import storage
from edgedash.config import load_config

logger = logging.getLogger(__name__)


def check_health(db_path: str = "edgedash.db") -> dict:
    """
    Perform read-only health checks on the EdgeDash system.
    Returns a dict with check results and overall status.
    """
    results = {
        "database_reachable": {"ok": True, "detail": "Connected successfully"},
        "newest_listing_freshness": {"ok": True, "detail": ""},
        "recent_successful_cycle": {"ok": True, "detail": ""},
        "consecutive_failures": {"ok": True, "detail": ""},
    }

    # --- 1. Database Reachability ---
    db_reachable = True
    try:
        with storage._conn(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:
        db_reachable = False
        results["database_reachable"] = {"ok": False, "detail": f"Database connection failed: {e}"}

    # If the DB is unreachable, all other checks are automatically failed
    if not db_reachable:
        results["newest_listing_freshness"] = {"ok": False, "detail": "Database unreachable"}
        results["recent_successful_cycle"] = {"ok": False, "detail": "Database unreachable"}
        results["consecutive_failures"] = {"ok": False, "detail": "Database unreachable"}
        return {"is_healthy": False, "results": results}

    # --- 2. Newest Listing Freshness (< 3 days) ---
    try:
        with storage._conn(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(fetched_at) FROM listings")
            row = cur.fetchone()
            max_fetched = row[0] if row else None

        if max_fetched:
            dt = datetime.fromisoformat(max_fetched.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - dt
            if age > timedelta(days=3):
                results["newest_listing_freshness"] = {
                    "ok": False,
                    "detail": f"Newest listing is {age.days} days old (older than 3 days limit)"
                }
            else:
                results["newest_listing_freshness"] = {
                    "ok": True,
                    "detail": f"Newest listing is {age.days} days old (within 3 days limit)"
                }
        else:
            results["newest_listing_freshness"] = {
                "ok": False,
                "detail": "No listings found in the database"
            }
    except Exception as e:
        results["newest_listing_freshness"] = {
            "ok": False,
            "detail": f"Freshness check failed: {e}"
        }

    # --- 3. Recent Successful Cycle (< 48 hours) ---
    try:
        with storage._conn(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(finished_at) FROM cycle_log "
                "WHERE agent = 'Orchestrator' AND status = 'complete'"
            )
            row = cur.fetchone()
            max_finished = row[0] if row else None

        if max_finished:
            dt = datetime.fromisoformat(max_finished.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - dt
            hours_ago = age.total_seconds() / 3600
            if age > timedelta(hours=48):
                results["recent_successful_cycle"] = {
                    "ok": False,
                    "detail": f"Last successful cycle was {hours_ago:.1f}h ago (older than 48 hours limit)"
                }
            else:
                results["recent_successful_cycle"] = {
                    "ok": True,
                    "detail": f"Last successful cycle was {hours_ago:.1f}h ago (within 48 hours limit)"
                }
        else:
            results["recent_successful_cycle"] = {
                "ok": False,
                "detail": "No successful cycle logged"
            }
    except Exception as e:
        results["recent_successful_cycle"] = {
            "ok": False,
            "detail": f"Successful cycle check failed: {e}"
        }

    # --- 4. Consecutive Failures (last 3 failed verification) ---
    try:
        with storage._conn(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT status FROM cycle_log "
                "WHERE agent = 'Orchestrator' "
                "ORDER BY finished_at DESC LIMIT 3"
            )
            rows = cur.fetchall()

        if len(rows) >= 3:
            failed_count = sum(1 for r in rows if r[0] != "complete")
            if failed_count == 3:
                results["consecutive_failures"] = {
                    "ok": False,
                    "detail": "Last 3 consecutive cycles failed verification"
                }
            else:
                results["consecutive_failures"] = {
                    "ok": True,
                    "detail": f"Last 3 cycles: {3 - failed_count} passed, {failed_count} failed"
                }
        elif len(rows) > 0:
            failed_count = sum(1 for r in rows if r[0] != "complete")
            results["consecutive_failures"] = {
                "ok": True,
                "detail": f"Only {len(rows)} cycle(s) logged: {len(rows) - failed_count} passed, {failed_count} failed"
            }
        else:
            results["consecutive_failures"] = {
                "ok": True,
                "detail": "No cycles recorded yet"
            }
    except Exception as e:
        results["consecutive_failures"] = {
            "ok": False,
            "detail": f"Consecutive failure check failed: {e}"
        }

    is_healthy = all(check["ok"] for check in results.values())
    return {"is_healthy": is_healthy, "results": results}


def _run_cli() -> None:
    """CLI execution entrypoint."""
    try:
        cfg = load_config()
        db_path = cfg.db_path
    except Exception:
        db_path = "edgedash.db"

    health = check_health(db_path)

    print("=== EdgeDash Health Check ===")
    for check_name, data in health["results"].items():
        status = "PASS" if data["ok"] else "FAIL"
        print(f"[{status}] {check_name.replace('_', ' ').title()}: {data['detail']}")

    if health["is_healthy"]:
        print("System is healthy.")
        sys.exit(0)
    else:
        print("System is UNHEALTHY!")
        sys.exit(1)


if __name__ == "__main__":
    _run_cli()
