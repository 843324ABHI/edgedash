from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from edgedash.config import Config
from edgedash.planning import StopConditions

if TYPE_CHECKING:
    from edgedash.verification import Verdict


@dataclass
class AgentResult:
    agent: str
    status: str          # "ok" | "failed" | "partial"
    records_touched: int
    notes: str
    verdict: Verdict | None = field(default=None, repr=False)


class Agent(Protocol):
    name: str

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions,
    ) -> AgentResult: ...
