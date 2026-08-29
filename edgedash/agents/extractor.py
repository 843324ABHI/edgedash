"""
edgedash/agents/extractor.py

Extracts structured facts from a job description by calling the LLM once.
This is the ONLY place in the codebase that translates raw listing text into
machine-readable fields. It has no scoring logic (rule 16).

Public API:
    extract(listing: dict, db_path: str = "edgedash.db") -> dict
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from edgedash import llm
from edgedash import storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema  (rule 16: no score field — there must never be one here)
# ---------------------------------------------------------------------------

# seniority is validated as a string at the schema level; the normaliser below
# enforces the exact enum values so the schema stays simple (our slim validator
# doesn't support "enum").
SENIORITY_VALUES = {"junior", "mid", "senior", "lead", "unknown"}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "properties": {
        "required_skills": {
            "type": "array",
            "items": {"type": "string"},
        },
        "nice_to_have": {
            "type": "array",
            "items": {"type": "string"},
        },
        "seniority": {
            # must be one of SENIORITY_VALUES; enforced in _normalise()
            "type": "string",
        },
        # years_required and remote_ok may be int/bool or null.
        # Our slim validator supports {} (any type) for nullable fields.
        "years_required": {},
        "remote_ok": {},
    },
}

# ---------------------------------------------------------------------------
# Prompt  (rule 16: model reads the document only — no candidate, no scoring)
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a document parser. Read the job description below and extract \
structured facts ONLY from what is explicitly stated in the text.

Rules:
- Do NOT infer, guess, or evaluate. Only extract what is written.
- Do NOT consider any candidate, resume, or profile. No candidate exists.
- Do NOT score, rank, or rate anything.
- If the listing does not state a value, use null (for years_required and \
remote_ok) or an empty list (for required_skills and nice_to_have).
- For seniority, use exactly one of: junior, mid, senior, lead, unknown.
  Use "unknown" unless the listing explicitly states a level.
- Skills must be taken verbatim from the listing text only.

--- JOB DESCRIPTION START ---
{description}
--- JOB DESCRIPTION END ---"""


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Coerce model output into clean Python types before caching or returning.

    - Skill name strings → lowercase and stripped.
    - seniority → lowercase; defaults to "unknown" if unrecognised.
    - years_required → int or None (never a float or string).
    - remote_ok → bool or None.
    """
    required_skills: list[str] = [
        s.strip().lower()
        for s in (raw.get("required_skills") or [])
        if isinstance(s, str) and s.strip()
    ]
    nice_to_have: list[str] = [
        s.strip().lower()
        for s in (raw.get("nice_to_have") or [])
        if isinstance(s, str) and s.strip()
    ]

    raw_seniority = str(raw.get("seniority") or "").strip().lower()
    seniority = raw_seniority if raw_seniority in SENIORITY_VALUES else "unknown"

    raw_years = raw.get("years_required")
    if raw_years is None:
        years_required: int | None = None
    else:
        try:
            years_required = int(raw_years)
        except (TypeError, ValueError):
            years_required = None

    raw_remote = raw.get("remote_ok")
    if raw_remote is None:
        remote_ok: bool | None = None
    elif isinstance(raw_remote, bool):
        remote_ok = raw_remote
    elif isinstance(raw_remote, str):
        remote_ok = raw_remote.strip().lower() in {"true", "yes", "1"}
    else:
        remote_ok = bool(raw_remote)

    return {
        "required_skills": required_skills,
        "nice_to_have": nice_to_have,
        "seniority": seniority,
        "years_required": years_required,
        "remote_ok": remote_ok,
    }


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


def _description_hash(text: str) -> str:
    """SHA-256 of the normalised description text — stable across runs."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(listing: dict[str, Any], db_path: str = "edgedash.db") -> dict[str, Any]:
    """
    Extract structured facts from *listing["description"]*.

    Cache behaviour (rule 18):
        - Hit  → return stored result immediately; no model call.
        - Miss → call the model, normalise, store, return.

    The model is given ONLY the job description text. It has no knowledge
    of any candidate, scoring weights, or fit criteria (rule 16).

    Returns a dict matching EXTRACTION_SCHEMA after normalisation.
    Raises llm.LLMError on unrecoverable model failure (callers handle per rule 17).
    """
    storage.migrate_extraction_cache(db_path)

    description: str = listing.get("description") or ""
    desc_hash = _description_hash(description)

    # --- cache hit ---
    cached = storage.get_extraction_cache(desc_hash, db_path)
    if cached is not None:
        logger.debug("Extraction cache hit for hash %s", desc_hash[:12])
        return cached

    # --- cache miss: call model ---
    logger.debug("Extraction cache miss for hash %s — calling model", desc_hash[:12])
    
    # Truncate to prevent 413 Request Entity Too Large errors on massive payloads
    # 8000 chars is roughly 2000 tokens, safe for stricter LLM context windows
    safe_description = description[:8000]
    prompt = _PROMPT_TEMPLATE.format(description=safe_description)

    raw = llm.complete_json(prompt, EXTRACTION_SCHEMA)
    normalised = _normalise(raw)

    storage.set_extraction_cache(desc_hash, normalised, db_path)
    logger.info(
        "Extracted and cached listing %s (hash %s)",
        listing.get("id", "<no-id>"),
        desc_hash[:12],
    )
    return normalised
