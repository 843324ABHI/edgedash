"""
edgedash/query/ask.py — Two-call query pipeline (steering rules 42-45).

Call 1: ROUTE  — pick a tool and parameters from the registry.
Call 2: PHRASE — turn returned rows into prose using ONLY the data.

No SQL. No third model call. No data fabrication.

Abuse guards (public deployment):
  - Input validation: length, control chars, injection patterns
  - Global daily cap from config (default 200)
  - Session rate limiting enforced at the UI layer (app.py)
  - Every rejection logged to query_log with reason

Public API:
    ask(question: str) -> Answer
    daily_query_count(db_path: str) -> int
    check_input(question: str) -> str | None   (None = OK, str = reason)
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from edgedash import llm, storage
from edgedash.query.tools import TOOLS, get_tool_specs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_QUESTION_LEN = 300

# Patterns that indicate prompt injection.  Matched case-insensitively
# against the cleaned input.  Keep this list short and precise — false
# positives are worse than false negatives here.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(all\s+)?previous",
        r"ignore\s+(all\s+)?above",
        r"ignore\s+(all\s+)?prior",
        r"system\s+prompt",
        r"you\s+are\s+now",
        r"new\s+instructions",
        r"forget\s+(all\s+)?your",
        r"disregard\s+(all\s+)?previous",
        r"override\s+(all\s+)?instructions",
    )
]

# Control character stripper (keeps printable ASCII + common Unicode)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------------------
# Answer dataclass
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    """Result of a natural-language query against EdgeDash data."""
    text: str
    rows: list[dict[str, Any]]
    tool_used: str | None
    params: dict[str, Any]


# ---------------------------------------------------------------------------
# Schemas for the two LLM calls
# ---------------------------------------------------------------------------

_ROUTE_SCHEMA: dict = {
    "type": "object",
    "required": ["tool", "params", "confidence"],
    "properties": {
        "tool": {"type": "string"},  # validated manually; null allowed
        "params": {"type": "object"},
        "confidence": {"type": "string"},
    },
}

_PHRASE_SCHEMA: dict = {
    "type": "object",
    "required": ["answer"],
    "properties": {
        "answer": {"type": "string"},
    },
}


# ---------------------------------------------------------------------------
# Routing prompt
# ---------------------------------------------------------------------------

def _build_route_prompt(question: str) -> str:
    """
    Build the routing prompt.  The model sees:
      - the user's question
      - the registry of available tools (name, description, parameters)
      - explicit instructions to return null if nothing matches
    Nothing else.  No schema names, no SQL, no table names.
    """
    specs = get_tool_specs()
    tool_block = json.dumps(specs, indent=2)

    return f"""\
You are a query router for a career intelligence dashboard.
Your ONLY job is to pick which tool answers the user's question,
and supply valid parameters for it.

AVAILABLE TOOLS:
{tool_block}

RULES:
- Pick exactly one tool whose description matches the user's question.
- Return its name in "tool" and the parameters in "params".
- If no tool is a clear match, set "tool" to null and "params" to {{}}.
  Do NOT pick the closest tool. Do NOT guess. Return null.
- Never invent a tool name that is not in the list above.
- "confidence" must be "high" or "low".
- Return ONLY the JSON object described below — no prose.

USER QUESTION:
{question}
"""


# ---------------------------------------------------------------------------
# Phrasing prompt
# ---------------------------------------------------------------------------

def _build_phrase_prompt(
    question: str,
    rows: list[dict[str, Any]],
    summary: str,
) -> str:
    """
    Build the phrasing prompt.  The model sees:
      - the user's question
      - the rows returned by the tool (verbatim JSON)
      - the tool's summary string
    Per rule 43: it may use ONLY numbers present in the rows.
    """
    rows_json = json.dumps(rows, indent=2, default=str)

    return f"""\
You are a data narrator for a career intelligence dashboard.
Below is a user question and the data rows that answer it.

QUESTION:
{question}

DATA SUMMARY:
{summary}

DATA ROWS:
{rows_json}

RULES — STRICT:
- Write 2-3 concise sentences that answer the question.
- You may use ONLY numbers and facts present in the data rows above.
- Do NOT estimate, extrapolate, round creatively, add outside context,
  or infer any value that is not explicitly in the rows.
