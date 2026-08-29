"""
edgedash/storage.py -- single storage interface (steering rule 2).

Back-end selection (rule 48):
  DATABASE_URL present  -> Postgres via psycopg2   (hosted / production)
  DATABASE_URL absent   -> SQLite                   (local development)

The active back-end is logged once at import time so it is always visible
in the process log (rules 47, 50).

CLI entry-points (run as  python -m edgedash.storage  <flag>):
  --migrate   Create / update every table on an empty Postgres DB; safe
              to run repeatedly (idempotent).
  --check     Print which back-end is active, connectivity, row counts.

No other module imports sqlite3 or psycopg2 directly (rule 2).
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import os
from contextlib import contextmanager
from typing import Any, Generator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Back-end detection (rule 48 -- one place, from environment only)
# ---------------------------------------------------------------------------
_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

if _DATABASE_URL:
    try:
        import psycopg2
        import psycopg2.extras
        _BACKEND = "postgres"
    except ImportError as _exc:  # pragma: no cover
        raise RuntimeError(
            "DATABASE_URL is set but psycopg2 is not installed. "
            "Run:  pip install psycopg2-binary"
        ) from _exc
else:
    import sqlite3 as _sqlite3
    _BACKEND = "sqlite"

_log.info("Storage back-end: %s", _BACKEND.upper())

# ---------------------------------------------------------------------------
# Placeholder tokens -- keep SQL readable without branching at every call site
# Postgres uses %s; SQLite uses ?
# ---------------------------------------------------------------------------
_PH = "%s" if _BACKEND == "postgres" else "?"


def _ph(n: int = 1) -> str:
    """Return n comma-separated placeholders for the active back-end."""
    return ", ".join([_PH] * n)


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------

@contextmanager
def _conn(db_path: str = "edgedash.db") -> Generator[Any, None, None]:
    """
    Yield a DB-API 2.0 connection for the active back-end.
    *db_path* is used only for the SQLite fall-back; Postgres reads
    DATABASE_URL from the environment (already captured in _DATABASE_URL).
    """
    if _BACKEND == "postgres":
        connection = psycopg2.connect(_DATABASE_URL)
        connection.autocommit = False
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    else:
        connection = _sqlite3.connect(db_path)
        connection.row_factory = _sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _fetchall(cursor: Any) -> list[dict[str, Any]]:
    """Return all rows as plain dicts, compatible with both back-ends."""
    if _BACKEND == "postgres":
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    else:
        return [dict(row) for row in cursor.fetchall()]


def _fetchone(cursor: Any) -> dict[str, Any] | None:
    """Return one row as a plain dict, or None."""
    if _BACKEND == "postgres":
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
    else:
        row = cursor.fetchone()
        return dict(row) if row else None


def _scalar(cursor: Any) -> Any:
    """Return the first column of the first row (aggregate helper)."""
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0]


# ---------------------------------------------------------------------------
# DDL helpers -- dialect-specific CREATE TABLE statements
# ---------------------------------------------------------------------------

_DDL_LISTINGS_SQLITE = """
    CREATE TABLE IF NOT EXISTS listings (
        id               TEXT    PRIMARY KEY,
        title            TEXT    NOT NULL,
        company          TEXT    NOT NULL,
        location         TEXT    NOT NULL,
        url              TEXT    NOT NULL,
        description      TEXT    NOT NULL,
        source           TEXT    NOT NULL,
        posted_at        TEXT    NOT NULL,
        fetched_at       TEXT    NOT NULL,
        fit_score        INTEGER NULL,
        fit_reason       TEXT    NULL,
        scored_at        TEXT    NULL,
        components_json  TEXT    NULL
    )
"""

_DDL_LISTINGS_POSTGRES = """
    CREATE TABLE IF NOT EXISTS listings (
        id               TEXT    PRIMARY KEY,
        title            TEXT    NOT NULL,
        company          TEXT    NOT NULL,
        location         TEXT    NOT NULL,
        url              TEXT    NOT NULL,
        description      TEXT    NOT NULL,
        source           TEXT    NOT NULL,
        posted_at        TEXT    NOT NULL,
        fetched_at       TEXT    NOT NULL,
        fit_score        INTEGER NULL,
        fit_reason       TEXT    NULL,
        scored_at        TEXT    NULL,
        components_json  TEXT    NULL
    )
