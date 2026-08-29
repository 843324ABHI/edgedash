"""
edgedash/verdicts.py — Verification history viewer.

Usage:
    python -m edgedash.verdicts              # last 20 cycles
    python -m edgedash.verdicts --n 40       # last 40 cycles
    python -m edgedash.verdicts --check gap_sample_size

Read-only. Reads from cycle_log through edgedash.storage (rule 2).
No writes. No schema changes.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from collections import Counter
from datetime import datetime, timezone

# Force UTF-8 on Windows where the default console is cp1252
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    except AttributeError:
        pass  # already wrapped (e.g. pytest capsys)

# ---------------------------------------------------------------------------
# ANSI colour helpers — no extra dependencies
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_WHITE  = "\033[97m"
_GREY   = "\033[90m"


def _c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes if stdout is a TTY."""
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + _RESET


# ---------------------------------------------------------------------------
# Notes parser — reads the format written by orchestrator._build_summary_notes
#
# Actual format (confirmed from live DB):
#   outcome=degraded | verification=VERDICT: fail | gap_sample_size observed=2 threshold=3
#   | retry_count=1 | Fetcher:status=ok,records=0,3ms | Scorer:status=partial,...
#
# Key insight: failed check details are SEPARATE "|" segments, not nested
# inside the verification= value.
# ---------------------------------------------------------------------------

_CHECK_RE   = re.compile(r"^(\w+)\s+observed=(.+?)\s+threshold=(.+)$")
_AGENT_RE   = re.compile(r"^([A-Za-z][A-Za-z]+):status=(\w+),records=(\d+),(\S+)$")
_SKIPPED_RE = re.compile(r"^([A-Za-z][A-Za-z]+):skipped\((.+)\)$")


def _parse_notes(notes: str) -> dict:
    """
    Return a structured dict from an Orchestrator cycle_log notes string.

    Keys:
      outcome       str            "complete" | "partial" | "degraded" | "nothing_to_do"
      verdict       str | None     "pass" | "fail" | None (pre-verifier cycles)
      failed_checks list[dict]     [{name, observed, threshold}, ...]
      retry_count   int
      agents_ran    list[str]      "AgentName status records Xms"
      agents_skipped list[str]     "AgentName reason"
    """
    out: dict = {
        "outcome":        "unknown",
        "verdict":        None,
        "failed_checks":  [],
        "retry_count":    0,
        "agents_ran":     [],
        "agents_skipped": [],
    }
    if not notes:
        return out

    for part in notes.split(" | "):
        part = part.strip()
        if not part:
            continue

        if part.startswith("outcome="):
            out["outcome"] = part.split("=", 1)[1]

        elif part.startswith("verification="):
            v = part[len("verification="):]
            out["verdict"] = "pass" if "VERDICT: pass" in v else "fail"

        elif part.startswith("retry_count="):
            try:
                out["retry_count"] = int(part.split("=", 1)[1])
            except ValueError:
                pass

        elif m := _CHECK_RE.match(part):
            # Standalone failed-check segment: "name observed=X threshold=Y"
            out["failed_checks"].append({
                "name":      m.group(1),
                "observed":  m.group(2),
                "threshold": m.group(3),
            })

        elif m := _AGENT_RE.match(part):
            out["agents_ran"].append(
                f"{m.group(1)} {m.group(2)} {m.group(3)}rec {m.group(4)}"
            )

        elif m := _SKIPPED_RE.match(part):
            out["agents_skipped"].append(f"{m.group(1)} — {m.group(2)}")

    # A cycle with no verification= segment is pre-verifier; infer verdict
    # from outcome for display purposes.
    if out["verdict"] is None and out["outcome"] == "complete":
        out["verdict"] = "pass"

    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_COL_TS     = 19
_COL_STATUS = 10
_COL_VERDICT = 8


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "n/a"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]


