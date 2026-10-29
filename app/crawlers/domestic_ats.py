"""Opt-in domestic ATS HTML adapter for Greeting and Ninehire.

This module has no undocumented API calls. It extracts visible same-origin
job links and permitted public detail pages. It fails closed when a layout
is unrecognized. An authorized=true flag is an operator attestation only.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler, JobRef
from .company_pages import parse_jsonld
from ..fetcher import PoliteFetcher
from ..schema import JobDraft

# Public URL structure; not a license to use the source.
GREETING_PATH = re.compile(r"^/(?:[a-z]{2}/)?o/(?P<id>\d+)(?:/)?$", re.I)
NINEHIRE_PATH = re.compile(r"^/job_posting/(?P<id>[A-Za-z0-9_-]+)(?:/)?$", re.I)
SUPPORTED = {'greeting', 'ninehire'}


def _safe_url(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('HTTPS, public domain and default port required')
    h = p.hostname.lower()
    try:
        ipaddress.ip_address(h)
        raise ValueError('IP literal disallowed')
    except ValueError as exc:
        if str(exc) == 'IP literal disallowed':
            raise
    if h in ('localhost', 'localhost.localdomain') or h.endswith(('.local', '.test', '.internal')):
        raise ValueError('Private host disallowed')
    return h, urlunsplit(('https', h, p.path or '/', p.query, ''))


def load_domestic_config(path: str | None = None) -> list[dict]:
    path = path or os.getenv('DOMESTIC_ATS_FILE', '')
    if not path:
        return []
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = doc.get('boards') if isinstance(doc, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('Expected boards array with <=500 entries')
    result, known = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('authorized') is not True:
            raise PermissionError('Every domestic ATS board requires explicit authorized=true')
        family = row.get('family')
        if family not in SUPPORTED:
            raise ValueError('Expected greeting or ninehire family')
        hostname, url = _safe_url(str(row.get('url', '')))
        if family == 'greeting' and not (hostname == 'career.greetinghr.com' or hostname.endswith('.career.greetinghr.com')):
            raise ValueError('Greeting family only accepts verified greetinghr hostname')
        if family == 'ninehire' and not (hostname == 'ninehire.site' or hostname.endswith('.ninehire.site')):
            raise ValueError('Ninehire family only accepts verified ninehire hostname')
        if not isinstance(row.get('company'), str) or not row['company'].strip():
            raise ValueError('company is required')
        if url in known:
            raise ValueError('Duplicate board URL')
        known.add(url)
        # Do not let user-configured selectors discover arbitrary hrefs.
        selector = row.get('link_selector', 'a[href]')
        if not isinstance(selector, str) or not selector or len(selector) > 180:
            raise ValueError('Invalid link selector')
        result.append({**row, 'url': url, 'link_selector': selector, 'company': row['company'].strip()})
    return result


def _job_parts(href: str, board_url: str, family: str):
    origin, _ = _safe_url(board_url)
    p = urlsplit(urljoin(board_url, href))
    if p.scheme != 'https' or p.hostname != origin or p.username or p.password or p.port not in (None, 443):
        return None
    # /apply is a personal-data form, not job description; intentionally exclude.
    match = (GREETING_PATH if family == 'greeting' else NINEHIRE_PATH).fullmatch(p.path.rstrip('/') or '/')
    if match is None:
        return None
    return match.group('id'), urlunsplit(('https', origin, p.path.rstrip('/'), '', ''))


def extract_domestic_links(html: str, board: dict) -> list[tuple[str, str, str]]:
    """(stable provider ID, canonical job detail URL, link title).

    Ignore job cards without an actual valid URL. Never invent an ID from title.
    """
    soup = BeautifulSoup(html, 'html.parser')
    found: dict[str, tuple[str, str, str]] = {}
    for a in soup.select(board.get('link_selector') or 'a[href]'):
        if a.name != 'a' or not a.get('href'):
            continue
        parts = _job_parts(str(a['href']), board['url'], board['family'])
        if not parts:
            continue
        job_id, canonical = parts
        title = a.get_text(' ', strip=True) or a.get('aria-label') or a.get('title') or ''
        title = str(title).strip()[:240]
        if not title or re.fullmatch(r'(지원하기|자세히 보기|상세 보기|view|apply)', title, re.I):
            continue
        found[canonical] = (job_id, canonical, title)
        if len(found) >= 1000:
            break
    return list(found.values())


def diagnose_listing(html: str, board: dict) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    links = extract_domestic_links(html, board)
    visible = soup.get_text(' ', strip=True)
    return {
        'family': board['family'], 'company': board['company'], 'recognized_jobs': len(links),
        'sample_urls': [j[1] for j in links[:3]],
        'possible_client_rendering': bool(soup.select_one('script#__NEXT_DATA__,div#__nuxt,script[data-nuxt-data]')),
        'html_chars': len(html), 'visible_chars': len(visible),
        'outcome': 'candidate_links_found' if links else 'manual_render_or_selector_review',
        'note': 'Offline HTML inspection does not confirm access rights, live parsing, or complete pagination.'
    }


def _date_from(text: str):
    m = re.search(r'(20\d{2})[./년\- ]+\s*(\d{1,2})[./월\- ]+\s*(\d{1,2})', text)
    if m:
        try:
            return datetime(*map(int, m.groups())).date()
        except ValueError:
            pass
    return None


def _label_value(soup: BeautifulSoup, label: str) -> str | None:
    # Common ATS layout variant: <dt>경력사항</dt><dd>신입</dd>.
    for dt in soup.select('dt'):
        if dt.get_text(' ', strip=True).strip(': ') == label:
            dd = dt.find_next_sibling('dd')
            if dd:
                value = dd.get_text(' ', strip=True)
                if value and len(value) <= 200:
                    return value
    # Common label-value components, including semantic table layouts.
    for tr in soup.select('tr'):
        cells = tr.select('th,td')
        if len(cells) == 2 and cells[0].get_text(' ', strip=True).strip(': ') == label:
            value = cells[1].get_text(' ', strip=True)
            if 0 < len(value) <= 200:
                return value
    return None


def parse_domestic_detail(html: str, url: str, board: dict, *, listing_title: str = '') -> JobDraft:
    parts = _job_parts(url, board['url'], board['family'])
    if parts is None:
        raise ValueError('Detail URL does not match same-origin family job URL pattern')
    job_id, canonical = parts
    external_id = hashlib.sha256((urlsplit(canonical).hostname + '|' + job_id).encode()).hexdigest()[:32]
    # Prefer published schema.org markup over guessing from components.
    jsonld = parse_jsonld(html, canonical, board['company'])
    if jsonld:
        for draft in jsonld:
            if draft.title and draft.body_text and len(draft.body_text) >= 40:
                return draft.model_copy(update={'source_id':'domestic_ats','external_id':external_id,
                                                'original_url':canonical, 'apply_url':draft.apply_url or canonical,
                                                'organization':board['company']})
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup.select('script,style,noscript,nav,footer,form,aside'):
        element.decompose()
    h1 = soup.select_one('h1')
    title = (h1.get_text(' ',strip=True) if h1 else '') or listing_title
    main = soup.select_one('main,article,[role="main"]') or soup.body
    text = main.get_text('\n', strip=True) if main else ''
    if not title or not text or len(text) < 80:
        raise ValueError('Unrecognized/short job detail. No fabricated posting')
    if re.search(r'로그인|이름|전화번호', title) and re.search(r'지원서 작성', text[:200]):
        raise ValueError('Application form is not a public job detail')
    career = _label_value(soup, '경력사항')
    employment = _label_value(soup, '고용형태')
    region = _label_value(soup, '근무지')
    deadline = _label_value(soup, '마감기한')
    # Avoid company brochure context as job metadata. Never claim open/closed
    # unless an explicitly represented status is recognized.
    status_text = _label_value(soup, '모집상태')
    status = 'closed' if status_text in ('마감', '모집마감', '접수마감') else 'open' if status_text in ('접수중', '모집중') else 'unknown'
    return JobDraft(source_id='domestic_ats',external_id=external_id, title=title,
                    original_url=canonical,apply_url=canonical,organization=board['company'],
                    recruitment_type='채용',employment_type=employment,
                    region=region,deadline_on=_date_from(deadline or '') if deadline else None,
                    deadline_text=deadline if deadline and not _date_from(deadline) else None,
                    body_text=(f'경력사항: {career}\n' if career else '')+text[:30000],source_status=status)


class DomesticATSCrawler(BaseCrawler):
    site_id = 'domestic_ats'

    def __init__(self, *, max_pages: int | None = None):
        boards = load_domestic_config()
        if max_pages is not None and max_pages < 1:
            raise ValueError('max_pages must be >=1')
        self.boards = boards[:max_pages] if max_pages else boards
        self.allowed_hosts = frozenset(urlsplit(b['url']).hostname for b in self.boards) or frozenset({'example.org'})

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        for board in self.boards:
            try:
                cache = getattr(self, 'listing_cache', None)
                response = None
                if cache is not None:
                    # Conditional request (304) avoids downloading the listing
                    # body. The shared cache schedules a periodic full scan.
                    response = await fetcher.get_response(board['url'],
                        headers=cache.headers_for(self.site_id,board['url']), accept='text/html')
                    if response.status == 304:
                        cache.update(self.site_id,board['url'],status=304)
                        continue
                    html = response.text or ''
                else:
                    html = await fetcher.get_text(board['url'])
                links = extract_domestic_links(html, board)
                # A second browser request is opt-in, never a block/captcha bypass.
                if not links and os.getenv('RENDER_DOMESTIC_ATS_JS') == '1':
                    html = await fetcher.render_html(board['url'])
                    links = extract_domestic_links(html, board)
                if not links:
                    raise ValueError('No validated visible job detail links; manual layout review needed')
                if cache is not None:
                    should_parse = cache.update(self.site_id,board['url'],status=200,text=html,
                        etag=response.etag,last_modified=response.last_modified)
                    if not should_parse:
                        continue
                for job_id, canonical, title in links:
                    stable = hashlib.sha256((urlsplit(canonical).hostname + '|' + job_id).encode()).hexdigest()[:32]
                    yield JobRef(stable, canonical, None, payload={'board':board,'title':title,'scope_id':board['url']})
            except Exception as exc:
                self.scope_errors.append(f"{board['url']}: {type(exc).__name__}: {str(exc)[:140]}")
        # A single sampled HTML list is not proof of full pagination; no closure inference.

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        html = await fetcher.get_text(ref.url, capture=True)
        draft = parse_domestic_detail(html, ref.url, ref.payload['board'], listing_title=ref.payload['title'])
        if draft.external_id != ref.external_id:
            raise ValueError('Stable job ID mismatch')
        return draft