"""

_DDL_SKILL_GAPS_SQLITE = """
    CREATE TABLE IF NOT EXISTS skill_gaps (
        skill      TEXT    PRIMARY KEY,
        frequency  INTEGER NOT NULL,
        last_seen  TEXT    NOT NULL
    )
"""

_DDL_SKILL_GAPS_POSTGRES = """
    CREATE TABLE IF NOT EXISTS skill_gaps (
        skill      TEXT    PRIMARY KEY,
        frequency  INTEGER NOT NULL,
        last_seen  TEXT    NOT NULL
    )
"""

_DDL_CYCLE_LOG_SQLITE = """
    CREATE TABLE IF NOT EXISTS cycle_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        agent           TEXT    NOT NULL,
        started_at      TEXT    NOT NULL,
        finished_at     TEXT    NOT NULL,
        records_touched INTEGER NOT NULL,
        status          TEXT    NOT NULL,
        notes           TEXT    NOT NULL
    )
"""

_DDL_CYCLE_LOG_POSTGRES = """
    CREATE TABLE IF NOT EXISTS cycle_log (
        id              SERIAL  PRIMARY KEY,
        agent           TEXT    NOT NULL,
        started_at      TEXT    NOT NULL,
        finished_at     TEXT    NOT NULL,
        records_touched INTEGER NOT NULL,
        status          TEXT    NOT NULL,
        notes           TEXT    NOT NULL
    )
"""

_DDL_EXTRACTION_CACHE_SQLITE = """
    CREATE TABLE IF NOT EXISTS extraction_cache (
        description_hash TEXT PRIMARY KEY,
        extraction_json  TEXT NOT NULL,
        cached_at        TEXT NOT NULL
    )
"""

_DDL_EXTRACTION_CACHE_POSTGRES = """
    CREATE TABLE IF NOT EXISTS extraction_cache (
        description_hash TEXT PRIMARY KEY,
        extraction_json  TEXT NOT NULL,
        cached_at        TEXT NOT NULL
    )
"""

# skill_gap_snapshots -- used by gaps.py; included so --migrate is complete.
_DDL_SGS_SQLITE = """
    CREATE TABLE IF NOT EXISTS skill_gap_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        computed_at      TEXT    NOT NULL,
        skill            TEXT    NOT NULL,
        rank             INTEGER NOT NULL,
        listings_blocked INTEGER NOT NULL DEFAULT 0,
        opportunity_cost REAL    NOT NULL DEFAULT 0,
        mean_score       REAL    NOT NULL DEFAULT 0,
        top_score        REAL    NOT NULL DEFAULT 0,
        sample_n         INTEGER NOT NULL DEFAULT 0,
        low_confidence   INTEGER NOT NULL DEFAULT 0,
        example_ids      TEXT    NOT NULL DEFAULT '[]'
    )
"""

_DDL_SGS_POSTGRES = """
    CREATE TABLE IF NOT EXISTS skill_gap_snapshots (
        id               SERIAL  PRIMARY KEY,
        computed_at      TEXT    NOT NULL,
        skill            TEXT    NOT NULL,
        rank             INTEGER NOT NULL,
        listings_blocked INTEGER NOT NULL DEFAULT 0,
        opportunity_cost REAL    NOT NULL DEFAULT 0,
        mean_score       REAL    NOT NULL DEFAULT 0,
        top_score        REAL    NOT NULL DEFAULT 0,
        sample_n         INTEGER NOT NULL DEFAULT 0,
        low_confidence   BOOLEAN NOT NULL DEFAULT FALSE,
        example_ids      TEXT    NOT NULL DEFAULT '[]'
    )
