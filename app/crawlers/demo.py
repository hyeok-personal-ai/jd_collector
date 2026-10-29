"""Offline demonstration using fictional fixture data (no real website accessed)."""
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from ..schema import Attachment, JobDraft
from .base import BaseCrawler, JobRef
from ..fetcher import PoliteFetcher

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_BASE = "https://example.org"


def _txt(soup: BeautifulSoup, selector: str) -> str | None:
    node = soup.select_one(selector)
    return node.get_text(" ", strip=True) if node else None


class DemoCrawler(BaseCrawler):
    site_id = "demo"
    allowed_hosts = frozenset({"example.org"})

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        soup = BeautifulSoup((_FIXTURES / "demo_list.html").read_text(encoding="utf-8"), "html.parser")
        for a in soup.select("a[data-job-id]"):
            yield JobRef(external_id=a["data-job-id"], url=urljoin(_BASE, a["href"]))

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        # Deliberately fixture-only; real crawlers MUST call fetcher.get_text(ref.url).
        safe_id = ref.external_id
        if safe_id not in {"1001", "1002", "1003"}:
            raise ValueError("Unknown demo fixture ID")
        html = (_FIXTURES / f"demo_{safe_id}.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        attachments = [Attachment(name=a.get_text(" ", strip=True), url=urljoin(_BASE, a["href"]))
                       for a in soup.select(".attachments a[href]")]
        posted = _txt(soup, ".posted")
        deadline = _txt(soup, ".deadline")
        apply_node = soup.select_one("a.apply[href]")
        return JobDraft(
            source_id=self.site_id, external_id=ref.external_id, original_url=ref.url,
            title=_txt(soup, "h1") or "", organization=_txt(soup, ".organization"),
            recruitment_type=_txt(soup, ".category"), employment_type=_txt(soup, ".employment"),
            region=_txt(soup, ".region"), posted_on=date.fromisoformat(posted) if posted else None,
            deadline_on=date.fromisoformat(deadline) if deadline and deadline != "상시모집" else None,
            deadline_text=deadline, body_text=_txt(soup, ".description"),
            apply_url=urljoin(_BASE, apply_node["href"]) if apply_node else None,
            attachments=attachments, source_status="unknown",
        )