- If the rows are empty, say plainly that the data does not contain an
  answer to this question. Do not speculate why.
- Mention what the data covers (use the summary line above) so the user
  knows the scope, e.g. "Across 47 listings from the last 7 days…"
- Return ONLY the JSON object described below — no prose.
"""


# ---------------------------------------------------------------------------
# "Can't answer" fixed message (no model call — rule 45)
# ---------------------------------------------------------------------------

def _cant_answer_text() -> str:
    """
    Fixed message when no tool matches.  Lists what CAN be asked.
    No model call — this is deterministic text.
    """
    lines = ["I can't answer that question with the tools available.\n"]
    lines.append("Here's what you **can** ask:\n")
    for spec in get_tool_specs():
        lines.append(f"- **{spec['name']}** — {spec['description']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query logging (with rejection_reason column)
# ---------------------------------------------------------------------------

def _ensure_query_log(db_path: str) -> None:
    """Create or migrate the query_log table (idempotent)."""
    if storage._BACKEND == "postgres":
        ddl = """
            CREATE TABLE IF NOT EXISTS query_log (
                id SERIAL PRIMARY KEY,
                asked_at TEXT NOT NULL,
                question TEXT NOT NULL,
                tool_chosen TEXT,
                params_json TEXT NOT NULL,
                answerable INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                rejection_reason TEXT
            )
        """
    else:
        ddl = """
            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asked_at TEXT NOT NULL,
                question TEXT NOT NULL,
                tool_chosen TEXT,
                params_json TEXT NOT NULL,
                answerable INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                rejection_reason TEXT
            )
        """
    with storage._conn(db_path) as conn:
        conn.execute(ddl)
        # Idempotent migration: add rejection_reason if missing
        if storage._BACKEND == "postgres":
            conn.execute(
                "ALTER TABLE query_log ADD COLUMN IF NOT EXISTS rejection_reason TEXT"
            )
        else:
            try:
                conn.execute("ALTER TABLE query_log ADD COLUMN rejection_reason TEXT")
            except Exception:
                pass  # column already exists


def _log_query(
    question: str,
    tool: str | None,
    params: dict,
    answerable: bool,
    duration_ms: int,
    db_path: str,
    rejection_reason: str | None = None,
) -> None:
    """Write one row to query_log (audit trail — intentional dashboard-side write)."""
    _ensure_query_log(db_path)
    now = datetime.now(timezone.utc).isoformat()
    ph = storage._PH
    with storage._conn(db_path) as conn:
        conn.execute(
            f"INSERT INTO query_log "
            f"(asked_at, question, tool_chosen, params_json, answerable, duration_ms, rejection_reason) "
            f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
            (now, question, tool, json.dumps(params), int(answerable), duration_ms, rejection_reason),
        )


# ---------------------------------------------------------------------------
# Input guards (checked BEFORE any model call)
# ---------------------------------------------------------------------------

def check_input(question: str) -> str | None:
    """
    Validate a raw question string.

    Returns None if the input is acceptable, or a rejection reason string
    if it must be blocked.  No model call is made — this is pure Python.
    """
    if not question or not question.strip():
        return "rejected: empty input"

    if len(question) > _MAX_QUESTION_LEN:
        return f"rejected: too long ({len(question)} chars, max {_MAX_QUESTION_LEN})"

    # Check for injection patterns
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(question):
            return "rejected: suspicious input"

    return None


def _sanitise(question: str) -> str:
    """Strip control characters from raw input."""
    return _CONTROL_RE.sub("", question).strip()


# ---------------------------------------------------------------------------
# Daily cap check
# ---------------------------------------------------------------------------

def daily_query_count(db_path: str) -> int:
    """
    Count questions logged today (UTC) in query_log.

    Returns 0 if the table does not exist yet.
    """
    _ensure_query_log(db_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with storage._conn(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM query_log WHERE asked_at >= {storage._PH}",
            (today,),
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Resolve db_path
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Resolve db_path from config."""
    try:
        from edgedash.config import load_config
        return load_config().db_path
    except FileNotFoundError:
        return "edgedash.db"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ask(question: str) -> Answer:
    """
    Answer a natural-language question about EdgeDash data.

    Two LLM calls (rule 42):
      1. ROUTE — pick a tool and params
      2. PHRASE — narrate the returned rows

    If no tool matches, returns a fixed message (no model call).
    Input guards are checked BEFORE any model call.
    """
    t0 = time.monotonic()
    db = _db_path()

    # ── SANITISE ─────────────────────────────────────────────────────────
    question = _sanitise(question)

    # ── INPUT GUARDS (before any model call) ─────────────────────────────
    rejection = check_input(question)
    if rejection is not None:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, None, {}, False, duration_ms, db, rejection_reason=rejection)

        if rejection == "rejected: suspicious input":
            # Don't explain the filter — return generic can't-answer (rule 45)
            return Answer(
                text=_cant_answer_text(),
                rows=[],
                tool_used=None,
                params={},
            )

        if "empty" in rejection:
            return Answer(
                text="Please ask a question.",
                rows=[],
                tool_used=None,
                params={},
            )

        if "too long" in rejection:
            return Answer(
                text=f"Questions are limited to {_MAX_QUESTION_LEN} characters. Please shorten yours.",
                rows=[],
                tool_used=None,
                params={},
            )

        # Generic fallback
        return Answer(
            text=_cant_answer_text(),
            rows=[],
            tool_used=None,
            params={},
        )

    # ── DAILY CAP (before any model call) ────────────────────────────────
    try:
        from edgedash.config import load_config
        cap = load_config().query_daily_cap
    except FileNotFoundError:
        cap = 200

    if daily_query_count(db) >= cap:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, None, {}, False, duration_ms, db, rejection_reason="rejected: daily cap exceeded")
        return Answer(
            text="The daily question limit has been reached. Please try again tomorrow.",
            rows=[],
            tool_used=None,
            params={},
        )

    # ── CALL 1: ROUTE ────────────────────────────────────────────────────
    route_prompt = _build_route_prompt(question)
    try:
        route_result = llm.complete_json(route_prompt, _ROUTE_SCHEMA)
    except llm.LLMError as exc:
        logger.error("Route call failed: %s", exc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, None, {}, False, duration_ms, db)
        return Answer(
            text="Sorry, I couldn't process that question (routing error).",
            rows=[],
            tool_used=None,
            params={},
        )

    tool_name = route_result.get("tool")
    params = route_result.get("params") or {}

    # ── NULL tool → fixed "can't answer" message (rule 45) ───────────────
    if tool_name is None or tool_name == "null":
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, None, params, False, duration_ms, db)
        return Answer(
            text=_cant_answer_text(),
            rows=[],
            tool_used=None,
            params=params,
        )

    # ── Validate tool name against registry (hard error) ─────────────────
    if tool_name not in TOOLS:
        logger.error("Model returned unknown tool name: %r", tool_name)
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, tool_name, params, False, duration_ms, db)
        return Answer(
            text=(
                f"The router picked '{tool_name}', which is not a known tool. "
                f"This is a hard error.\n\n{_cant_answer_text()}"
            ),
            rows=[],
            tool_used=tool_name,
            params=params,
        )

    # ── EXECUTE — call the tool with validated, clamped params ───────────
    tool = TOOLS[tool_name]
    try:
        result = tool.fn(**params)
    except TypeError as exc:
        # Bad params shape — the model gave us keys the function doesn't accept
        logger.error("Tool %s execution failed: %s", tool_name, exc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        _log_query(question, tool_name, params, False, duration_ms, db)
        return Answer(
            text=f"Tool '{tool_name}' could not run with the given parameters: {exc}",
            rows=[],
            tool_used=tool_name,
            params=params,
        )

    rows: list[dict] = result.get("rows", [])
    summary: str = result.get("summary", "")

    # ── CALL 2: PHRASE — narrate the rows ────────────────────────────────
    phrase_prompt = _build_phrase_prompt(question, rows, summary)
    try:
        phrase_result = llm.complete_json(phrase_prompt, _PHRASE_SCHEMA)
        answer_text = phrase_result.get("answer", "")
    except llm.LLMError as exc:
        logger.error("Phrase call failed: %s", exc)
        # Fallback: use the tool's summary directly
        answer_text = summary if summary else "Data returned but phrasing failed."

    duration_ms = int((time.monotonic() - t0) * 1000)
    _log_query(question, tool_name, params, True, duration_ms, db)

    return Answer(
        text=answer_text,
        rows=rows,
        tool_used=tool_name,
        params=params,
    )
