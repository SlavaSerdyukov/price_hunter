from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pricehunter.domain.discovery import Capability


class IngestionMode(StrEnum):
    DISCOVERY = "discovery"
    SEARCH_SNAPSHOT = "search_snapshot"


@dataclass(frozen=True)
class FeedRevalidationContext:
    """Server-side evidence from a current completed feed, never a caller's freshness flag."""

    program_id: UUID
    external_id: str
    generation: int
    confirmed_at: datetime


def search_ingestion(capabilities: frozenset[Capability]) -> IngestionMode:
    # Item refresh retains authority even if an adapter also consumes feed snapshots.
    if Capability.SNAPSHOT_REFRESH in capabilities and Capability.REFRESH not in capabilities:
        return IngestionMode.SEARCH_SNAPSHOT
    return IngestionMode.DISCOVERY
