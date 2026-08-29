# EdgeDash Steering Document

## Project Overview
**EdgeDash** — An autonomous AI career intelligence agent. A scheduled loop that fetches live job listings, scores them for fit against the user's profile, surfaces skill gaps, verifies its own output, and publishes a Streamlit dashboard.

---

## Architecture
> **Strict Requirement**: Do not deviate from this architectural flow without explicit user consultation.

```
Trigger (scheduled) -> Orchestrator -> Sub-agents (Fetcher, Scorer, GapAnalyzer)
                   -> Verifier -> Storage -> Dashboard (read-only)
```

- **Orchestrator**: Reads state and delegates execution; **never** fetches or scores directly.
- **Sub-agents**: Each sub-agent has exactly **one goal** and **one stop condition**.
- **Dashboard**: Read-only interface consuming processed data from Storage.

---

## Hard Rules

1. **Python 3.11+ & Standard Library First**: Add a third-party dependency only when it genuinely saves substantial real work. State the justification before adding it.
2. **Single Storage Module Interface**: ALL storage access must go through a single storage module with a thin interface. No other module may import `sqlite3` directly. Storage swap (e.g., SQLite to hosted Postgres) must be a single-file change.
3. **No Hardcoded User Data**: Never hardcode role, city, keywords, or skills profile. All user-specific parameters must live in configuration files.
4. **No Secrets in Code**: Environment variables only, loaded in exactly one centralized place.
5. **Cycle Logging**: Every agent run must write a record to a `cycle_log` table tracking:
   - What ran
   - Execution timestamp
   - Number of records touched
   - Pass/fail status
   - Retry reason (if applicable)
6. **Fail Loudly**: No bare `except: pass` or swallowed errors. Fail explicitly and visibly when an anomaly or exception occurs.
7. **Type Hints**: Mandatory type hints on every function signature. Docstrings only where intent is not immediately obvious from function/variable names.
8. **File Size Limit**: Keep files under ~150 lines. Proactively split files into separate modules before line limits are exceeded.

---

## Network & Sources

9. **Source Isolation**: Every external source lives behind a `Source` class with a uniform interface. The Fetcher never contains source-specific parsing. Adding a source must never require editing the Fetcher.
10. **Normalized Schema**: Every `Source` returns a list of normalized dicts with EXACTLY these keys: `source`, `external_id`, `title`, `company`, `location`, `url`, `description`, `posted_at`, `raw`. Missing values are `None`, never empty string, never `"N/A"`.
11. **Centralized Network Helper**: All network calls go through one helper with a timeout (10s default), explicit retry (2 attempts, exponential backoff), and a User-Agent header. No bare `requests.get` anywhere else in the codebase.
12. **Resilient Failure Scope**: A source failing must NEVER kill the cycle. Catch per-source, log the failure to `cycle_log` with status `"failed"`, and continue to the next source. One dead job board must not stop the other sources.
13. **Environment Secrets**: Secrets come from environment variables via a `.env` file that is gitignored. Never a literal key in code, never a key in `config.yaml`. If a key is missing, that source skips itself with a clear log line — it does not crash the cycle.
14. **Respect the Source**: Rate limit to at most 1 request per second per source, set a real User-Agent, and honour any documented page limits.

---

## Intelligence & Scoring

15. **Single LLM Gateway**: All LLM calls go through one module, `edgedash/llm.py`, exposing one function. The provider and model name come from config, never hardcoded. Rate limit to stay inside a free tier (default 1 request per second, max 15 per minute). No other file imports an LLM SDK.
16. **Facts-Only Extraction**: NEVER ask a model for a final score, ranking, or numeric rating. The model extracts structured facts only. All scoring arithmetic is deterministic Python in ONE function. The model never sees the scoring weights.
17. **Schema Validation on Every Response**: Every model response is validated against an explicit schema before use. A response that fails validation is retried once, then logged as a failure for THAT listing only — it must not crash the cycle or stop the remaining listings. Never `json.loads` raw model text without a validation and repair path.
18. **Idempotent Scoring with Extraction Cache**: Scoring is idempotent. Never re-score a listing that already has a score. Select only listings `WHERE score IS NULL`. Cache extraction results keyed on a hash of the job description so the same text is never sent to the model twice.
19. **Deterministic Score Reasons**: Every score carries a human-readable reason GENERATED FROM THE SCORE COMPONENTS by our code — never free text written by the model.
20. **Score Distribution Logging**: Log the score distribution (count, min, max, mean, spread) to `cycle_log` on every scoring run. A run where all scores fall within 10 points is a suspect run and must be logged as such.
21. **Configurable Batch Cap**: Cap listings scored per cycle at a configurable batch size (default 25) so a cost or rate-limit blowup is structurally impossible.

---

## Aggregate Analysis

