import time
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config
from edgedash.sources.base import Source, register
from edgedash.sources.http import SourceError, get_json

_API_URL = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5
_MIN_RESULTS_BEFORE_RELAX = 5
_RATE_LIMIT_SECONDS = 1.0


def _matches_keywords(job: dict[str, Any], keywords: list[str]) -> bool:
    search_text = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        " ".join(job.get("tags", [])),
    ]).lower()
    return any(kw.lower() in search_text for kw in keywords)


def _matches_city(job: dict[str, Any], city: str) -> bool:
    location = (job.get("location") or "").lower()
    return city.lower() in location or job.get("remote", False)


def _to_posted_at(created_at: int | None) -> str | None:
    if not created_at:
        return None
    return datetime.fromtimestamp(created_at, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _normalize(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "arbeitnow",
        "external_id": job.get("slug") or None,
        "title": job.get("title") or None,
        "company": job.get("company_name") or None,
        "location": job.get("location") or None,
        "url": job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at": _to_posted_at(job.get("created_at")),
        "raw": job,
    }


@register
class ArbeitnowSource(Source):
    name = "arbeitnow"

    def fetch(self, config: Config, max_pages: int | None = None) -> list[dict[str, Any]]:
        page_cap = max_pages if max_pages is not None else _MAX_PAGES
        raw_jobs: list[dict[str, Any]] = []

        for page in range(1, page_cap + 1):
            try:
                data = get_json(_API_URL, params={"page": page})
            except SourceError as exc:
                print(f"  [arbeitnow] page {page} failed: {exc}")
                break

            page_jobs: list[dict[str, Any]] = data.get("data", [])
            if not page_jobs:
                break

            # Stop paging if this page has no keyword matches at all
            page_matches = [j for j in page_jobs if _matches_keywords(j, config.keywords)]
            raw_jobs.extend(page_jobs)

            if not page_matches:
                print(f"  [arbeitnow] page {page}: no keyword matches, stopping pagination")
                break

            if page < page_cap:
                time.sleep(_RATE_LIMIT_SECONDS)

        print(f"  [arbeitnow] fetched {len(raw_jobs)} raw listings across pages")

        # Filter by keyword first
        keyword_matches = [j for j in raw_jobs if _matches_keywords(j, config.keywords)]

        # Then by city; relax if too few results
        city_matches = [j for j in keyword_matches if _matches_city(j, config.target_city)]

        if len(city_matches) < _MIN_RESULTS_BEFORE_RELAX:
            print(
                f"  [arbeitnow] only {len(city_matches)} results after city filter "
                f"(threshold={_MIN_RESULTS_BEFORE_RELAX}) — "
                f"relaxing location filter, showing all keyword matches instead"
            )
            filtered = keyword_matches
        else:
            filtered = city_matches

        print(f"  [arbeitnow] {len(filtered)} listings survived filtering")
        return [_normalize(j) for j in filtered]