"""


def _all_ddl() -> list[str]:
    """Return the ordered list of CREATE TABLE statements for the active back-end."""
    if _BACKEND == "postgres":
        return [
            _DDL_LISTINGS_POSTGRES,
            _DDL_SKILL_GAPS_POSTGRES,
            _DDL_CYCLE_LOG_POSTGRES,
            _DDL_EXTRACTION_CACHE_POSTGRES,
            _DDL_SGS_POSTGRES,
        ]
    else:
        return [
            _DDL_LISTINGS_SQLITE,
            _DDL_SKILL_GAPS_SQLITE,
            _DDL_CYCLE_LOG_SQLITE,
            _DDL_EXTRACTION_CACHE_SQLITE,
            _DDL_SGS_SQLITE,
        ]


# ---------------------------------------------------------------------------
# Public API -- identical signatures to the original (rule 2)
# ---------------------------------------------------------------------------

def init_db(db_path: str = "edgedash.db") -> None:
    """Create all tables if they do not already exist (idempotent)."""
    with _conn(db_path) as c:
        cur = c.cursor()
        for ddl in _all_ddl():
            cur.execute(ddl)


def generate_listing_id(source: str, url: str) -> str:
    raw = f"{source.strip().lower()}:{url.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_listings(rows: list[dict[str, Any]], db_path: str = "edgedash.db") -> int:
    """
    Insert new listings, silently skipping rows whose id already exists.
    Returns the number of rows actually inserted.
    Dialect differences:
      SQLite   -- INSERT OR IGNORE
      Postgres -- INSERT ... ON CONFLICT (id) DO NOTHING
    """
    inserted_count = 0
    if _BACKEND == "postgres":
        query = """
            INSERT INTO listings (
                id, title, company, location, url, description,
                source, posted_at, fetched_at, fit_score, fit_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """
    else:
        query = """
            INSERT OR IGNORE INTO listings (
                id, title, company, location, url, description,
                source, posted_at, fetched_at, fit_score, fit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
    with _conn(db_path) as c:
        cur = c.cursor()
        for row in rows:
            record = dict(row)
            if "id" not in record or not record["id"]:
                record["id"] = generate_listing_id(
                    record.get("source", ""), record.get("url", "")
                )
            record.setdefault("fit_score", None)
            record.setdefault("fit_reason", None)
            cur.execute(query, (
                record["id"], record["title"], record["company"],
                record["location"], record["url"], record["description"],
                record["source"], record["posted_at"], record["fetched_at"],
                record["fit_score"], record["fit_reason"],
            ))
            inserted_count += cur.rowcount
    return inserted_count


def count_unscored(db_path: str = "edgedash.db") -> int:
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NULL")
        return int(_scalar(cur) or 0)


def last_fetch_time(db_path: str = "edgedash.db") -> str | None:
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("SELECT MAX(fetched_at) FROM listings")
        return _scalar(cur)


def log_cycle(
    agent: str,
    started_at: str,
    finished_at: str,
    records_touched: int,
    status: str,
    notes: str = "",
    db_path: str = "edgedash.db",
) -> int:
    if _BACKEND == "postgres":
        query = """
            INSERT INTO cycle_log
                (agent, started_at, finished_at, records_touched, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        with _conn(db_path) as c:
            cur = c.cursor()
            cur.execute(query, (agent, started_at, finished_at, records_touched, status, notes))
            return int(_scalar(cur))
    else:
        query = """
            INSERT INTO cycle_log
                (agent, started_at, finished_at, records_touched, status, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with _conn(db_path) as c:
            cur = c.cursor()
            cur.execute(query, (agent, started_at, finished_at, records_touched, status, notes))
            return int(cur.lastrowid)


def get_listings(
    limit: int = 100,
    min_score: int | None = None,
    db_path: str = "edgedash.db",
) -> list[dict[str, Any]]:
    if min_score is not None:
        sql = f"SELECT * FROM listings WHERE fit_score >= {_PH} ORDER BY fetched_at DESC LIMIT {_PH}"
        params: list[Any] = [min_score, limit]
    else:
        sql = f"SELECT * FROM listings ORDER BY fetched_at DESC LIMIT {_PH}"
        params = [limit]
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(sql, params)
        return _fetchall(cur)


def migrate_score_columns(db_path: str = "edgedash.db") -> None:
    """
    Idempotent migration: add scored_at and components_json to listings.
    Safe to run on an existing database -- uses ALTER TABLE only when column
    is absent; SQLite raises OperationalError if column already exists,
    which we catch and ignore.  Postgres 9.6+ supports IF NOT EXISTS.
    """
    with _conn(db_path) as c:
        cur = c.cursor()
        for col, definition in (
            ("scored_at",       "TEXT NULL"),
            ("components_json", "TEXT NULL"),
        ):
            if _BACKEND == "postgres":
                cur.execute(
                    f"ALTER TABLE listings ADD COLUMN IF NOT EXISTS {col} {definition}"
                )
            else:
                try:
                    cur.execute(
                        f"ALTER TABLE listings ADD COLUMN {col} {definition}"
                    )
                except Exception:
                    pass  # column already exists -- safe to continue


def write_score(
    listing_id: str,
    score: int,
    reason: str,
    components: dict[str, Any],
    db_path: str = "edgedash.db",
) -> None:
    """
    Write score, reason, components, and scored_at for one listing.
    Rule 18: only call this for listings WHERE score IS NULL.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    sql = f"""
        UPDATE listings
           SET fit_score       = {_PH},
               fit_reason      = {_PH},
               components_json = {_PH},
               scored_at       = {_PH}
         WHERE id = {_PH}
    """
    with _conn(db_path) as c:
        c.cursor().execute(sql, (score, reason, _json.dumps(components), now, listing_id))


def get_unscored_listings(
    limit: int = 25,
    db_path: str = "edgedash.db",
) -> list[dict[str, Any]]:
    """Return up to *limit* listings WHERE fit_score IS NULL (rule 18 + 21)."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(
            f"SELECT * FROM listings WHERE fit_score IS NULL LIMIT {_PH}",
            (limit,),
        )
        return _fetchall(cur)


def migrate_extraction_cache(db_path: str = "edgedash.db") -> None:
    """Idempotent migration -- safe to call on an existing database."""
    with _conn(db_path) as c:
        c.cursor().execute(
            _DDL_EXTRACTION_CACHE_POSTGRES
            if _BACKEND == "postgres"
            else _DDL_EXTRACTION_CACHE_SQLITE
        )


def get_extraction_cache(
    description_hash: str,
    db_path: str = "edgedash.db",
) -> dict[str, Any] | None:
    """Return the cached extraction dict for *description_hash*, or None."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(
            f"SELECT extraction_json FROM extraction_cache WHERE description_hash = {_PH}",
            (description_hash,),
        )
        row = _fetchone(cur)
    if row is None:
        return None
    return _json.loads(row["extraction_json"])


def set_extraction_cache(
    description_hash: str,
    extraction: dict[str, Any],
    db_path: str = "edgedash.db",
) -> None:
    """Persist *extraction* in the cache keyed on *description_hash*."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    if _BACKEND == "postgres":
        query = """
            INSERT INTO extraction_cache (description_hash, extraction_json, cached_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (description_hash) DO UPDATE
                SET extraction_json = EXCLUDED.extraction_json,
                    cached_at       = EXCLUDED.cached_at
        """
    else:
        query = """
            INSERT OR REPLACE INTO extraction_cache (description_hash, extraction_json, cached_at)
            VALUES (?, ?, ?)
        """
    with _conn(db_path) as c:
        c.cursor().execute(query, (description_hash, _json.dumps(extraction), now))


# ---------------------------------------------------------------------------
# Rescore escape hatch (manual only -- rule 18 still holds for the cycle)
# ---------------------------------------------------------------------------

_CLEAR_COLS = "fit_score = NULL, fit_reason = NULL, components_json = NULL, scored_at = NULL"


def clear_score(listing_id: str, db_path: str = "edgedash.db") -> int:
    """
    Clear the score for one listing by ID.
    Returns the number of rows actually updated (0 or 1).
    Extraction cache is NEVER touched.
    """
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(
            f"UPDATE listings SET {_CLEAR_COLS} WHERE id = {_PH}",
            (listing_id,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# State-inspection helpers  (used by edgedash/state.py -- rule 2)
# Cheap queries only: MAX(timestamp) or LIMIT 1. No full table scans.
# ---------------------------------------------------------------------------

def last_gap_computed_at(db_path: str = "edgedash.db") -> str | None:
    """Return MAX(computed_at) from skill_gap_snapshots, or None if no snapshot exists."""
    try:
        with _conn(db_path) as c:
            cur = c.cursor()
            cur.execute("SELECT MAX(computed_at) FROM skill_gap_snapshots")
            return _scalar(cur)
    except Exception:
        return None  # table may not exist yet before first gap run


def last_scored_at(db_path: str = "edgedash.db") -> str | None:
    """Return MAX(scored_at) from listings, or None if nothing has been scored."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("SELECT MAX(scored_at) FROM listings")
        return _scalar(cur)


def last_cycle_row(db_path: str = "edgedash.db") -> dict | None:
    """Return the most-recent cycle_log row as a dict, or None."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM cycle_log ORDER BY finished_at DESC LIMIT 1")
        return _fetchone(cur)


def clear_all_scores(db_path: str = "edgedash.db") -> int:
    """
    Clear scores for every listing.
    Returns the number of rows updated.
    Extraction cache is NEVER touched.
    """
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(f"UPDATE listings SET {_CLEAR_COLS}")
        return cur.rowcount


# ---------------------------------------------------------------------------
# Verification data helpers  (steering rules 34-38)
# ---------------------------------------------------------------------------

def get_verification_data(db_path: str = "edgedash.db") -> dict:
    """
    Return the three data sets run_all_checks needs, gathered in one query trip.

    Returns a dict with keys:
      scores          - list[float]  all fit_scores from the current cycle
      facts_list      - list[dict]   one dict per listing, with required_skills
      gaps            - list[dict]   top gap snapshot rows as dicts
      latest_fetch_at - str | None   MAX(fetched_at) across listings
    """
    with _conn(db_path) as c:
        cur = c.cursor()

        # Scores: every scored listing (fit_score IS NOT NULL)
        cur.execute("SELECT fit_score FROM listings WHERE fit_score IS NOT NULL")
        scores = [float(r["fit_score"]) for r in _fetchall(cur)]

        # Extraction facts: Python-side hash join -- back-ends lack sha256().
        cur.execute("SELECT description FROM listings WHERE fit_score IS NOT NULL")
        desc_rows = _fetchall(cur)
        facts_list: list[dict] = []
        for lr in desc_rows:
            h = hashlib.sha256(lr["description"].encode("utf-8")).hexdigest()
            cur.execute(
                f"SELECT extraction_json FROM extraction_cache WHERE description_hash = {_PH}",
                (h,),
            )
            cache_row = _fetchone(cur)
            if cache_row:
                facts_list.append(_json.loads(cache_row["extraction_json"]))

        # Gap snapshot: most recent computed_at
        gaps: list[dict] = []
        try:
            cur.execute("SELECT MAX(computed_at) FROM skill_gap_snapshots")
            latest_run = _scalar(cur)
            if latest_run:
                cur.execute(
                    f"""SELECT skill, rank, example_ids
                          FROM skill_gap_snapshots
                         WHERE computed_at = {_PH}
                         ORDER BY rank""",
                    (latest_run,),
                )
                for gr in _fetchall(cur):
                    gaps.append({
                        "skill": gr["skill"],
                        "rank":  gr["rank"],
                        "listing_ids": _json.loads(gr["example_ids"]),
                    })
        except Exception:
            pass  # table may not exist before first gap run

        # Latest fetch timestamp
        cur.execute("SELECT MAX(fetched_at) FROM listings")
        latest_fetch_at: str | None = _scalar(cur)

    return {
        "scores": scores,
        "facts_list": facts_list,
        "gaps": gaps,
        "latest_fetch_at": latest_fetch_at,
    }


def get_last_good_cycle(db_path: str = "edgedash.db") -> dict | None:
    """
    Return the most recent cycle_log row whose status is 'complete'.

    Rule 38: only cycles with a passing verdict may be read by the dashboard.
    A 'degraded' or 'partial' cycle must never overwrite the last known-good data.
    Stale verified data always beats fresh unverified data.

    The dashboard calls this function instead of last_cycle_row() to ensure
    it only ever shows data from a clean, verified cycle.
    """
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("""
            SELECT * FROM cycle_log
             WHERE agent = 'Orchestrator'
               AND status = 'complete'
             ORDER BY finished_at DESC
             LIMIT 1
        """)
        return _fetchone(cur)


def get_recent_cycles(
    limit: int = 30,
    db_path: str = "edgedash.db",
) -> list[dict]:
    """
    Return the most recent *limit* Orchestrator cycle_log rows, newest first.
    Includes all statuses (complete, partial, degraded) -- the dashboard
    activity log shows failures deliberately (rule 37).
    """
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(
            f"""SELECT *
                  FROM cycle_log
                 WHERE agent = 'Orchestrator'
                 ORDER BY finished_at DESC
                 LIMIT {_PH}""",
            (limit,),
        )
        return _fetchall(cur)


def get_scored_listings(
    limit: int = 10,
    db_path: str = "edgedash.db",
) -> list[dict]:
    """Return the top *limit* scored listings ordered by fit_score descending."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute(
            f"""SELECT id, title, company, location, url, fit_score, fit_reason, posted_at
                  FROM listings
                 WHERE fit_score IS NOT NULL
                 ORDER BY fit_score DESC
                 LIMIT {_PH}""",
            (limit,),
        )
        return _fetchall(cur)


def get_top_gaps(
    limit: int = 10,
    db_path: str = "edgedash.db",
) -> list[dict]:
    """Return the top *limit* skill gaps from the most recent snapshot."""
    try:
        with _conn(db_path) as c:
            cur = c.cursor()
            cur.execute("SELECT MAX(computed_at) FROM skill_gap_snapshots")
            latest = _scalar(cur)
            if not latest:
                return []
            cur.execute(
                f"""SELECT skill, rank, listings_blocked, opportunity_cost,
                           mean_score, top_score, sample_n, low_confidence
                      FROM skill_gap_snapshots
                     WHERE computed_at = {_PH}
                     ORDER BY rank
                     LIMIT {_PH}""",
                (latest, limit),
            )
            return _fetchall(cur)
    except Exception:
        return []  # table may not exist before first gap run


def get_db_summary(db_path: str = "edgedash.db") -> dict:
    """Return headline counts for the dashboard header strip."""
    with _conn(db_path) as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM listings")
        total = _scalar(cur) or 0
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL")
        scored = _scalar(cur) or 0
    return {"total_listings": int(total), "scored_listings": int(scored)}


# ---------------------------------------------------------------------------
# CLI entry-points:  python -m edgedash.storage --migrate | --check
# ---------------------------------------------------------------------------

_ALL_TABLES = [
    "listings",
    "skill_gaps",
    "cycle_log",
    "extraction_cache",
    "skill_gap_snapshots",
]


def _cli_migrate(db_path: str = "edgedash.db") -> None:
    """Create every table; safe to run on an existing database."""
    print(f"[migrate] back-end: {_BACKEND.upper()}")
    try:
        init_db(db_path)
        print("[migrate] All tables created / verified OK.")
    except Exception as exc:
        print(f"[migrate] FAILED: {exc}")
        raise SystemExit(1) from exc


def _cli_check(db_path: str = "edgedash.db") -> None:
    """Print back-end, connectivity, and row counts per table."""
    print(f"Back-end : {_BACKEND.upper()}")
    if _BACKEND == "postgres":
        url = _DATABASE_URL or ""
        print(f"URL      : {url[:40]}...  (truncated)")
    else:
        print(f"File     : {db_path}")
    try:
        with _conn(db_path) as c:
            cur = c.cursor()
            print("Connected: YES")
            for table in _ALL_TABLES:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    count = _scalar(cur) or 0
                    print(f"  {table:<28} {count:>8} rows")
                except Exception as exc:
                    print(f"  {table:<28} ERROR -- {exc}")
    except Exception as exc:
        print(f"Connected: NO -- {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    db_path_arg = "edgedash.db"
    if "--migrate" in args:
        _cli_migrate(db_path_arg)
    elif "--check" in args:
        _cli_check(db_path_arg)
    else:
        print("Usage:  python -m edgedash.storage --migrate | --check")
        raise SystemExit(1)
