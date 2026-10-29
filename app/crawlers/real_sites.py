"""Conservative HTML adapters for three user-provided recruitment URLs.

NOT verified against live network in the packaging environment. Deliberately fail
rather than storing fake data when an expected listing/detail layout is missing.
The fetcher checks robots.txt; never bypass anti-bot or login restrictions.
"""
from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..fetcher import PoliteFetcher
from ..schema import JobDraft
from .base import BaseCrawler, JobRef

SARMIN_PARAMS = {
    "exp_cd": "1,2", "company_cd": "0,1,2,3,4,5,6,7,9,10", "exp_none": "y",
    "exp_max": "1", "cat_mcls": "2", "panel_type": "domestic",
    "search_optional_item": "y", "search_done": "y", "panel_count": "y",
    "preview": "y", "page": "1", "sort": "EA", "isAjaxRequest": "y",
    "page_count": "100",
}
SITE_LISTS = {
    "jobkorea": "https://www.jobkorea.co.kr/theme/entry-level-internship",
    "saramin": "https://www.saramin.co.kr/zf_user/jobs/public/list?" + urlencode(SARMIN_PARAMS),
    "inthiswork": "https://inthiswork.com/it",
}


def _text(node) -> str | None:
    value = node.get_text(" ", strip=True) if node else ""
    return re.sub(r"\s+", " ", value).strip() or None


def _first(soup, selectors: tuple[str, ...]) -> str | None:
    return next((value for css in selectors if (value := _text(soup.select_one(css)))), None)


def _safe_url(href: str | None, base: str) -> str | None:
    if not href:
        return None
    url = urljoin(base, href)
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return None
    return url


def _stable_ref(site: str, href: str, base: str) -> JobRef | None:
    url = _safe_url(href, base)
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if site == "jobkorea":
        if host != "www.jobkorea.co.kr":
            return None
        match = re.search(r"/recruit/gi_read/(\d+)(?:/|$)", parts.path, re.I)
        if not match:
            return None
        clean = f"https://www.jobkorea.co.kr/Recruit/GI_Read/{match.group(1)}"
        return JobRef(match.group(1), clean)
    if site == "saramin":
        if host != "www.saramin.co.kr" or not parts.path.startswith("/zf_user/jobs/relay/view"):
            return None
        job_id = parse_qs(parts.query).get("rec_idx", [""])[0]
        if not job_id.isdecimal():
            return None
        clean = "https://www.saramin.co.kr/zf_user/jobs/relay/view?" + urlencode({"rec_idx": job_id})
        return JobRef(job_id, clean)
    if site == "incruit":
        if host != "lab.incruit.com":
            return None
        match = re.fullmatch(r"/jobs/(\d+)/?", parts.path)
        return JobRef(match.group(1), f"https://lab.incruit.com/jobs/{match.group(1)}") if match else None
    if site == "zighang":
        if host not in {"zighang.com", "www.zighang.com"}:
            return None
        # The site's actual page structure is not established; only accepts
        # explicit individual recruitment links, never guessed private APIs.
        match = re.fullmatch(r"/(?:recruitment|jobs)/([a-zA-Z0-9_-]{6,})/?", parts.path)
        return JobRef(match.group(1), f"https://zighang.com{parts.path.rstrip('/')}") if match else None
    if site == "inthiswork":
        if host not in {"inthiswork.com", "www.inthiswork.com"}:
            return None
        match = re.fullmatch(r"/archives/(\d+)/?", parts.path)
        if not match:
            return None
        clean = f"https://inthiswork.com/archives/{match.group(1)}"
        return JobRef(match.group(1), clean)
    raise ValueError(site)


def listing_refs(site: str, html: str, base_url: str) -> list[JobRef]:
    """Parse stable IDs, not position-dependent row indexes or tracking queries."""
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select("a[href]")
    if site == "inthiswork":
        # Prefer entry headings, to exclude most unrelated sidebar/content links.
        titled = soup.select("article h2 a[href], article h3 a[href], .entry-title a[href], .post-title a[href]")
        if titled:
            links = titled
    results: dict[str, JobRef] = {}
    for a in links:
        if site == "inthiswork":
            title = _text(a) or ""
            if not any(word in title for word in ("｜", "채용", "모집", "인턴", "개발자", "Engineer", "Developer")):
                continue
            # Exclude explicitly labeled career editorial articles.
            context = a.find_parent("article")
            cats = " ".join(x.get_text(" ", strip=True) for x in context.select(".cat-links, .post-categories, .category")[:3]) if context else ""
            if "취업토크" in cats and "신입" not in cats:
                continue
        ref = _stable_ref(site, a.get("href", ""), base_url)
        if ref:
            results.setdefault(ref.external_id, ref)
    return list(results.values())


