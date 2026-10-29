"""Opt-in, directly-listed company career pages with schema.org/JobPosting JSON-LD.

No search-engine harvesting, crawling arbitrary discovered links, hidden APIs,
anti-bot bypass, or implicit authorization. Each URL must be explicitly listed.
"""
from __future__ import annotations
import hashlib
import json
import os
import ipaddress
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from .base import BaseCrawler, JobRef
from ..schema import JobDraft
from ..fetcher import PoliteFetcher


def configured_pages(path: str | None = None) -> list[dict]:
    path = path or os.getenv('COMPANY_JOB_PAGES_FILE', '')
    if not path:
        return []
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    pages = doc.get('pages') if isinstance(doc, dict) else None
    if not isinstance(pages, list) or len(pages) > 100:
        raise ValueError('Company pages file needs pages array of <=100 URLs')
    out = []
    for row in pages:
        if not isinstance(row, dict) or not row.get('authorized') is True:
            raise PermissionError('Every company page must have authorized=true after review')
        url = str(row.get('url', ''))
        part = urlsplit(url)
        if part.scheme != 'https' or not part.hostname or part.username or part.password or part.port not in (None, 443):
            raise ValueError('Company page URLs must be public HTTPS URLs without custom ports')
        try:
            ipaddress.ip_address(part.hostname)
        except ValueError:
            pass
        else:
            raise ValueError('Company page URLs cannot be IP literals')
        if part.hostname in {'localhost','localhost.localdomain'} or part.hostname.endswith(('.local','.internal','.test')):
            raise ValueError('Company page URLs must be externally hosted public domains')
        if not isinstance(row.get('company'), str) or not row['company'].strip():
            raise ValueError('Each company page needs its company name')
        out.append({'url':url,'company':row['company'].strip()})
    if len({r['url'] for r in out}) != len(out):
        raise ValueError('Repeated company page URL in config')
    return out


def _posting_objects(doc):
    if isinstance(doc, list):
        for item in doc:
            yield from _posting_objects(item)
    elif isinstance(doc, dict):
        kinds = doc.get('@type', [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if 'JobPosting' in kinds or 'https://schema.org/JobPosting' in kinds:
            yield doc
        for key in ('@graph','itemListElement'):
            if key in doc:
                yield from _posting_objects(doc[key])
        if 'item' in doc:
            yield from _posting_objects(doc['item'])


def _parse_date(raw):
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z','+00:00')).date()
    except ValueError:
        return None


def parse_jsonld(html: str, page_url: str, organization: str) -> list[JobDraft]:
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    page_host = urlsplit(page_url).hostname
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for item in _posting_objects(data):
            title = str(item.get('title') or '').strip()
            if not title:
                continue
            url = str(item.get('url') or page_url)
            part = urlsplit(url)
            if part.scheme != 'https' or part.hostname != page_host or part.username or part.password:
                url = page_url  # Canonical ID is based on the operator-approved page.
            org = item.get('hiringOrganization') or {}
            name = org.get('name') if isinstance(org, dict) else organization
            place = item.get('jobLocation') or {}
            if isinstance(place, list):
                place = place[0] if place else {}
            address = place.get('address', {}) if isinstance(place, dict) else {}
            if isinstance(address, dict):
                region = ', '.join(str(address.get(k)) for k in ('addressRegion','addressLocality','addressCountry') if address.get(k)) or None
            else:
                region = str(address) if address else None
            description = item.get('description') or ''
            desc = BeautifulSoup(str(description), 'html.parser').get_text('\n', strip=True)
            identity = item.get('identifier') or {}
            if isinstance(identity, dict):
                identity = identity.get('value')
            stable = str(identity or url)
            external_id = hashlib.sha256((page_host + '|' + stable).encode()).hexdigest()[:32]
            expires = _parse_date(item.get('validThrough'))
            result.append(JobDraft(source_id='company_jsonld', external_id=external_id,
                title=title, original_url=url, apply_url=url,
                organization=str(name or organization), region=region,
                employment_type=str(item.get('employmentType') or '') or None,
                recruitment_type='채용', body_text=desc or None,
                posted_on=_parse_date(item.get('datePosted')), deadline_on=expires,
                source_status='open'))
    unique = {row.external_id:row for row in result}
    return list(unique.values())


class CompanyJsonLdCrawler(BaseCrawler):
    site_id = 'company_jsonld'

    def __init__(self, *, max_pages: int | None = None):
        self.pages = configured_pages()
        self.allowed_hosts = frozenset(urlsplit(p['url']).hostname for p in self.pages)
        if max_pages is not None and max_pages < 1:
            raise ValueError('max_pages must be positive')
        self.pages = self.pages[:max_pages] if max_pages else self.pages
        if not self.allowed_hosts:  # Instantiating without a config is useful for access/status UI.
            self.allowed_hosts = frozenset({'example.org'})

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        for page in self.pages:
            scope = page['url']
            try:
                response = await fetcher.get_response(scope, capture=True, accept='text/html')
                if response.status != 200 or not response.text:
                    raise ValueError('No HTML response for configured page')
                items = parse_jsonld(response.text, scope, page['company'])
                if not items:
                    raise ValueError('No JobPosting JSON-LD found; no closure inference')
                self.completed_scopes[scope] = {j.external_id for j in items}
                for draft in items:
                    fetcher.capture_rendered_html(draft.original_url, response.text)
                    yield JobRef(draft.external_id, draft.original_url,
                                 hashlib.sha256(draft.model_dump_json().encode()).hexdigest(),
                                 payload={'draft':draft.model_dump(mode='json'), 'scope_id':scope})
            except Exception as exc:
                self.completed_scopes.pop(scope, None)
                self.scope_errors.append(f'{scope}: {type(exc).__name__}: {str(exc)[:140]}')

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        return JobDraft.model_validate(ref.payload['draft'])
