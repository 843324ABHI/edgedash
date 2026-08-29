"""
app.py — EdgeDash read-only activity dashboard.

Reads through edgedash.storage ONLY. Never writes. Never runs a cycle.

Per rule 38, data panels show the last PASSING cycle only.
The activity log is the exception — it shows ALL cycles including
failures, because the failures are the point of that panel.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="EdgeDash",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Logging — server-side only, never shown to visitors
# ---------------------------------------------------------------------------
import logging as _logging
_logger = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSS — minimal, functional
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Merriweather&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=Mea+Culpa&display=swap');

    * {
        font-family: 'Merriweather', serif !important;
        font-size: 12px !important;
    }

    /* Tighter top padding */
    .block-container { padding-top: 1.2rem; }

    /* Status pill helpers */
    .pill {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: .03em;
    }
    .pill-ok      { background: #1a3a2a; color: #4ade80; border: 1px solid #166534; }
    .pill-partial { background: #3a2a10; color: #fbbf24; border: 1px solid #92400e; }
    .pill-degraded{ background: #3a1010; color: #f87171; border: 1px solid #991b1b; }
    .pill-none    { background: #1e293b; color: #94a3b8; border: 1px solid #334155; }

    /* Stale-data warning banner */
    .stale-banner {
        background: #2d1b00;
        border: 1px solid #b45309;
        border-left: 4px solid #f59e0b;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 1rem;
        color: #fde68a;
        font-size: 0.9rem;
    }

    /* Activity log row colouring via st.dataframe isn't possible —
       we build an HTML table instead */
    .log-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    .log-table th {
        background: #0f172a;
        color: #94a3b8;
        padding: 6px 10px;
        text-align: left;
        border-bottom: 1px solid #1e293b;
        white-space: nowrap;
    }
    .log-table td {
        padding: 6px 10px;
        border-bottom: 1px solid #1e293b;
        vertical-align: top;
        color: #e2e8f0;
    }
    .row-complete  { background: transparent; }
    .row-partial   { background: #1c180a; }
    .row-degraded  { background: #1a0a0a; }
    .row-none      { background: #0d1117; }
    .fail-detail   { color: #f87171; font-size: 0.78rem; margin-top: 3px; }
    .skip-detail   { color: #64748b; font-size: 0.78rem; }

    /* Score bar */
    .score-bar-wrap { background: #1e293b; border-radius: 4px; height: 8px;
                      width: 100%; margin-top: 4px; }
    .score-bar      { background: #4ade80; border-radius: 4px; height: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Config / DB path
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("config.yaml")
DB_PATH = "edgedash.db"

try:
    import yaml  # type: ignore
    with open(CONFIG_PATH) as f:
        _cfg = yaml.safe_load(f) or {}
    DB_PATH = str(_cfg.get("db_path", "edgedash.db"))
except Exception:
    pass  # fall back to default

# ---------------------------------------------------------------------------
# Database reachability guard (rule 50)
# ---------------------------------------------------------------------------

_DB_OK = True
_DB_ERROR = ""

try:
    import edgedash.storage as _storage_probe
    # Attempt a lightweight connection to verify the DB is reachable
    with _storage_probe._conn(DB_PATH) as _test_conn:
        _test_conn.cursor().execute("SELECT 1")
except Exception as _exc:
    _DB_OK = False
    _DB_ERROR = str(_exc)
    _logger.error("Database unreachable at startup: %s", _exc, exc_info=True)

if not _DB_OK:
    st.markdown(
        "<div style='padding: 20px 0; text-align: center;'>"
        "<h2 style='font-family: \"Mea Culpa\", cursive !important; "
        "font-size: 32px !important; color: yellow !important; "
        "font-weight: normal !important;'>⚡ EdgeDash</h2></div>",
        unsafe_allow_html=True,
    )
    st.error(
        "**Database not configured.**\n\n"
        "EdgeDash needs a database connection to display data. "
        "Set the `DATABASE_URL` environment variable in your deployment settings, "
        "or ensure the local SQLite file exists.\n\n"
        "If you just deployed, run the scheduler once to populate the database."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Cached reads — short TTL so the page feels live without hammering SQLite
# ---------------------------------------------------------------------------

TTL = 30  # seconds


@st.cache_data(ttl=TTL)
def _load_summary() -> dict:
    try:
        import edgedash.storage as s
        return s.get_db_summary(DB_PATH)
    except Exception:
        return {"total_listings": 0, "scored_listings": 0}


@st.cache_data(ttl=TTL)
def _load_last_good_cycle() -> dict | None:
    try:
        import edgedash.storage as s
        return s.get_last_good_cycle(DB_PATH)
    except Exception:
        return None


@st.cache_data(ttl=TTL)
def _load_latest_cycle() -> dict | None:
    try:
        import edgedash.storage as s
        return s.last_cycle_row(DB_PATH)
    except Exception:
        return None


@st.cache_data(ttl=TTL)
def _load_recent_cycles(limit: int = 30) -> list[dict]:
    try:
        import edgedash.storage as s
        return s.get_recent_cycles(limit=limit, db_path=DB_PATH)
    except Exception:
        return []


@st.cache_data(ttl=TTL)
def _load_scored_listings(limit: int = 10) -> list[dict]:
    try:
        import edgedash.storage as s
        return s.get_scored_listings(limit=limit, db_path=DB_PATH)
    except Exception:
        return []


@st.cache_data(ttl=TTL)
def _load_top_gaps(limit: int = 10) -> list[dict]:
    try:
        import edgedash.storage as s
        return s.get_top_gaps(limit=limit, db_path=DB_PATH)
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_ts(ts: str | None, fallback: str = "—") -> str:
    """ISO-8601 → human-readable local-ish string."""
    if not ts:
        return fallback
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ts


def _age_str(ts: str | None) -> str:
    """Return 'X h ago' / 'X d ago' relative to now."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = (now - dt).total_seconds()
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{secs / 3600:.1f}h ago"
        return f"{secs / 86400:.1f}d ago"
    except Exception:
        return ""