22. **Deterministic Aggregation**: Aggregate analysis is deterministic SQL and Python. No LLM call may produce, adjust, or rank an aggregate number. A model may only SUGGEST canonical groupings for a human to approve.
23. **Canonical Skill Alias Map**: Skill names are canonicalised through an explicit alias map in `config.yaml` that I own and can read. Never auto-merge skill names by model judgement or string similarity alone.
24. **Fit-Weighted Gap Ranking**: Gap ranking is weighted by the fit score of the listing the gap came from. A gap in a listing scored 20 is worth far less than a gap in a listing scored 85. Never rank gaps by raw frequency alone.
25. **Immutable Timestamped Snapshots**: Every gap report run writes a timestamped SNAPSHOT. Never overwrite the previous report. Trend over time is a first-class output, not an afterthought.
26. **Full Drilldown Traceability**: Every aggregate number must be traceable to the rows that produced it. Any reported gap must be able to list the specific listing IDs it was computed from. No number appears in the dashboard that cannot be drilled into.
27. **Always Report Sample Size**: Report the sample size alongside every aggregate. A gap computed from 3 listings and a gap computed from 90 listings must never be presented as equally reliable.

---

## Coding Style & Workflow

- **Function Design**: Write small, unit-testable functions.
- **Python Philosophy**: Prefer plain, readable Python over complex or clever abstractions.
- **Modular Development**: Build only the specific module requested — do not scaffold unrequested components or full application structures upfront.

---

## Orchestration

28. **State-Driven Dispatch**: The Orchestrator reads system state and decides which agents to run. It never runs a fixed sequence. Skipping an agent because there is no work for it is a SUCCESSFUL outcome, not a failure.
29. **Explicit Goals and Stop Conditions**: Every delegation carries an explicit goal and an explicit stop condition (max items, max duration). A sub-agent never decides its own limits — the Orchestrator sets them.
30. **Orchestrator Does No Agent Work**: The Orchestrator never does an agent's work. It reads state, delegates, collects results, logs. No fetching, scoring, or analysis logic in the Orchestrator.
31. **Plan Before Execute**: The Orchestrator prints and logs its PLAN before executing it — which agents will run, which are skipped, and the state value that caused each decision.
32. **Fault Isolation**: One sub-agent failing does not stop the cycle. Log the failure, continue with the remaining plan, and mark the cycle partial.
33. **Single Summary Row Per Cycle**: Every cycle writes exactly one summary row: what ran, what was skipped, why, duration per agent, and the outcome.

---

## Verification

34. **Verifier Is Verdict-Only**: The Verifier judges output plausibility and NEVER repairs, rewrites, or adjusts data. It returns a verdict and a reason. The Orchestrator decides what to do about a failure.
35. **Plausibility, Not Correctness**: Verification checks plausibility, never correctness. There is no ground truth for a fit score. Checks assert properties of the output distribution and shape, not the accuracy of any single value.
36. **Bounded Retry on Failure**: A failed verification triggers at most ONE retry of the failing agent with adjusted context. After that the cycle is marked "degraded" and stops. Never retry in an unbounded loop.
37. **Structured Failure Logging**: Every verdict is logged to `cycle_log` with the check that failed and the observed value that failed it — never just "failed".
38. **Stale Verified Data Beats Fresh Unverified**: Only cycles with a passing verdict may be read by the dashboard. A failed cycle must never overwrite the last known-good data. Stale verified data always beats fresh unverified data.
39. **Thresholds in Config**: Verification thresholds live in `config.yaml`, not in code, and every threshold has a comment saying what failure it is designed to catch.

---

## Natural Language Queries

40. **No Text-to-SQL**: NEVER generate SQL from a model. No text-to-SQL, ever, in any form. The model selects from a fixed registry of parameterised query functions that I wrote. It never composes a query.
41. **Typed, Read-Only Query Tools**: Every query tool is read-only, parameterised, and takes typed parameters that are validated and clamped to a safe range before execution. A model-supplied parameter is untrusted input.
42. **Two-Call Pattern Only**: The model appears exactly twice per question: once to ROUTE (pick a tool and its parameters) and once to PHRASE (turn returned rows into prose). It never touches the database in either call.
43. **Data-Faithful Phrasing**: The phrasing call may use ONLY the numbers present in the rows it was given. It must not estimate, extrapolate, add outside context, or infer a value that is not in the data. If the rows are empty it must say so plainly.
44. **Mandatory Row Display**: Every answer displays the underlying rows alongside it. No prose answer appears without the data that produced it.
45. **No Guessing**: If no tool matches the question, say so and list what CAN be asked. Never guess at the closest tool and never answer from general knowledge.
46. **Last Passing Cycle Only**: Query tools read from the last passing cycle only, per rule 38.

---

## Deployment

47. **Ephemeral Filesystem**: Never rely on the local filesystem for anything that must survive a restart. Hosting filesystems are ephemeral. All persistent state is in the hosted database.
48. **Single Secret Source**: Every secret comes from an environment variable read in one place. No secret is ever committed, printed, logged, or shown in an error message or traceback.
49. **Process Separation**: The scheduled job and the dashboard are separate processes that share only the database. The dashboard never runs a cycle; the scheduler never serves a page.
50. **Graceful Empty-DB Start**: The deployed app must start and render even when the database is empty, unreachable, or mid-migration. It shows a clear status message instead of a stack trace. A stranger must never see a traceback.
51. **Idempotent Scheduler**: The scheduled job is idempotent and safe to run twice. It must have a hard timeout and stay inside free-tier limits.
