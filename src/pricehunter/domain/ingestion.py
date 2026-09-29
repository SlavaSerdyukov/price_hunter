from enum import StrEnum

from pricehunter.domain.discovery import Capability


class IngestionMode(StrEnum):
    DISCOVERY = "discovery"
    SEARCH_SNAPSHOT = "search_snapshot"


def search_ingestion(capabilities: frozenset[Capability]) -> IngestionMode:
    # Item refresh retains authority even if an adapter also consumes feed snapshots.
    if Capability.SNAPSHOT_REFRESH in capabilities and Capability.REFRESH not in capabilities:
        return IngestionMode.SEARCH_SNAPSHOT
    return IngestionMode.DISCOVERY
