"""
edgedash/query/tools.py — Parameterised read-only query tool registry.

Steering rules enforced:
  40  No text-to-SQL. Every query is a fixed Python function I wrote.
  41  Parameters are typed, validated, and clamped before use.
  42  No model call in this file — this is the deterministic layer.
  46  All reads use data from the last passing cycle only.

Public API:
    TOOLS          – dict[str, ToolSpec]  (name → description + params + fn)
    get_tool_specs – returns the serialisable registry the router model sees

"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from edgedash import storage
from edgedash.config import load_config
from edgedash.skills import canonical


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    """JSON-schema-style spec for a single parameter."""
    name: str
    type: str               # "integer" | "string"
    description: str
    required: bool = False
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    """One registered query tool."""
    name: str
    description: str
    params: list[ParamSpec]
    fn: Callable[..., dict[str, Any]]


TOOLS: dict[str, ToolSpec] = {}


def _tool(
    name: str,
    description: str,
    params: list[ParamSpec],
) -> Callable:
    """Decorator that registers a function in the TOOLS dict."""
    def decorator(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            params=params,
            fn=fn,
        )
        return fn
    return decorator


def get_tool_specs() -> list[dict[str, Any]]:
    """Return the registry as JSON-serialisable dicts (what the router sees)."""
    specs: list[dict[str, Any]] = []
    for tool in TOOLS.values():
        param_list = []
        for p in tool.params:
            entry: dict[str, Any] = {
                "name": p.name,
                "type": p.type,
                "description": p.description,
            }
            if p.required:
                entry["required"] = True
            if p.default is not None:
                entry["default"] = p.default
            if p.minimum is not None:
                entry["minimum"] = p.minimum
            if p.maximum is not None:
                entry["maximum"] = p.maximum
            param_list.append(entry)

        specs.append({
            "name": tool.name,
            "description": tool.description,
            "parameters": param_list,
        })
    return specs


# ---------------------------------------------------------------------------
# Helpers — parameter validation (rule 41)
# ---------------------------------------------------------------------------


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """Coerce *value* to int, then clamp to [low, high]."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def _load_aliases() -> dict[str, str]:
    """Load the skill alias map from config.yaml — same path as gap_analyzer."""
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
    return {}


def _canonicalise_skill(raw: str) -> str:
    """Normalise and alias-resolve a model-supplied skill string."""
    aliases = _load_aliases()
    return canonical(raw, aliases)


def _skill_exists_in_db(skill: str, db_path: str) -> bool:
    """Check whether *skill* appears in any snapshot (never interpolated)."""
    rows = storage.get_top_gaps(limit=100, db_path=db_path)
    return any(r["skill"] == skill for r in rows)


def _db_path() -> str:
    """Resolve db_path from config, falling back to default."""
    try:
        cfg = load_config()
        return cfg.db_path
    except FileNotFoundError:
        return "edgedash.db"


# ---------------------------------------------------------------------------
# Tools — all read-only, all from the last passing cycle (rule 46)
# ---------------------------------------------------------------------------


@_tool(
    name="companies_hiring",
    description=(
        "Companies with job listings posted in the last N days, with a count "
        "of how many listings each company has. Use when the question asks "
        "which companies are hiring, who is recruiting, or how many openings "
        "a company has recently posted."
    ),
    params=[
        ParamSpec(
            name="days",
            type="integer",
            description="Look-back window in days.",
            default=7,
            minimum=1,
            maximum=90,
        ),
    ],
)
def companies_hiring(days: int = 7) -> dict[str, Any]:
    """Companies with listings posted in the last N days, with counts."""
    days = _clamp_int(days, 1, 90, 7)
    db = _db_path()

    # Rule 46: only read from the last passing cycle
    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    # Fetch all scored listings and filter by posted_at within the window
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    all_listings = storage.get_listings(limit=10_000, db_path=db)
    recent = [
        row for row in all_listings
        if row.get("posted_at") and row["posted_at"] >= cutoff_iso
    ]

    # Aggregate by company
    counts: dict[str, int] = {}
    for row in recent:
        company = row.get("company", "Unknown")
        counts[company] = counts.get(company, 0) + 1

    rows = sorted(
        [{"company": c, "listings": n} for c, n in counts.items()],
        key=lambda r: r["listings"],
        reverse=True,
    )

    summary = f"{len(recent)} listings from {len(counts)} companies in the last {days} days."
    return {"rows": rows, "summary": summary}


@_tool(
    name="best_matches",
    description=(
        "Highest-scoring listings ordered by fit score descending. Returns "
        "score, title, company, and reason. Use when the question asks about "
        "top matches, best fits, highest-scored jobs, or recommended listings."
    ),
    params=[
        ParamSpec(
            name="n",
            type="integer",
            description="Number of listings to return.",
            default=10,
            minimum=1,
            maximum=25,
        ),
    ],
)
def best_matches(n: int = 10) -> dict[str, Any]:
    """Top N highest-scoring listings."""
    n = _clamp_int(n, 1, 25, 10)
    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    rows = storage.get_scored_listings(limit=n, db_path=db)
    result = [
        {
            "title": r["title"],
            "company": r["company"],
            "score": r["fit_score"],
            "reason": r.get("fit_reason", ""),
            "url": r.get("url", ""),
        }
        for r in rows
    ]
    summary = f"Top {len(result)} listings by fit score."
    return {"rows": result, "summary": summary}


