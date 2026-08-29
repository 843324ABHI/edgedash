from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv
import yaml

# Centralized environment variable loading (Rule 4)
load_dotenv()


@dataclass
class Config:
    target_role: str = ""
    target_city: str = ""
    keywords: list[str] = field(default_factory=list)
    my_skills: list[str] = field(default_factory=list)
    experience_years: int = 0
    db_path: str = "edgedash.db"
    min_fit_score: int = 70
    sources: list[str] = field(default_factory=lambda: ["arbeitnow"])
    use_mock_fetcher: bool = False
    # LLM settings (rule 15)
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_rps: float = 1.0   # max requests per second
    llm_rpm: int = 15       # max requests per minute (rolling window)
    llm_tpm: int = 4000     # max tokens per minute (rolling window)
    llm_batch_size: int = 25  # max listings scored per cycle (rule 21)
    # Orchestration thresholds (steering rules 28-33)
    fetch_interval_hours: float = 6.0  # run Fetcher when hours_since_fetch >= this
    fetch_max_pages: int = 5           # stop condition passed to Fetcher
    fetch_max_listings: int = 200      # stop condition passed to Fetcher
    score_max_seconds: int = 300       # stop condition passed to Scorer
    analyse_max_seconds: int = 120     # stop condition passed to GapAnalyzer
    # Scoring weights (rule 16 — deterministic arithmetic only)
    target_seniority: str = "mid"  # junior|mid|senior|lead
    weight_skill_match:   float = 0.45
    weight_seniority_fit: float = 0.25
    weight_location_fit:  float = 0.15
    weight_recency:       float = 0.15
    # Query abuse guards
    query_daily_cap: int = 200  # max ask-box questions per calendar day (UTC)
    # Verification thresholds (steering rules 34-39)
    verification_min_score_spread:       float = 10.0  # min max-min spread across scores
    verification_min_score_stdev:        float = 5.0   # min stdev across scores
    verification_max_empty_extraction_pct: float = 20.0  # max % listings with empty skills
    verification_max_skills_per_listing: int   = 20    # max skills in one listing
    verification_min_gap_sample:         int   = 3     # min listings backing top gap
    verification_max_data_age_days:      float = 3.0   # max age of newest listing in days


def load_config(config_path: str = "config.yaml") -> Config:
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"Configuration file '{config_path}' was not found. "
            "Please create config.yaml at the project root."
        )

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return Config(
        target_role=str(data.get("target_role", "")),
        target_city=str(data.get("target_city", "")),
        keywords=list(data.get("keywords", [])),
        my_skills=list(data.get("my_skills", [])),
        experience_years=int(data.get("experience_years", 0)),
        db_path=str(data.get("db_path", "edgedash.db")),
        min_fit_score=int(data.get("min_fit_score", 70)),
        sources=list(data.get("sources", ["arbeitnow"])),
        use_mock_fetcher=bool(data.get("use_mock_fetcher", False)),
        llm_provider=str(data.get("llm_provider", "groq")),
        llm_model=str(data.get("llm_model", "llama-3.3-70b-versatile")),
        llm_rps=float(data.get("llm_rps", 1.0)),
        llm_rpm=int(data.get("llm_rpm", 15)),
        llm_tpm=int(data.get("llm_tpm", 4000)),
        llm_batch_size=int(data.get("llm_batch_size", 25)),
        fetch_interval_hours=float(data.get("fetch_interval_hours", 6.0)),
        fetch_max_pages=int(data.get("fetch_max_pages", 5)),
        fetch_max_listings=int(data.get("fetch_max_listings", 200)),
        score_max_seconds=int(data.get("score_max_seconds", 300)),
        analyse_max_seconds=int(data.get("analyse_max_seconds", 120)),
        target_seniority=str(data.get("target_seniority", "mid")),
        weight_skill_match=float(data.get("weight_skill_match", 0.45)),
        weight_seniority_fit=float(data.get("weight_seniority_fit", 0.25)),
        weight_location_fit=float(data.get("weight_location_fit", 0.15)),
        weight_recency=float(data.get("weight_recency", 0.15)),
        query_daily_cap=int(data.get("query_daily_cap", 200)),
        verification_min_score_spread=float(
            data.get("verification_min_score_spread", 10.0)
        ),
        verification_min_score_stdev=float(
            data.get("verification_min_score_stdev", 5.0)
        ),
        verification_max_empty_extraction_pct=float(
            data.get("verification_max_empty_extraction_pct", 20.0)
        ),
        verification_max_skills_per_listing=int(
            data.get("verification_max_skills_per_listing", 20)
        ),
        verification_min_gap_sample=int(
            data.get("verification_min_gap_sample", 3)
        ),
        verification_max_data_age_days=float(
            data.get("verification_max_data_age_days", 3.0)
        ),
    )
