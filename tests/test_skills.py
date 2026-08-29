"""
tests/test_skills.py — Unit tests for edgedash/skills.py :: canonical().

canonical() is a pure function: no mocks, no network, no DB.
Every test is deterministic.
"""

from __future__ import annotations

import pytest

from edgedash.skills import canonical


# ---------------------------------------------------------------------------
# Fixture: a small but representative alias map
# ---------------------------------------------------------------------------

ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "postgresql": "postgres",
    "psql": "postgres",
    "google cloud": "gcp",
    "google cloud platform": "gcp",
    "ml": "machine learning",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
    "nodejs": "node",
    "node.js": "node",
}


# ---------------------------------------------------------------------------
# Case normalisation
# ---------------------------------------------------------------------------


def test_uppercase_is_lowercased() -> None:
    assert canonical("Python", ALIASES) == "python"


def test_mixed_case_is_lowercased() -> None:
    assert canonical("PostgreSQL", ALIASES) == "postgres"


def test_all_caps_is_lowercased() -> None:
    assert canonical("SQL", ALIASES) == "sql"


# ---------------------------------------------------------------------------
# Whitespace handling
# ---------------------------------------------------------------------------


def test_leading_trailing_whitespace_stripped() -> None:
    assert canonical("  python  ", ALIASES) == "python"


def test_internal_whitespace_collapsed() -> None:
    assert canonical("machine   learning", ALIASES) == "machine learning"


def test_tab_and_newline_collapsed() -> None:
    assert canonical("machine\tlearning", ALIASES) == "machine learning"


# ---------------------------------------------------------------------------
# Parenthetical qualifier removal
# ---------------------------------------------------------------------------


def test_parenthetical_qualifier_dropped() -> None:
    assert canonical("kubernetes (eks)", ALIASES) == "kubernetes"


def test_parenthetical_with_commas_dropped() -> None:
    assert canonical("aws (ec2, s3)", ALIASES) == "aws"


def test_parenthetical_version_dropped() -> None:
    assert canonical("python (3.x)", ALIASES) == "python"


def test_no_parenthetical_unchanged() -> None:
    assert canonical("docker", ALIASES) == "docker"


# ---------------------------------------------------------------------------
# Alias lookup
# ---------------------------------------------------------------------------


def test_aliased_term_resolves() -> None:
    assert canonical("k8s", ALIASES) == "kubernetes"


def test_aliased_term_case_insensitive() -> None:
    assert canonical("K8S", ALIASES) == "kubernetes"


def test_psql_resolves_to_postgres() -> None:
    assert canonical("psql", ALIASES) == "postgres"


def test_ml_resolves_to_machine_learning() -> None:
    assert canonical("ml", ALIASES) == "machine learning"


def test_cicd_resolves() -> None:
    assert canonical("cicd", ALIASES) == "ci/cd"


def test_ci_cd_with_space_resolves() -> None:
    assert canonical("ci cd", ALIASES) == "ci/cd"


def test_nodejs_resolves_to_node() -> None:
    assert canonical("nodejs", ALIASES) == "node"


def test_node_js_dot_resolves_to_node() -> None:
    assert canonical("node.js", ALIASES) == "node"


# ---------------------------------------------------------------------------
# Term with no alias — must pass through unchanged
# ---------------------------------------------------------------------------


def test_unknown_term_passthrough() -> None:
    assert canonical("terraform", ALIASES) == "terraform"


def test_unknown_multi_word_passthrough() -> None:
    assert canonical("data engineering", ALIASES) == "data engineering"


# ---------------------------------------------------------------------------
# Node ≠ JavaScript (must NOT be merged)
# ---------------------------------------------------------------------------


def test_node_and_javascript_are_distinct() -> None:
    assert canonical("node", ALIASES) != canonical("javascript", ALIASES)
    assert canonical("nodejs", ALIASES) == "node"
    assert canonical("javascript", ALIASES) == "javascript"


# ---------------------------------------------------------------------------
# Empty string
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty() -> None:
    assert canonical("", ALIASES) == ""


def test_whitespace_only_returns_empty() -> None:
    assert canonical("   ", ALIASES) == ""


def test_punctuation_only_returns_empty() -> None:
    # After stripping surrounding punctuation nothing is left
    assert canonical("---", ALIASES) == ""


# ---------------------------------------------------------------------------
# Non-string input guard
# ---------------------------------------------------------------------------


def test_none_input_returns_empty() -> None:
    assert canonical(None, ALIASES) == ""  # type: ignore[arg-type]


def test_integer_input_returns_empty() -> None:
    assert canonical(42, ALIASES) == ""  # type: ignore[arg-type]
