from datetime import datetime, timezone

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.sources.base import SOURCES
from edgedash.sources.http import SourceError
import edgedash.sources.arbeitnow  # noqa: F401 — registers ArbeitnowSource into SOURCES
import edgedash.storage as storage


class Fetcher:
    name: str = "Fetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions = StopConditions(),
    ) -> AgentResult:
        # Honour stop conditions set by the Orchestrator (rule 29)
        max_pages: int | None = stop_conditions.max_pages
        max_listings: int | None = stop_conditions.max_items

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        source_summaries: list[str] = []
        all_rows: list[dict] = []

        for source_name in config.sources:
            if source_name not in SOURCES:
                msg = f"source '{source_name}' not found in registry - skipping"
                print(f"  [Fetcher] WARNING: {msg}")
                storage.log_cycle(
                    agent=f"Fetcher/{source_name}",
                    started_at=fetched_at,
                    finished_at=fetched_at,
                    records_touched=0,
                    status="failed",
                    notes=msg,
                    db_path=db_path,
                )
                source_summaries.append(f"{source_name}: FAILED (not registered)")
                continue

            source = SOURCES[source_name]()
            t0 = datetime.now(timezone.utc)

            try:
                rows = source.fetch(config, max_pages=max_pages)
            except (SourceError, Exception) as exc:
                t1 = datetime.now(timezone.utc)
                msg = f"{type(exc).__name__}: {exc}"
                print(f"  [Fetcher] WARNING: source '{source_name}' failed — {msg}")
                storage.log_cycle(
                    agent=f"Fetcher/{source_name}",
                    started_at=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    finished_at=t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    records_touched=0,
                    status="failed",
                    notes=msg,
                    db_path=db_path,
                )
                # Extract a short label for the notes line
                short_err = str(exc).split("\n")[0][:60]
                source_summaries.append(f"{source_name}: FAILED ({short_err})")
                continue

            t1 = datetime.now(timezone.utc)

            # Stamp the listing id using storage's canonical hash (no second impl)
            for row in rows:
                if not row.get("id"):
                    row["id"] = storage.generate_listing_id(
                        row.get("source", ""), row.get("url", "")
                    )
                row.setdefault("fetched_at", fetched_at)

            storage.log_cycle(
                agent=f"Fetcher/{source_name}",
                started_at=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                finished_at=t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
                records_touched=len(rows),
                status="ok",
                notes=f"fetched {len(rows)} rows",
                db_path=db_path,
            )
            all_rows.extend(rows)
            source_summaries.append(f"{source_name}: {len(rows)} rows")

        # Apply max_listings cap across all sources combined (rule 29)
        if max_listings is not None and len(all_rows) > max_listings:
            all_rows = all_rows[:max_listings]

        # Bulk upsert all rows from all sources; track how many are genuinely new
        new_count = storage.upsert_listings(all_rows, db_path) if all_rows else 0

        # Annotate each source summary with its new-row share where unambiguous
        if len(config.sources) == 1 and all_rows:
            source_summaries = [f"{source_summaries[0]} ({new_count} new)"]

        notes = " | ".join(source_summaries) if source_summaries else "no sources configured"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=notes,
        )