def _age(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 3600:
            return f"{int(secs//60)}m ago"
        if secs < 86400:
            return f"{secs/3600:.1f}h ago"
        return f"{secs/86400:.1f}d ago"
    except Exception:
        return ""


def _verdict_str(parsed: dict) -> str:
    v = parsed["verdict"]
    if v == "pass":
        return _c("[PASS]", _GREEN, _BOLD)
    if v == "fail":
        return _c("[FAIL]", _RED, _BOLD)
    return _c("[--]", _GREY)


def _outcome_str(outcome: str) -> str:
    return {
        "complete":      _c("complete",      _GREEN),
        "partial":       _c("partial",       _YELLOW),
        "degraded":      _c("DEGRADED",      _RED, _BOLD),
        "nothing_to_do": _c("nothing_to_do", _GREY),
    }.get(outcome, _c(outcome, _GREY))


def _divider(char: str = "-", width: int = 78) -> str:
    return _c(char * width, _GREY)


# ---------------------------------------------------------------------------
# Row renderer
# ---------------------------------------------------------------------------


def _render_row(cyc: dict, idx: int) -> None:
    parsed   = _parse_notes(cyc.get("notes", ""))
    ts       = _fmt_ts(cyc.get("finished_at"))
    age      = _age(cyc.get("finished_at"))
    outcome  = parsed["outcome"]
    retry    = parsed["retry_count"]
    checks   = parsed["failed_checks"]
    ran      = parsed["agents_ran"]
    skipped  = parsed["agents_skipped"]

    # ── main line ──────────────────────────────────────────────────────────
    ts_col      = _c(ts, _WHITE)
    age_col     = _c(f"({age})", _GREY)
    outcome_col = _outcome_str(outcome)
    verdict_col = _verdict_str(parsed)
    retry_col   = (
        _c(f"retry={retry}", _YELLOW) if retry else _c("retry=—", _GREY)
    )

    print(f"  {ts_col} {age_col:<14}  {outcome_col:<20}  {verdict_col:<16}  {retry_col}")

    # -- failed checks (with observed value -- rule 37) ---------------------
    for fc in checks:
        name  = _c(fc["name"], _RED)
        obs   = _c(f"observed={fc['observed']}", _RED, _BOLD)
        thr   = _c(f"threshold={fc['threshold']}", _GREY)
        print(f"    -> {name}  {obs}  {thr}")

    # ── agents line ────────────────────────────────────────────────────────
    if ran:
        agents_str = _c("  ".join(ran), _GREY)
        print(f"    agents : {agents_str}")
    if skipped:
        skip_str = _c(" · ".join(s.split("—")[0].strip() for s in skipped), _DIM)
        print(f"    skipped: {skip_str}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _render_summary(parsed_rows: list[dict]) -> None:
    n = len(parsed_rows)
    if n == 0:
        return

    passes   = sum(1 for p in parsed_rows if p["verdict"] == "pass")
    fails    = sum(1 for p in parsed_rows if p["verdict"] == "fail")
    degraded = sum(1 for p in parsed_rows if p["outcome"] == "degraded")
    pass_pct = passes / n * 100

    # Tally failing check names across all cycles
    check_counter: Counter = Counter()
    for p in parsed_rows:
        for fc in p["failed_checks"]:
            check_counter[fc["name"]] += 1

    print()
    print(_divider("="))
    print(f"  {_c('SUMMARY', _WHITE, _BOLD)}  (last {n} cycles)")
    print(_divider("="))

    pass_color = _GREEN if pass_pct >= 80 else _YELLOW if pass_pct >= 50 else _RED
    print(
        f"  Pass rate : {_c(f'{pass_pct:.0f}%', pass_color, _BOLD)}"
        f"  ({passes} pass · {fails} fail · {degraded} degraded)"
    )

    if check_counter:
        noisiest_name, noisiest_count = check_counter.most_common(1)[0]
        noise_pct = noisiest_count / n * 100
        noise_label = _c(f"{noisiest_name} ({noisiest_count}/{n} cycles, {noise_pct:.0f}%)", _RED)
        print(f"  Noisiest  : {noise_label}")
        if noisiest_count / n > 0.5:
            print(
                _c(
                    f"  !! '{noisiest_name}' fails on >{noise_pct:.0f}% of cycles -- "
                    "consider tuning or removing this threshold.",
                    _YELLOW,
                )
            )
    else:
        print(f"  Noisiest  : {_c('none -- all checks passed', _GREEN)}")

    print(_divider("-"))
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.verdicts",
        description="Verification history from cycle_log. Read-only.",
    )
    parser.add_argument(
        "--n", type=int, default=20, metavar="N",
        help="Number of recent cycles to show (default: 20)",
    )
    parser.add_argument(
        "--check", metavar="NAME",
        help="Filter to cycles where this specific check failed",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None,
        help="Path to edgedash.db (default: read from config.yaml)",
    )
    args = parser.parse_args(argv)

    # Resolve DB path
    db_path = args.db
    if db_path is None:
        try:
            import yaml
            from pathlib import Path
            with open("config.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            db_path = cfg.get("db_path", "edgedash.db")
        except Exception:
            db_path = "edgedash.db"

    # Read through storage (rule 2 — no direct sqlite3)
    import edgedash.storage as storage
    cycles = storage.get_recent_cycles(limit=args.n, db_path=db_path)

    if not cycles:
        print(_c("  No cycles recorded yet.", _GREY))
        return

    # Parse all rows up front (needed for summary even when filtering)
    all_parsed = [_parse_notes(c.get("notes", "")) for c in cycles]

    # Apply --check filter
    if args.check:
        filter_name = args.check.lower()
        filtered = [
            (cyc, parsed)
            for cyc, parsed in zip(cycles, all_parsed)
            if any(fc["name"].lower() == filter_name for fc in parsed["failed_checks"])
        ]
        if not filtered:
            print(
                _c(f"  No cycles found where check '{args.check}' failed.", _GREY)
            )
            return
        display_cycles = [c for c, _ in filtered]
        display_parsed = [p for _, p in filtered]
    else:
        display_cycles = cycles
        display_parsed = all_parsed

    # Header
    print()
    print(_divider("="))
    header = f"  VERIFICATION HISTORY -- last {len(cycles)} cycles"
    if args.check:
        header += _c(f"  [filter: check={args.check}]", _CYAN)
    print(_c(header, _WHITE, _BOLD))
    print(_divider("="))
    print(
        _c(
            f"  {'TIMESTAMP':<21}  {'OUTCOME':<12}  {'VERDICT':<10}  RETRY",
            _GREY,
        )
    )
    print(_divider("-"))

    for cyc, parsed in zip(display_cycles, display_parsed):
        _render_row(cyc, 0)
        print()

    # Summary always runs over the full (unfiltered) set
    _render_summary(all_parsed)


if __name__ == "__main__":
    main()
