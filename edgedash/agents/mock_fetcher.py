from datetime import datetime, timezone

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
import edgedash.storage as storage

# ---------------------------------------------------------------------------
# 12 fake listings. IDs for entries 0-3 are hardcoded (stable across runs)
# so that deduplication is provably observable on a second run.
# ---------------------------------------------------------------------------

_STABLE_IDS = [
    "stable-id-aaa0000000000000000000000000001",
    "stable-id-aaa0000000000000000000000000002",
    "stable-id-aaa0000000000000000000000000003",
    "stable-id-aaa0000000000000000000000000004",
]


def _listings(role: str, city: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()

    fixed = [
        {
            "id": _STABLE_IDS[0],
            "title": f"Junior {role}",
            "company": "InfySpark Analytics",
            "location": city,
            "url": "https://jobs.example.com/infyspark/jr-da-001",
            "description": (
                "Work with SQL and Python to build reporting pipelines. "
                "Tableau dashboards for business stakeholders. "
                "0-2 years experience. Team of 5."
            ),
            "source": "mock",
            "posted_at": "2026-08-05T09:00:00Z",
            "fetched_at": now,
        },
        {
            "id": _STABLE_IDS[1],
            "title": f"Senior {role}",
            "company": "Flipkart Commerce",
            "location": city,
            "url": "https://jobs.example.com/flipkart/sr-da-002",
            "description": (
                "Lead analytics for the supply-chain vertical. "
                "Expert in BigQuery, dbt, and Airflow. "
                "5+ years, Python and Spark required."
            ),
            "source": "mock",
            "posted_at": "2026-08-06T11:30:00Z",
            "fetched_at": now,
        },
        {
            "id": _STABLE_IDS[2],
            "title": f"{role} – Growth",
            "company": "Razorpay FinTech",
            "location": city,
            "url": "https://jobs.example.com/razorpay/da-growth-003",
            "description": (
                "Drive A/B testing and funnel analysis for the payments product. "
                "Strong statistics, SQL, and Excel skills expected. "
                "Mix of Python scripting and BI tools."
            ),
            "source": "mock",
            "posted_at": "2026-08-04T08:15:00Z",
            "fetched_at": now,
        },
        {
            "id": _STABLE_IDS[3],
            "title": f"{role} – Marketing",
            "company": "Swiggy Delivery",
            "location": city,
            "url": "https://jobs.example.com/swiggy/da-mkt-004",
            "description": (
                "Segment customers, build cohort analyses, and own the weekly "
                "growth report. SQL heavy. Google Sheets, PowerBI. "
                "1-3 years experience in consumer internet."
            ),
            "source": "mock",
            "posted_at": "2026-08-06T14:00:00Z",
            "fetched_at": now,
        },
    ]

    varied = [
        {
            "title": f"{role} – Risk & Compliance",
            "company": "CRED Financial",
            "location": city,
            "url": "https://jobs.example.com/cred/da-risk-005",
            "description": (
                "Analyse credit risk signals using Python and Pandas. "
                "Build alerting pipelines in Airflow. "
                "Familiarity with ML model monitoring a plus."
            ),
            "source": "mock",
            "posted_at": "2026-08-07T07:45:00Z",
            "fetched_at": now,
        },
        {
            "title": f"Lead {role}",
            "company": "Meesho E-Commerce",
            "location": city,
            "url": "https://jobs.example.com/meesho/lead-da-006",
            "description": (
                "Define the analytics roadmap for Tier-2 seller acquisition. "
                "Advanced SQL, strong Python, and experience presenting to C-suite. "
                "7+ years, people management expected."
            ),
            "source": "mock",
            "posted_at": "2026-08-03T16:20:00Z",
            "fetched_at": now,
        },
        {
            "title": f"{role} – Product",
            "company": "Ola Electric",
            "location": city,
            "url": "https://jobs.example.com/ola/da-product-007",
            "description": (
                "Instrument new EV features, own retention metrics, "
                "and drive experimentation culture. "
                "Python, Amplitude, and Mixpanel experience desired."
            ),
            "source": "mock",
            "posted_at": "2026-08-05T10:00:00Z",
            "fetched_at": now,
        },
        {
            "title": f"Associate {role}",
            "company": "Zepto Hyperlocal",
            "location": city,
            "url": "https://jobs.example.com/zepto/assoc-da-008",
            "description": (
                "Support ops analytics — daily dashboards, ad-hoc SQL queries, "
                "and Excel-based reporting. "
                "Fresh graduates or 1 year experience welcome."
            ),
            "source": "mock",
            "posted_at": "2026-08-07T06:30:00Z",
            "fetched_at": now,
        },
        {
            "title": f"{role} – Healthcare",
            "company": "Practo Health",
            "location": city,
            "url": "https://jobs.example.com/practo/da-health-009",
            "description": (
                "Patient journey analytics using Python and Power BI. "
                "Work with HIPAA-adjacent data; strong data governance required. "
                "3-5 years in healthcare or pharma analytics preferred."
            ),
            "source": "mock",
            "posted_at": "2026-08-04T13:00:00Z",
            "fetched_at": now,
        },
        {
            "title": f"Staff {role}",
            "company": "PhonePe Payments",
            "location": city,
            "url": "https://jobs.example.com/phonepe/staff-da-010",
            "description": (
                "Define metrics strategy across the payments super-app. "
                "Spark, Hive, and dbt at scale. "
                "10+ years; prior fintech experience required."
            ),
            "source": "mock",
            "posted_at": "2026-08-02T09:00:00Z",
            "fetched_at": now,
        },
        {
            "title": f"{role} – Supply Chain",
            "company": "Amazon India",
            "location": city,
            "url": "https://jobs.example.com/amazon/da-sc-011",
            "description": (
                "Inventory and fulfilment analytics. "
                "SQL and Python essential; R or Julia a bonus. "
                "2-4 years in logistics or operations research."
            ),
            "source": "mock",
            "posted_at": "2026-08-06T08:00:00Z",
            "fetched_at": now,
        },
        {
            "title": f"Data Analyst – Ads",
            "company": "Google India",
            "location": city,
            "url": "https://jobs.example.com/google/da-ads-012",
            "description": (
                "Measure ad effectiveness and incrementality for South Asia. "
                "BigQuery, Looker, and Python. "
                "Causal inference or experimentation background strongly preferred."
            ),
            "source": "mock",
            "posted_at": "2026-08-07T05:00:00Z",
            "fetched_at": now,
        },
    ]

    # Stable rows carry their IDs; varied rows get auto-generated IDs in storage
    return fixed + varied


class MockFetcher:
    name: str = "MockFetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions = StopConditions(),
    ) -> AgentResult:
        rows = _listings(config.target_role, config.target_city)

        # Honour max_items cap set by the Orchestrator (rule 29)
        if stop_conditions.max_items is not None:
            rows = rows[: stop_conditions.max_items]

        new_count = storage.upsert_listings(rows, db_path)
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=f"Offered {len(rows)} listings; {new_count} were genuinely new.",
        )