def _date(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"(?<!\d)(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})(?!\d)", text)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _label_value(soup: BeautifulSoup, label: str) -> str | None:
    for row in soup.select("tr, li, dl, .info_item, .summary_item"):
        txt = row.get_text(" ", strip=True)
        if not txt.startswith(label) or len(txt) > 260:
            continue
        value = txt[len(label):].lstrip(" :·\t")
        if value:
            return value
    return None


def detail_draft(site: str, ref: JobRef, html: str) -> JobDraft:
    """Fail closed on login pages and JS shells; never save empty/sham postings."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, nav, footer, .comments-area, .related-posts"):
        node.decompose()
    if site == "inthiswork":
        title = _first(soup, ("h1.entry-title", "article h1", "main h1", "h1"))
        body = _first(soup, (".entry-content", "article .post-content", "article", "main"))
        post_date = soup.select_one("time[datetime]")
        posted_on = _date(post_date.get("datetime")) if post_date else None
        org = title.split("｜", 1)[0].strip() if title and "｜" in title else None
        apply_url = None
        scope = soup.select_one(".entry-content, article .post-content, article")
        if scope:
            for a in scope.select("a[href]"):
                t = _text(a) or ""
                if "지원" in t or "Apply" in t:
                    apply_url = _safe_url(a.get("href"), ref.url)
                    break
        deadline = None
        if body:
            m = re.search(r"(?:접수\s*마감|지원\s*마감|마감일)\s*[:：]?\s*((?:20\d{2})[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})", body)
            deadline = _date(m.group(1)) if m else None
        recruitment_type = "인턴" if title and "인턴" in title else "채용"
        employment_type = "인턴" if title and "인턴" in title else None
        region = None
        deadline_text = None
    elif site == "saramin":
        # Saramin relay pages can be JS shells: demand a real posting title/content.
        title = _first(soup, (".job_tit h1", ".tit_job", "h1.job_tit", "h1"))
        body = _first(soup, (".cont_recruit", ".job_view_content", ".recruitment_details", ".detail_content"))
        org = _first(soup, (".company_name a", ".company_name", ".company_nm"))
        region = _label_value(soup, "근무지역")
        deadline_text = _label_value(soup, "접수기간") or _label_value(soup, "마감일")
        deadline = _date(deadline_text)
        posted_on = None
        apply_url = None
        recruitment_type = "채용"
        employment_type = _label_value(soup, "근무형태")
    elif site in {"incruit", "zighang"}:
        title = _first(soup, ("h1", ".job-title", ".recruitment-title"))
        body = _first(soup, (".job-description", ".recruitment-content", ".job-detail", "main article", "main"))
        # A generic JS shell or marketing page is not a recruitment notice.
        if body and not any(term in body for term in (
            '자격요건', '담당업무', '모집부문', '지원자격', '우대사항',
            'Responsibilities', 'Requirements', 'Qualifications', '채용공고'
        )):
            body = None
        org = _first(soup, (".company-name", ".company", ".organization"))
        region = _label_value(soup, "근무지역")
        deadline_text = _label_value(soup, "접수마감") or _label_value(soup, "마감일")
        deadline = _date(deadline_text)
        posted_on = None
        apply_url = None
        recruitment_type = "채용"
        employment_type = _label_value(soup, "고용형태")
    elif site == "jobkorea":
        title = _first(soup, ("h1", ".tbDetail h3", ".recruitment-title"))
        body = _first(soup, (".detail-content", ".tbDetail", ".recruitment-content", "main", "article"))
        org = _first(soup, (".coName a", ".company-name", ".coName"))
        region = _label_value(soup, "근무지주소") or _label_value(soup, "근무지역")
        deadline_text = _label_value(soup, "마감일")
        deadline = _date(deadline_text)
        posted_on = _date(_label_value(soup, "시작일"))
        apply_url = None
        recruitment_type = "채용"
        employment_type = _label_value(soup, "고용형태")
    else:
        raise ValueError(site)
    if not title or not body or len(body) < 50:
        raise ValueError(f"{site} detail layout missing title/body (JS shell, login or changed HTML)")
    if not org and site == "jobkorea":
        # The company name is often in a heading immediately preceding h1.
        top = soup.select_one("h1")
        prev = top.find_previous("a") if top else None
        org = _text(prev) if prev and len(_text(prev) or "") < 90 else None
    return JobDraft(source_id=site, external_id=ref.external_id, original_url=ref.url,
        title=title, organization=org, recruitment_type=recruitment_type,
        employment_type=employment_type, region=region, posted_on=posted_on,
        deadline_on=deadline, deadline_text=deadline_text, body_text=body,
        apply_url=apply_url, source_status="unknown")


def _page_url(site: str, page: int) -> str:
    if site == "inthiswork":
        return SITE_LISTS[site] if page == 1 else f"https://inthiswork.com/it/page/{page}/"
    if site in {"incruit", "zighang"}:
        # Pagination mechanics are unverified; only check the user-supplied page.
        return {"incruit": "https://lab.incruit.com/navi/jobs?sort=deadline&occu=IT",
                "zighang": "https://zighang.com/recruitment"}[site]
    if site == "jobkorea":
        # The themed listing uses unknown dynamic pagination; only first page.
        return SITE_LISTS[site]
    parts = urlsplit(SITE_LISTS[site]); params = dict(parse_qs(parts.query))
    flat = {key: values[0] for key, values in params.items()}
    flat["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(flat), ""))


class HtmlSiteCrawler(BaseCrawler):
    site_id: str
    allowed_hosts: frozenset[str]

    def __init__(self, *, max_pages: int | None = None):
        default = int(os.getenv("MAX_LIST_PAGES", "2"))
        self.max_pages = default if max_pages is None else max_pages
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        pages = min(self.max_pages, 1) if self.site_id in {'jobkorea', 'incruit', 'zighang'} else self.max_pages
        consecutive_old = 0
        backfill = os.getenv('CRAWL_BACKFILL', '') == '1'
        for page in range(1, pages + 1):
            url = _page_url(self.site_id, page)
            cache = getattr(self, 'listing_cache', None)
            dynamic_render = self.site_id in {'incruit', 'zighang'} and os.getenv('RENDER_JS', '') == '1'
            # A static JS shell's HTTP ETag does NOT describe hydrated listings.
            headers = cache.headers_for(self.site_id, url) if cache and not backfill and not dynamic_render else {}
            result = await fetcher.get_response(url, headers=headers)
            if result.status == 304:
                if cache:
                    cache.update(self.site_id, url, status=304)
                continue
            html = result.text or ''
            if dynamic_render:
                # The initial GET has verified robots.txt and live HTTP permission.
                # Use a standard browser only when explicitly enabled by the operator.
                html = await fetcher.render_html(url)
            refs = listing_refs(self.site_id, html, url)
            if not refs:
                if page == 1:
                    raise ValueError(f'{self.site_id} returned no stable listing IDs: JS shell or layout change')
                break
            if cache:
                fresh = cache.update(self.site_id, url, status=200, text=html,
                                     etag='' if dynamic_render else result.etag,
                                     last_modified='' if dynamic_render else result.last_modified)
                if not fresh and not backfill:
                    continue
                known = cache.count_known(self.site_id, [ref.external_id for ref in refs])
            else:
                known = 0
            for ref in refs:
                yield ref
            # Only pages with verified chronological pagination may stop early.
            # On page 1, verify revisions; after 2 entirely known pages stop.
            if self.site_id == 'inthiswork' and not backfill and page > 1:
                consecutive_old = consecutive_old + 1 if known == len(refs) else 0
                if consecutive_old >= 2:
                    break

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if self.site_id in {'incruit', 'zighang'} and os.getenv('RENDER_JS', '') == '1':
            # No WAF/login bypass: first normal GET must be permitted and 200.
            await fetcher.get_response(ref.url)
            html = await fetcher.render_html(ref.url)
            draft = detail_draft(self.site_id, ref, html)
            fetcher.capture_rendered_html(ref.url, html)
            return draft
        html = await fetcher.get_text(ref.url, capture=True)
        return detail_draft(self.site_id, ref, html)


class InThisWorkCrawler(HtmlSiteCrawler):
    site_id = "inthiswork"
    allowed_hosts = frozenset({"inthiswork.com", "www.inthiswork.com"})


class SaraminCrawler(HtmlSiteCrawler):
    site_id = "saramin"
    allowed_hosts = frozenset({"www.saramin.co.kr"})


class JobKoreaCrawler(HtmlSiteCrawler):
    # Use official API if permission is granted; this HTML adapter is unverified.
    site_id = "jobkorea"
    allowed_hosts = frozenset({"www.jobkorea.co.kr"})


class IncruitCrawler(HtmlSiteCrawler):
    """Fail-closed adapter; live React shell may expose no stable /jobs/<id> links."""
    site_id = 'incruit'
    allowed_hosts = frozenset({'lab.incruit.com'})


class ZighangCrawler(HtmlSiteCrawler):
    """Fail-closed adapter; not a claim that the live JS app is SSR-rendered."""
    site_id = 'zighang'
    allowed_hosts = frozenset({'zighang.com', 'www.zighang.com'})
