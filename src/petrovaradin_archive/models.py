from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DiscoveredURL:
    url: str
    site_id: str
    discovered_by: str
    query: str | None = None
    expected_date: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

