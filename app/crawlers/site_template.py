"""COPY this file for each real site. Not enabled in registry by default.

Selectors, pagination and dates are site-dependent. Verify site permission and
robots.txt first, then replace example.org and the CSS selectors.
"""
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from ..schema import JobDraft
from .base import BaseCrawler, JobRef
from ..fetcher import PoliteFetcher


class SiteTemplateCrawler(BaseCrawler):
    site_id = "replace_me"
    allowed_hosts = frozenset({"example.org"})
    base_url = "https://example.org"
    max_pages = 10  # Increase for authorized backfill; consider site's pagination rules.

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        seen_ids: set[str] = set()
        for page in range(1, self.max_pages + 1):
            html = await fetcher.get_text(f"{self.base_url}/jobs?page={page}")
            soup = BeautifulSoup(html, "html.parser")
            links = soup.select("article.job a.job-link[href]")  # TODO: customize
            if not links:
                break
            for a in links:
                absolute = urljoin(self.base_url, a["href"])
                # TODO: prefer stable source API ID / HTML data-id / query param.
                external_id = urlsplit(absolute).path.rstrip("/").split("/")[-1]
                if not external_id or external_id in seen_ids:
                    continue
                seen_ids.add(external_id)
                yield JobRef(external_id=external_id, url=absolute)

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        html = await fetcher.get_text(ref.url)
        soup = BeautifulSoup(html, "html.parser")

        def text(css: str) -> str | None:
            node = soup.select_one(css)
            return node.get_text(" ", strip=True) if node else None

        posted = text(".posted-date")  # TODO: normalize site's date format
        deadline = text(".deadline-date")
        return JobDraft(
            source_id=self.site_id, external_id=ref.external_id,
            original_url=ref.url, title=text("h1.job-title") or "",
            organization=text(".company-name"), recruitment_type=text(".job-category"),
            employment_type=text(".employment-type"), region=text(".location"),
            posted_on=date.fromisoformat(posted) if posted else None,
            deadline_on=date.fromisoformat(deadline) if deadline else None,
            deadline_text=deadline, body_text=text(".job-description"),
        )
