# EdgeDash

EdgeDash is an autonomous AI career intelligence agent designed to streamline tech job discovery and personal skill development. Operating as a scheduled loop, EdgeDash fetches live job listings across targeted roles and locations, scores them for fit against a personalized user profile, surfaces recurring skill gaps, verifies output integrity, and publishes processed insights to a read-only Streamlit dashboard.

---

## Architecture

```
                                +-----------------------------------+
                                |        Trigger (Scheduled)        |
                                +-----------------------------------+
                                                  |
                                                  v
                                +-----------------------------------+
                                |           Orchestrator            |
                                +-----------------------------------+
                                                  |
                 +--------------------------------+--------------------------------+
                 |                                |                                |
                 v                                v                                v
     +-----------------------+        +-----------------------+        +-----------------------+
     |     Mock Fetcher      |        |        Scorer         |        |      GapAnalyzer      |
     |      (Sub-agent)      |        |      (Sub-agent)      |        |      (Sub-agent)      |
     +-----------------------+        +-----------------------+        +-----------------------+
                 |                                |                                |
                 +--------------------------------+--------------------------------+
                                                  |
                                                  v
                                +-----------------------------------+
                                |             Verifier              |
                                +-----------------------------------+
                                                  |
                                                  v
                                +-----------------------------------+
                                |      Storage (Single Module)      |
                                +-----------------------------------+
                                                  |
                                                  v
                                +-----------------------------------+
                                |       Dashboard (Read-Only)       |
                                +-----------------------------------+
```

---

## Current Status & Roadmap

- [x] **Week 1: Core Foundation & Loop Skeleton** *(Built)*
  - [x] Steering document and project guidelines (`.kiro/steering/edgedash.md`)
  - [x] Centralized configuration dataclass & YAML loader (`edgedash/config.py`)
  - [x] Thin storage interface with SQLite tables (`edgedash/storage.py`)
  - [x] Agent Protocol & Execution Result types (`edgedash/agents/base.py`)
  - [x] **Mock Fetcher** *(Temporary test agent with 12 mock listings & 4 stable IDs for dedup testing)*
  - [x] Orchestrator loop, registry, and cycle logger (`edgedash/orchestrator.py`)
  - [x] CLI entry point (`run_cycle.py`)

- [ ] **Week 2: Real Scraping & Scoring** *(Upcoming)*
  - [ ] Real job fetcher agent (replacing temporary `MockFetcher`)
  - [ ] LLM-assisted job fit Scorer agent

- [ ] **Week 3: Gap Analysis & Verification** *(Upcoming)*
  - [ ] Skill Gap Analyzer agent
  - [ ] Verifier agent to validate data consistency

- [ ] **Week 4: Storage Migration & Dashboard** *(Upcoming)*
  - [ ] Hosted Postgres storage module migration
  - [ ] Streamlit read-only dashboard publication

---

## Setup & Running

### Requirements
- **Python 3.11+**

### Installation

1. **Clone the repository and create a virtual environment:**
   ```powershell
   python -m venv .venv
   ```

2. **Activate the virtual environment:**
   - **PowerShell:**
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   - **Command Prompt:**
     ```cmd
     .venv\Scripts\activate.bat
     ```

3. **Install dependencies:**
   ```powershell
   pip install pyyaml
   ```

### Configuration

Edit `config.yaml` at the root of the project to customize your role, location, target skills, and criteria:

```yaml
target_role: "Data Analyst"
target_city: "Bengaluru"
keywords:
  - "Data Analyst"
  - "SQL"
  - "Python"
  - "PowerBI"
my_skills:
  - "Python"
  - "SQL"
  - "Pandas"
  - "Tableau"
  - "Excel"
experience_years: 3
db_path: "edgedash.db"
min_fit_score: 70
```

### Running a Cycle

To execute one full orchestrator cycle:

```powershell
python run_cycle.py
```

---

## Design Decisions

- **Single Isolated Storage Module**: All database access is encapsulated within `edgedash/storage.py`, strictly prohibiting direct database calls or `sqlite3` imports in other modules. This ensures swapping SQLite for hosted PostgreSQL in Week 4 will require modifying only a single file without altering any sub-agent or orchestrator code.
- **Stable Hash Listing IDs**: Listing IDs are derived deterministically using a SHA-256 hash of `source + url` (`generate_listing_id`). This ensures idempotent deduplication across multiple fetch runs via `INSERT OR IGNORE`, so identical job postings are never stored or processed twice.
- **Orchestrator Delegation**: The orchestrator is strictly an event-driven coordinator that reads state, formulates execution plans, delegates tasks to sub-agents, and logs results. By keeping fetching and scoring out of the orchestrator, sub-agents remain isolated, single-purpose, and independently testable.