def _parse_notes(notes: str) -> dict:
    """
    Extract structured fields from the Orchestrator cycle_log notes string.

    Expected shape (built by _build_summary_notes in orchestrator.py):
      outcome=complete | verification=VERDICT: pass ... | retry_count=1 |
      Fetcher:status=ok,records=42,1.2s | Scorer:status=ok,...
    """
    result: dict = {
        "verdict": None,     # "pass" | "fail" | None
        "failed_checks": [], # list of str "name observed=X threshold=Y"
        "retry_count": 0,
        "agents_ran": [],    # list of str "AgentName (status, N records, Xms)"
        "agents_skipped": [],# list of str "AgentName (reason)"
        "duration_ms": None,
    }
    if not notes:
        return result

    for part in notes.split(" | "):
        part = part.strip()
        if part.startswith("verification="):
            v = part[len("verification="):]
            result["verdict"] = "pass" if "VERDICT: pass" in v else "fail"
            # Extract individual check failures: "name observed=X threshold=Y"
            # They follow " | " inside the verdict string
            for chunk in v.split(" | "):
                chunk = chunk.strip()
                if "observed=" in chunk and "threshold=" in chunk:
                    result["failed_checks"].append(chunk)
        elif part.startswith("retry_count="):
            try:
                result["retry_count"] = int(part.split("=", 1)[1])
            except Exception:
                pass
        elif re.match(r"[A-Z][a-zA-Z]+:status=", part):
            agent_name = part.split(":")[0]
            m = re.search(r"status=(\w+),records=(\d+),(\S+)", part)
            if m:
                result["agents_ran"].append(
                    f"{agent_name} ({m.group(1)}, {m.group(2)} rec, {m.group(3)})"
                )
        elif "skipped(" in part:
            m = re.match(r"(\w+):skipped\((.+)\)", part)
            if m:
                result["agents_skipped"].append(f"{m.group(1)} — {m.group(2)}")

    return result