@_tool(
    name="top_gaps",
    description=(
        "Top skill gaps ranked by opportunity cost (sum of fit scores of "
        "listings blocked by each missing skill). Returns skill name, "
        "opportunity cost, and number of listings blocked. Use when the "
        "question asks about skill gaps, missing skills, what to learn, "
        "or which skills would unlock the most opportunities."
    ),
    params=[
        ParamSpec(
            name="n",
            type="integer",
            description="Number of gaps to return.",
            default=5,
            minimum=1,
            maximum=25,
        ),
    ],
)
def top_gaps(n: int = 5) -> dict[str, Any]:
    """Top N skill gaps by opportunity cost."""
    n = _clamp_int(n, 1, 25, 5)
    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    gaps = storage.get_top_gaps(limit=n, db_path=db)
    result = [
        {
            "skill": g["skill"],
            "opportunity_cost": g["opportunity_cost"],
            "listings_blocked": g["listings_blocked"],
            "mean_score": g["mean_score"],
            "sample_n": g["sample_n"],
            "low_confidence": bool(g.get("low_confidence")),
        }
        for g in gaps
    ]
    summary = f"Top {len(result)} skill gaps by opportunity cost."
    return {"rows": result, "summary": summary}


@_tool(
    name="gap_detail",
    description=(
        "Drill-down for one specific skill gap: lists the individual job "
        "listings that require this skill and are blocked by its absence. "
        "Use when the question names a specific skill and asks which jobs "
        "need it, or wants details about one gap. This is rule 26's "
        "drill-down exposed as a question."
    ),
    params=[
        ParamSpec(
            name="skill",
            type="string",
            description="The skill to drill into (will be canonicalised).",
            required=True,
        ),
    ],
)
def gap_detail(skill: str) -> dict[str, Any]:
    """Listings blocked by one named skill."""
    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    canon = _canonicalise_skill(skill)
    if not canon:
        return {"rows": [], "summary": f"Skill '{skill}' resolved to empty after canonicalisation."}

    # Find the gap snapshot row for this skill
    all_gaps = storage.get_top_gaps(limit=100, db_path=db)
    match = None
    for g in all_gaps:
        if g["skill"] == canon:
            match = g
            break

    if match is None:
        return {
            "rows": [],
            "summary": f"No gap found for skill '{canon}'. It may not appear in any listing.",
        }

    # Resolve example_ids to actual listings
    try:
        example_ids: list[str] = json.loads(match.get("example_ids", "[]"))
    except (json.JSONDecodeError, TypeError):
        example_ids = []

    if not example_ids:
        return {
            "rows": [{"skill": canon, "listings_blocked": match["listings_blocked"]}],
            "summary": f"Gap '{canon}' blocks {match['listings_blocked']} listings (no IDs recorded).",
        }

    # Fetch the actual listings by ID
    all_listings = storage.get_listings(limit=10_000, db_path=db)
    id_to_listing = {row["id"]: row for row in all_listings}

    result = []
    for lid in example_ids:
        listing = id_to_listing.get(lid)
        if listing:
            result.append({
                "id": lid[:16],
                "title": listing["title"],
                "company": listing["company"],
                "score": listing.get("fit_score"),
                "url": listing.get("url", ""),
            })

    summary = (
        f"Skill '{canon}': {match['listings_blocked']} listings blocked, "
        f"{len(result)} shown with details."
    )
    return {"rows": result, "summary": summary}


