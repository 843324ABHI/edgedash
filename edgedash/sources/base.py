from abc import ABC, abstractmethod
from typing import Any

from edgedash.config import Config


class Source(ABC):
    name: str

    @abstractmethod
    def fetch(self, config: Config, max_pages: int | None = None) -> list[dict[str, Any]]:
        """Fetch normalised job rows.

        Each row must contain exactly these keys (steering rule 10):
            source, external_id, title, company, location, url,
            description, posted_at, raw
        Missing values are None — never empty string, never "N/A".
        """


# ---------------------------------------------------------------------------
# Global registry.  New sources are added by decorating the class only.
# ---------------------------------------------------------------------------
SOURCES: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Class decorator — adds cls to SOURCES under cls.name."""
    SOURCES[cls.name] = cls
    return cls