def _pill(status: str) -> str:
    cls = {
        "complete": "pill-ok",
        "partial":  "pill-partial",
        "degraded": "pill-degraded",
    }.get(status, "pill-none")
    label = {
        "complete": "✓ complete",
        "partial":  "⚠ partial",
        "degraded": "✗ degraded",
    }.get(status, status)
    return f'<span class="pill {cls}">{label}</span>'


def _row_class(status: str) -> str:
    return {
        "complete": "row-complete",
        "partial":  "row-partial",
        "degraded": "row-degraded",
    }.get(status, "row-none")


# ---------------------------------------------------------------------------
# ── SECTION 1: Header strip
# ---------------------------------------------------------------------------

try:
    summary      = _load_summary()
    last_good    = _load_last_good_cycle()
    latest_cycle = _load_latest_cycle()

    st.markdown(
        "<div style='padding: 20px 0; min-height: 90px; display: flex; justify-content: center; align-items: center;'><h2 style='text-align: center; font-family: \"Mea Culpa\", cursive !important; font-size: 32px !important; color: yellow !important; font-weight: normal !important; line-height: 1.5 !important; margin: 0;'>⚡ EdgeDash</h2></div>",
        unsafe_allow_html=True,
    )

    # ── Status line (Rule 50: nested try-except so health checks never crash page) ──
    health_color = "#94a3b8"  # Neutral Grey
    health_text = "Status: Unknown"

    try:
        from edgedash.health import check_health
        health_info = check_health(DB_PATH)
        res = health_info["results"]

        last_3_failed = not res["consecutive_failures"]["ok"]

        last_good_dt = None
        if last_good and last_good.get("finished_at"):
            last_good_dt = datetime.fromisoformat(last_good["finished_at"].replace("Z", "+00:00"))

        is_stale_24h = True
        if last_good_dt:
            age = datetime.now(timezone.utc) - last_good_dt
            if age <= timedelta(hours=24):
                is_stale_24h = False

        if last_3_failed:
            health_color = "#ef4444"  # Red
            health_text = "Status: Unhealthy — Last 3 verification cycles failed"
        elif is_stale_24h:
            health_color = "#f59e0b"  # Amber
            health_text = "Status: Stale — No successful cycle in the last 24h"
        else:
            health_color = "#10b981"  # Green
            health_text = "Status: Healthy — Data is live"
    except Exception as _health_exc:
        _logger.exception("Health check reporting failed in app.py")
        health_color = "#94a3b8"
        health_text = "Status: Unknown"

    st.markdown(
        f"<div style='text-align: center; margin-bottom: 20px; font-size: 0.9rem;'>"
        f"<span style='color: {health_color}; font-size: 1.2rem; vertical-align: middle; margin-right: 6px;'>●</span>"
        f"<span style='color: #e2e8f0; font-weight: 600;'>{health_text}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Empty-database message (rule 50)
    if summary["total_listings"] == 0 and not latest_cycle:
        st.info(
            "🚀 **No data yet.** The first scheduled cycle will populate this dashboard. "
            "Run `python run_cycle.py` or wait for the scheduler."
        )

    # Stale-data warning — shown when the newest cycle is NOT passing
    elif latest_cycle and latest_cycle.get("status") not in ("complete",):
        good_ts  = _fmt_ts(last_good["finished_at"]) if last_good else "never"
        st.markdown(
            textwrap.dedent(
                f"""<div class="stale-banner">
                ⚠ <strong>The most recent cycle did not pass verification</strong>
                (status: <strong>{latest_cycle.get("status", "unknown")}</strong>).
                Data below is from the last verified cycle — <strong>{good_ts}</strong>.
                Fresh unverified data is withheld (rule 38).
                </div>"""
            ),
            unsafe_allow_html=True,
        )
    elif not latest_cycle:
        st.markdown(
            '<div class="stale-banner">ℹ No cycles have run yet.</div>',
            unsafe_allow_html=True,
        )

    # Header metric strip
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        good_ts_display = _fmt_ts(last_good["finished_at"]) if last_good else "—"
        good_age        = _age_str(last_good["finished_at"]) if last_good else ""
        st.metric("Last Good Cycle", good_ts_display, good_age or None)

    with col2:
        st.metric("Total Listings", f"{summary['total_listings']:,}")

    with col3:
        st.metric("Scored", f"{summary['scored_listings']:,}")

    with col4:
        if latest_cycle:
            status = latest_cycle.get("status", "—")
            parsed = _parse_notes(latest_cycle.get("notes", ""))
            verdict = parsed["verdict"]
            label   = "✓ PASS" if verdict == "pass" else ("✗ FAIL" if verdict == "fail" else status)
            st.metric("Verdict", label)
        else:
            st.metric("Verdict", "—")

except Exception as _panel_exc:
    _logger.exception("Header panel failed")
    st.warning("⚠ The header panel could not load. The team has been notified.")

st.divider()

# ---------------------------------------------------------------------------
# ── SECTION 2: Agent activity log (all statuses, most recent 30)
# ---------------------------------------------------------------------------

try:
    st.markdown("### 📋 Agent Activity Log")
    st.caption("Most recent 30 orchestrator cycles — failed and degraded rows are highlighted.")

    cycles = _load_recent_cycles(30)

    if not cycles:
        st.info("No cycles recorded yet. Run `python run_cycle.py` to start.")
    else:
        rows_html = ""
        for cyc in cycles:
            status   = cyc.get("status", "")
            notes    = cyc.get("notes", "")
            parsed   = _parse_notes(notes)
            ts       = _fmt_ts(cyc.get("finished_at"))
            age      = _age_str(cyc.get("finished_at"))
            rc       = cyc.get("records_touched", 0)
            row_cls  = _row_class(status)

            # Verdict cell
            verdict = parsed["verdict"]
            if verdict == "pass":
                verdict_cell = '<span style="color:#4ade80">✓ pass</span>'
            elif verdict == "fail":
                verdict_cell = '<span style="color:#f87171">✗ fail</span>'
            else:
                verdict_cell = '<span style="color:#94a3b8">—</span>'

            # Failed checks (observed value always present per rule 37)
            fail_html = ""
            for fc in parsed["failed_checks"]:
                fail_html += f'<div class="fail-detail">↳ {fc}</div>'

            # Agents ran / skipped
            agents_text = "<br>".join(parsed["agents_ran"]) or "—"
            skip_text   = ""
            if parsed["agents_skipped"]:
                skip_text = "<br>".join(
                    f'<span class="skip-detail">skip: {s}</span>'
                    for s in parsed["agents_skipped"]
                )

            retry_cell = str(parsed["retry_count"]) if parsed["retry_count"] else "—"

            rows_html += f"""
            <tr class="{row_cls}">
              <td style="white-space:nowrap">{ts}<br><span style="color:#64748b;font-size:.75rem">{age}</span></td>
              <td>{_pill(status)}</td>
              <td>{verdict_cell}{fail_html}</td>
              <td style="max-width:280px">{agents_text}<br>{skip_text}</td>
              <td style="text-align:center">{retry_cell}</td>
            </tr>
            """

        table_html = f"""
        <table class="log-table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Outcome</th>
              <th>Verdict / Failed Check (observed)</th>
              <th>Agents</th>
              <th>Retries</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
        """
        st.markdown(textwrap.dedent(table_html), unsafe_allow_html=True)

except Exception as _panel_exc:
    _logger.exception("Activity log panel failed")
    st.warning("⚠ The activity log could not load.")

st.divider()

# ---------------------------------------------------------------------------
# ── SECTION 3: Top listings + top gaps (compact, side by side)
# ---------------------------------------------------------------------------

try:
    col_listings, col_gaps = st.columns(2)

    # -- Top 10 scored listings --------------------------------------------------
    with col_listings:
        st.markdown("### 🏆 Top 10 Scored Listings")

        listings = _load_scored_listings(10)
        if not listings:
            st.info("No scored listings yet.")
        else:
            for row in listings:
                score   = row.get("fit_score", 0) or 0
                title   = row.get("title", "Untitled")
                company = row.get("company", "")
                reason  = row.get("fit_reason", "")
                url     = row.get("url", "")
                pct     = min(max(int(score), 0), 100)

                score_color = (
                    "#4ade80" if score >= 75 else
                    "#fbbf24" if score >= 50 else
                    "#f87171"
                )

                with st.container():
                    title_md = f"[{title}]({url})" if url else title
                    st.markdown(
                        f"**{title_md}** &nbsp; <span style='color:{score_color};"
                        f"font-weight:700;font-size:1.05rem'>{score}</span> &nbsp;"
                        f"<span style='color:#64748b;font-size:.85rem'>{company}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"<div class='score-bar-wrap'>"
                        f"<div class='score-bar' style='width:{pct}%;background:{score_color}'></div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    if reason:
                        st.caption(reason[:180])
                    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

    # -- Top 10 skill gaps -------------------------------------------------------
    with col_gaps:
        st.markdown("### 🔍 Top 10 Skill Gaps")
        st.caption("Ranked by fit-weighted opportunity cost (rule 24). Sample size shown.")

        gaps = _load_top_gaps(10)
        if not gaps:
            st.info("No gap analysis yet.")
        else:
            for gap in gaps:
                skill     = gap.get("skill", "?")
                rank      = gap.get("rank", "?")
                cost      = gap.get("opportunity_cost", 0.0)
                n         = gap.get("sample_n", 0)
                low_conf  = bool(gap.get("low_confidence", 0))
                top_score = gap.get("top_score", 0)
                blocked   = gap.get("listings_blocked", 0)

                conf_badge = (
                    " ⚠ low-confidence" if low_conf else ""
                )
                conf_color = "#f59e0b" if low_conf else "#64748b"

                # Cost bar — normalise across the visible set
                max_cost = max((g.get("opportunity_cost", 0) for g in gaps), default=1) or 1
                bar_pct  = int((cost / max_cost) * 100)

                st.markdown(
                    f"**#{rank} {skill.title()}**"
                    f"<span style='color:{conf_color};font-size:.78rem'>{conf_badge}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div class='score-bar-wrap'>"
                    f"<div class='score-bar' style='width:{bar_pct}%;background:#818cf8'></div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"cost {cost:.2f} · {blocked} listing(s) · top score {top_score} · n={n}"
                )
                st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)

except Exception as _panel_exc:
    _logger.exception("Listings/gaps panel failed")
    st.warning("⚠ The listings and gaps panel could not load.")

# ---------------------------------------------------------------------------
# ── SECTION 4: Ask your data (rules 42-45 + abuse guards)
# ---------------------------------------------------------------------------

st.divider()

# ── Abuse-guard helpers ──────────────────────────────────────────────────

_SESSION_RATE_LIMIT = 10       # max questions per window
_SESSION_RATE_WINDOW = 10 * 60  # 10 minutes in seconds


def _session_rate_ok() -> tuple[bool, int]:
    """
    Check the per-session rate limit.

    Returns (allowed: bool, wait_seconds: int).
    Tracks timestamps in st.session_state so it survives reruns but
    resets on a new browser tab / session.
    """
    import time as _time

    now = _time.time()
    key = "_ask_timestamps"
    if key not in st.session_state:
        st.session_state[key] = []

    # Purge timestamps outside the window
    cutoff = now - _SESSION_RATE_WINDOW
    st.session_state[key] = [t for t in st.session_state[key] if t > cutoff]

    if len(st.session_state[key]) >= _SESSION_RATE_LIMIT:
        oldest = min(st.session_state[key])
        wait = int(oldest + _SESSION_RATE_WINDOW - now) + 1
        return False, max(wait, 1)

    return True, 0


def _record_session_question() -> None:
    """Record a timestamp for the current question."""
    import time as _time
    st.session_state.setdefault("_ask_timestamps", []).append(_time.time())


try:
    st.markdown("### 💬 Ask Your Data")
    st.caption(
        "Ask a question in plain English. The system picks a read-only query tool, "
        "runs it, and narrates the result. Every answer shows the underlying rows (rule 44)."
    )

    # ── Daily cap check ──────────────────────────────────────────────────────

    _daily_cap_exceeded = False
    try:
        from edgedash.query.ask import daily_query_count
        _today_count = daily_query_count(DB_PATH)
        try:
            from edgedash.config import load_config as _lc
            _cap = _lc().query_daily_cap
        except Exception:
            _cap = 200
        _daily_cap_exceeded = _today_count >= _cap
    except Exception:
        _daily_cap_exceeded = False

    if _daily_cap_exceeded:
        st.info(
            "📊 The daily question limit has been reached. "
            "The ask box is paused until midnight UTC. "
            "All dashboard data is still available below."
        )

    # ── UI: example buttons + text input ─────────────────────────────────────

    _EXAMPLES = [
        "Which companies are hiring this week?",
        "What are my top 5 skill gaps?",
        "How many listings have been scored?",
    ]

    # Show buttons and input even when capped, but disable execution
    example_cols = st.columns(len(_EXAMPLES))
    for i, example in enumerate(_EXAMPLES):
        with example_cols[i]:
            if st.button(
                example,
                key=f"example_{i}",
                width="stretch",
                disabled=_daily_cap_exceeded,
            ):
                st.session_state["ask_input"] = example

    ask_input = st.text_input(
        "Your question",
        value=st.session_state.get("ask_input", ""),
        placeholder="e.g. Which companies posted the most jobs recently?",
        key="ask_question_input",
        label_visibility="collapsed",
        max_chars=300,
        disabled=_daily_cap_exceeded,
    )

    # ── Execute query (with all guards) ─────────────────────────────────────

    if ask_input and ask_input.strip() and not _daily_cap_exceeded:

        # Guard 1: session rate limit (checked before any model call)
        rate_ok, wait_secs = _session_rate_ok()
        if not rate_ok:
            mins = wait_secs // 60
            secs = wait_secs % 60
            wait_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            st.warning(
                f"⏳ You've asked {_SESSION_RATE_LIMIT} questions in the last "
                f"{_SESSION_RATE_WINDOW // 60} minutes. "
                f"Please wait **{wait_str}** before asking again."
            )
        else:
            # Guard 2: input validation (checked before any model call)
            from edgedash.query.ask import check_input
            rejection = check_input(ask_input)

            if rejection and "too long" in rejection:
                st.warning(f"Questions are limited to 300 characters. Please shorten yours.")
            elif rejection:
                # Suspicious input or other rejection — show generic response,
                # don't reveal the filter
                from edgedash.query.ask import _cant_answer_text
                st.markdown(f"**Answer:** {_cant_answer_text()}")
            else:
                # All guards passed — execute the pipeline
                _record_session_question()

                with st.spinner("Routing → Executing → Phrasing…"):
                    try:
                        from edgedash.query.ask import ask as ask_data
                        answer = ask_data(ask_input)
                    except Exception:
                        _logger.exception("Ask-box query failed")
                        st.error("Something went wrong processing your question. Please try again.")
                        answer = None

                if answer is not None:
                    # Prose answer
                    st.markdown(f"**Answer:** {answer.text}")

                    if answer.tool_used:
                        st.caption(
                            f"Tool: `{answer.tool_used}` · "
                            f"Params: `{json.dumps(answer.params, default=str)}`"
                        )

                    # Underlying rows — rule 44: always shown alongside the prose
                    if answer.rows:
                        import pandas as pd
                        st.dataframe(
                            pd.DataFrame(answer.rows),
                            width="stretch",
                            hide_index=True,
                        )
                    elif answer.tool_used:
                        st.info("No rows returned by this tool.")

except Exception as _panel_exc:
    _logger.exception("Ask-box panel failed")
    st.warning("⚠ The ask box could not load.")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.divider()

_footer_cycle_ts = "—"
try:
    _footer_good = _load_last_good_cycle()
    if _footer_good:
        _footer_cycle_ts = _fmt_ts(_footer_good["finished_at"])
except Exception:
    pass

st.caption(
    f"EdgeDash · read-only dashboard · last verified cycle: {_footer_cycle_ts} · "
    f"[GitHub](https://github.com/843324ABHI/edgedash)"
)
