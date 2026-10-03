from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pricehunter.db.models import MerchantProgram
from pricehunter.domain.feeds import FeedProductData, FeedReference, RejectedFeedRow


class FeedSource(ABC):
    """Network wire formats end here. Sources never write canonical catalog rows."""

    name: str

    async def discover_feeds(self) -> list[FeedReference]:
        return []

    async def source_version(self, program: MerchantProgram) -> str | None:
        return None

    @abstractmethod
    def stream_items(
        self, program: MerchantProgram
    ) -> AsyncIterator[FeedProductData | RejectedFeedRow]: ...