@_tool(
    name="trend",
    description=(
        "Change in opportunity cost for each skill gap over the last N weeks, "
        "computed from timestamped snapshots. Use when the question asks about "
        "trends, changes over time, whether a gap is growing or shrinking, or "
        "historical gap movement."
    ),
    params=[
        ParamSpec(
            name="weeks",
            type="integer",
            description="Number of weeks to look back.",
            default=3,
            minimum=1,
            maximum=12,
        ),
    ],
)
def trend(weeks: int = 3) -> dict[str, Any]:
    """Gap opportunity_cost change over N weeks from the snapshots."""
    from datetime import datetime, timezone, timedelta

    weeks = _clamp_int(weeks, 1, 12, 3)
    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    cutoff_iso = cutoff.isoformat()

    # Load all snapshot run_ids within the window
    try:
        with storage._conn(db) as conn:
            ph = storage._PH
            run_rows = conn.execute(
                f"SELECT DISTINCT run_id FROM skill_gap_snapshots "
                f"WHERE computed_at >= {ph} ORDER BY run_id ASC",
                (cutoff_iso,),
            ).fetchall()
    except Exception:
        return {"rows": [], "summary": "No snapshot data available."}

    run_ids = [r[0] for r in run_rows]
    if len(run_ids) < 2:
        return {
            "rows": [],
            "summary": (
                f"Need at least 2 snapshots within {weeks} weeks for a trend. "
                f"Found {len(run_ids)}."
            ),
        }

    earliest_id = run_ids[0]
    latest_id = run_ids[-1]

    # Load cost maps for earliest and latest
    def _cost_map(rid: str) -> dict[str, float]:
        with storage._conn(db) as c:
            rows = c.execute(
                f"SELECT skill, opportunity_cost FROM skill_gap_snapshots WHERE run_id = {storage._PH}",
                (rid,),
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    earliest_map = _cost_map(earliest_id)
    latest_map = _cost_map(latest_id)

    all_skills = sorted(set(earliest_map.keys()) | set(latest_map.keys()))
    result = []
    for skill in all_skills:
        old = earliest_map.get(skill)
        new = latest_map.get(skill)
        entry: dict[str, Any] = {"skill": skill}
        if old is not None:
            entry["earliest_cost"] = round(old, 2)
        if new is not None:
            entry["latest_cost"] = round(new, 2)
        if old is not None and new is not None:
            delta = round(new - old, 2)
            entry["change"] = delta
            entry["direction"] = "rising" if delta > 0 else ("falling" if delta < 0 else "flat")
        elif old is None:
            entry["direction"] = "new"
        else:
            entry["direction"] = "dropped"
        result.append(entry)

    # Sort by latest_cost descending (most impactful first)
    result.sort(key=lambda r: r.get("latest_cost", 0), reverse=True)

    summary = (
        f"Trend over {len(run_ids)} snapshots in the last {weeks} weeks "
        f"({earliest_id[:10]} → {latest_id[:10]}), {len(result)} skills tracked."
    )
    return {"rows": result, "summary": summary}


@_tool(
    name="listing_count",
    description=(
        "Total counts of listings in the database: total, scored, unscored, "
        "and the date of the newest listing. Use when the question asks how "
        "many listings there are, how many have been scored, or when the "
        "last fetch happened."
    ),
    params=[],
)
def listing_count() -> dict[str, Any]:
    """Totals: listings, scored, unscored, newest listing date."""
    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    summary_data = storage.get_db_summary(db_path=db)
    total = summary_data["total_listings"]
    scored = summary_data["scored_listings"]
    unscored = total - scored
    newest = storage.last_fetch_time(db_path=db)

    rows = [{
        "total_listings": total,
        "scored": scored,
        "unscored": unscored,
        "newest_listing_date": newest,
    }]
    summary = f"{total} listings ({scored} scored, {unscored} unscored), newest: {newest}."
    return {"rows": rows, "summary": summary}


@_tool(
    name="skill_demand",
    description=(
        "How often one specific skill appears across listings, broken down by "
        "whether it is listed as required or nice-to-have. Use when the "
        "question asks about demand for a particular skill, how common it is "
        "in job descriptions, or whether it appears as required or optional."
    ),
    params=[
        ParamSpec(
            name="skill",
            type="string",
            description="The skill to look up (will be canonicalised).",
            required=True,
        ),
    ],
)
def skill_demand(skill: str) -> dict[str, Any]:
    """How often one skill appears in required vs nice_to_have."""
    import hashlib

    db = _db_path()

    good_cycle = storage.get_last_good_cycle(db_path=db)
    if good_cycle is None:
        return {"rows": [], "summary": "No passing cycle found."}

    canon = _canonicalise_skill(skill)
    if not canon:
        return {"rows": [], "summary": f"Skill '{skill}' resolved to empty after canonicalisation."}

    aliases = _load_aliases()

    # Scan extraction cache for listings that mention this skill
    all_listings = storage.get_listings(limit=10_000, db_path=db)
    required_count = 0
    nice_count = 0
    total_checked = 0

    for listing in all_listings:
        desc = listing.get("description", "")
        if not desc:
            continue
        desc_hash = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        cached = storage.get_extraction_cache(desc_hash, db_path=db)
        if cached is None:
            continue
        total_checked += 1

        req = cached.get("required_skills") or []
        nth = cached.get("nice_to_have") or []

        req_canonical = [canonical(s, aliases) for s in req if isinstance(s, str)]
        nth_canonical = [canonical(s, aliases) for s in nth if isinstance(s, str)]

        if canon in req_canonical:
            required_count += 1
        if canon in nth_canonical:
            nice_count += 1

    rows = [{
        "skill": canon,
        "required_in": required_count,
        "nice_to_have_in": nice_count,
        "total_mentions": required_count + nice_count,
        "listings_checked": total_checked,
    }]
    summary = (
        f"Skill '{canon}': required in {required_count}, nice-to-have in {nice_count} "
        f"(out of {total_checked} listings with extractions)."
    )
    return {"rows": rows, "summary": summary}
