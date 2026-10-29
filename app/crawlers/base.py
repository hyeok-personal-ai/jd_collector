"""Contract: discover references then fetch/parse each posting."""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from ..schema import JobDraft
from ..fetcher import PoliteFetcher


class PostingUnavailable(ValueError):
    """A listed public URL explicitly returned 404/410; never fabricate a posting."""


@dataclass(frozen=True)
class JobRef:
    external_id: str
    url: str
    listing_fingerprint: str | None = None  # Updated timestamp or revision token, if provided by source
    payload: dict | None = None  # Provider-authorized feed row; no detail HTTP required


class BaseCrawler(ABC):
    site_id: str
    allowed_hosts: frozenset[str]

    @abstractmethod
    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        """Yield references across all accessible listing pages, without duplicates."""
        if False:
            yield JobRef("", "")

    @abstractmethod
    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        """Return a normalized posting. Raise on parse failure (runner records it)."""
        ...
