"""Documented, read-only public recruitment feeds. No API keys or private endpoints.

Publicly reachable is NOT a license to republish. This module only issues GET to
well-documented posting endpoints. Keep source attribution and original links.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import BaseCrawler, JobRef
from ..fetcher import PoliteFetcher
from ..schema import JobDraft
from ..board_catalog import default_ids, company_name
from ..feed_cache import public_json

IDENTIFIER = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


def _boards(var: str, default: str = '') -> list[str]:
    names = [v.strip() for v in os.getenv(var, default).split(',') if v.strip()]
    if len(names) > 20000:
        raise ValueError(f'Too many configured boards in {var}; use collection batches')
    if any(not IDENTIFIER.fullmatch(v) for v in names):
        raise ValueError(f'{var} only accepts company board names (alphanumeric, _ and -)')
    return list(dict.fromkeys(names))


def _fingerprint(row: dict, updated=None) -> str:
    # Hash the full feed row if no trustworthy revision field is present.
    if updated:
        return str(updated)
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    output = BeautifulSoup(html, 'html.parser').get_text('\n', strip=True)
    return output or None


def _date_iso(value) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def _date_epoch_ms(value) -> date | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date()
    except (OverflowError, ValueError, TypeError, OSError):
        return None


def _public_url(url: str, hosts: set[str]) -> str:
    """Reject unexpected URL protocols and off-site links in provider-supplied URLs."""
    part = urlsplit(url)
    if part.scheme != 'https' or part.hostname not in hosts or part.username or part.password:
        raise ValueError('Unexpected non-public job link in feed')
    return url


GH_ORGANIZATIONS = {
    'get-delight': 'delight.ai (Sendbird)',
    'seoulrobotics': 'Seoul Robotics',
    'marqvision': 'MarqVision',
    'moloco': 'Moloco',
}


def parse_greenhouse(row: dict, board: str) -> JobDraft:
    if not isinstance(row, dict) or not row.get('id') or not row.get('title'):
        raise ValueError('Greenhouse row requires id and title')
    job_id = str(row['id'])
    # Greenhouse supports custom job post URLs controlled by employers. Preserve
    # strict outbound-host safety by linking to the provider-hosted fallback
    # when the feed points at a custom domain (rather than discarding the post).
    supplied_url = str(row.get('absolute_url') or '')
    if supplied_url and urlsplit(supplied_url).scheme != 'https':
        raise ValueError('Greenhouse job URL must use HTTPS')
    try:
        url = _public_url(supplied_url,
                          {'job-boards.greenhouse.io', 'boards.greenhouse.io', 'boards-api.greenhouse.io'})
    except ValueError:
        url = f'https://job-boards.greenhouse.io/{board}/jobs/{job_id}'
    location = row.get('location') or {}
    region = location.get('name') if isinstance(location, dict) else str(location)
    return JobDraft(source_id='public_greenhouse', external_id=f'{board}:{job_id}',
                    title=str(row['title']), organization=company_name("greenhouse", board),
                    original_url=url, apply_url=url, region=region,
                    body_text=_html_to_text(row.get('content')),
                    recruitment_type='채용', source_status='open')


class GreenhousePublicCrawler(BaseCrawler):
    site_id = 'public_greenhouse'
    allowed_hosts = frozenset({'boards-api.greenhouse.io'})

    def __init__(self, *, max_pages: int | None = None):
        self.boards = _boards('PUBLIC_GREENHOUSE_BOARDS', default_ids('greenhouse'))
        if max_pages not in (None, 1):
            raise ValueError('Greenhouse publishes a single jobs list per company; max_pages must be 1')

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        for board in self.boards:
            seen = set()
            try:
                response = await public_json(fetcher, site=self.site_id,
                    url=f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs',
                    params={'content': 'true'}, listing_cache=getattr(self,'listing_cache',None))
                rows = response.get('jobs') if isinstance(response, dict) else None
                if not isinstance(rows, list):
                    raise ValueError(f'{board}: invalid Greenhouse jobs payload')
                for row in rows:
                    draft = parse_greenhouse(row, board)
                    seen.add(draft.external_id)
                    yield JobRef(draft.external_id, draft.original_url,
                                 _fingerprint(row, row.get('updated_at')), payload={'board': board, 'data': row})
            except Exception as exc:
                self.scope_errors.append(f'{board}: {type(exc).__name__}: {str(exc)[:140]}')
            else:
                self.completed_scopes[board] = seen

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if not ref.payload:
            raise ValueError('Greenhouse list is expected to contain job details')
        return parse_greenhouse(ref.payload['data'], ref.payload['board'])


LEVER_ORGANIZATIONS = {'zoyi': '채널코퍼레이션 (채널톡)', 'palantir': 'Palantir Technologies',
                       'clovirtualfashion': 'CLO Virtual Fashion', 'matchgroup': 'Match Group'}


def parse_lever(row: dict, company: str) -> JobDraft:
    if not isinstance(row, dict) or not row.get('id') or not row.get('text'):
        raise ValueError('Lever row requires id and text')
    url = _public_url(str(row.get('hostedUrl') or ''), {'jobs.lever.co', 'jobs.eu.lever.co'})
    categories = row.get('categories') or {}
    categories = categories if isinstance(categories, dict) else {}
    plain = row.get('descriptionPlain') or row.get('descriptionBodyPlain') or row.get('openingPlain')
    parts=[plain or _html_to_text(row.get('description'))]
    for section in row.get('lists') or []:
        if isinstance(section,dict):
            parts.extend([section.get('text'),_html_to_text(section.get('content'))])
    parts.append(row.get('additionalPlain') or _html_to_text(row.get('additional')))
    body='\n\n'.join(dict.fromkeys(part for part in parts if isinstance(part,str) and part.strip())) or None
    return JobDraft(source_id='public_lever', external_id=f"{company}:{row['id']}",
                    title=str(row['text']), original_url=url, organization=company_name("lever", company),
                    region=categories.get('location'),
                    employment_type=categories.get('commitment'),
                    recruitment_type='채용',
                    posted_on=_date_epoch_ms(row.get('createdAt')),
                    body_text=body,
                    apply_url=_public_url(str(row.get('applyUrl') or url), {'jobs.lever.co','jobs.eu.lever.co'}),
                    source_status='open')


class LeverPublicCrawler(BaseCrawler):
    site_id = 'public_lever'
    allowed_hosts = frozenset({'api.lever.co', 'api.eu.lever.co'})

    def __init__(self, *, max_pages: int | None = None):
        self.boards = _boards('PUBLIC_LEVER_SITES', default_ids('lever'))
        self.max_pages = max_pages or 2
        if not 1 <= self.max_pages <= 100:
            raise ValueError('max_pages must be 1..100')
        self.eu = os.getenv('PUBLIC_LEVER_REGION') == 'eu'

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        host = 'api.eu.lever.co' if self.eu else 'api.lever.co'
        for site in self.boards:
            seen, complete = set(), False
            try:
                for page in range(self.max_pages):
                    rows = await public_json(fetcher, site=self.site_id,
                        url=f'https://{host}/v0/postings/{site}',
                        params={'mode': 'json', 'skip': page*100, 'limit': 100},
                        listing_cache=getattr(self,'listing_cache',None))
                    if not isinstance(rows, list):
                        raise ValueError(f'{site}: invalid Lever postings payload')
                    for row in rows:
                        draft = parse_lever(row, site)
                        seen.add(draft.external_id)
                        yield JobRef(draft.external_id, draft.original_url,
                                     _fingerprint(row, row.get('updatedAt')), payload={'company': site, 'data': row})
                    if len(rows) < 100:
                        complete = True
                        break
                if not complete:
                    self.scope_errors.append(f'{site}: pagination cap reached; scope reconciliation disabled')
            except Exception as exc:
                self.scope_errors.append(f'{site}: {type(exc).__name__}: {str(exc)[:140]}')
            else:
                if complete:
                    self.completed_scopes[site] = seen

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if not ref.payload:
            raise ValueError('Lever list is expected to contain descriptions')
        return parse_lever(ref.payload['data'], ref.payload['company'])


ASHBY_ORGANIZATIONS = {'furiosa-ai': '퓨리오사AI (FuriosaAI)'}


def parse_ashby(row: dict, company: str) -> JobDraft:
    if not isinstance(row, dict) or not row.get('title'):
        raise ValueError('Ashby row requires title')
    job_url = _public_url(str(row.get('jobUrl') or ''), {'jobs.ashbyhq.com'})
    external_key = row.get('id') or hashlib.sha256(job_url.encode()).hexdigest()[:24]
    return JobDraft(source_id='public_ashby', external_id=f'{company}:{external_key}',
                    title=str(row['title']), original_url=job_url, organization=company_name("ashby", company),
                    region=row.get('location'), employment_type=row.get('employmentType'),
                    recruitment_type='채용', posted_on=_date_iso(row.get('publishedAt')),
                    body_text=row.get('descriptionPlain') or _html_to_text(row.get('descriptionHtml')),
                    apply_url=_public_url(str(row.get('applyUrl') or job_url), {'jobs.ashbyhq.com'}),
                    source_status='open')


class AshbyPublicCrawler(BaseCrawler):
    site_id = 'public_ashby'
    allowed_hosts = frozenset({'api.ashbyhq.com','jobs.ashbyhq.com'})

    def __init__(self, *, max_pages: int | None = None):
        self.boards = _boards('PUBLIC_ASHBY_BOARDS', default_ids('ashby'))
        if max_pages not in (None, 1):
            raise ValueError('Ashby publishes a single jobs list per company; max_pages must be 1')

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        for company in self.boards:
            seen = set()
            try:
                try:
                    rows_doc = await public_json(fetcher, site=self.site_id,
                        url=f'https://api.ashbyhq.com/posting-api/job-board/{company}',
                        listing_cache=getattr(self,'listing_cache',None))
                except PermissionError:
                    # Separate, robots-permitted official hosted page. Never bypass the API gate.
                    app_data=ashby_app_data(await fetcher.get_text(f'https://jobs.ashbyhq.com/{company}'))
                    postings=(app_data.get('jobBoard') or {}).get('jobPostings')
                    if not isinstance(postings,list):raise ValueError('Ashby hosted listing missing')
                    for brief in postings:
                        identifier=brief.get('id','')
                        if not re.fullmatch(r'[0-9a-f-]{36}',identifier):raise ValueError('Invalid Ashby hosted ID')
                        if brief.get('isListed') is False:continue
                        external=f'{company}:{identifier}'
                        seen.add(external)
                        yield JobRef(external,f'https://jobs.ashbyhq.com/{company}/{identifier}',
                            payload={'company':company,'hosted':True})
                    self.completed_scopes[company]=seen
                    continue
                rows = rows_doc.get('jobs') if isinstance(rows_doc, dict) else None
                if not isinstance(rows, list):
                    raise ValueError(f'{company}: invalid Ashby jobs payload')
                for row in rows:
                    if row.get('isListed') is False:
                        continue
                    draft = parse_ashby(row, company)
                    seen.add(draft.external_id)
                    yield JobRef(draft.external_id, draft.original_url,
                                 _fingerprint(row), payload={'company': company, 'data': row})
            except Exception as exc:
                self.scope_errors.append(f'{company}: {type(exc).__name__}: {str(exc)[:140]}')
            else:
                self.completed_scopes[company] = seen

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if not ref.payload:
            raise ValueError('Ashby list is expected to contain descriptions')
        if ref.payload.get('hosted'):
            app_data=ashby_app_data(await fetcher.get_text(ref.url,capture=True))
            posting=app_data.get('posting') or {}
            if posting.get('isConfidential') or posting.get('isListed') is False:
                raise ValueError('Ashby posting is not publicly listed')
            if not posting.get('title') or not posting.get('descriptionHtml'):raise ValueError('Ashby hosted detail missing')
            return JobDraft(source_id=self.site_id,external_id=ref.external_id,title=posting['title'],
                original_url=ref.url,organization=(app_data.get('organization') or {}).get('name'),
                region=posting.get('locationName'),employment_type=posting.get('employmentType'),
                body_text=_html_to_text(posting['descriptionHtml']),recruitment_type='채용',
                deadline_on=_date_iso(posting.get('applicationDeadline')),apply_url=ref.url+'/application',source_status='open')
        return parse_ashby(ref.payload['data'], ref.payload['company'])


def ashby_app_data(html):
    match=re.search(r'window\.__appData\s*=\s*',html)
    if not match:raise ValueError('No public Ashby bootstrap data')
    data,_=json.JSONDecoder().raw_decode(html[match.end():])
    if not isinstance(data,dict):raise ValueError('Invalid Ashby hosted document')
    return data


def parse_smartrecruiters(row: dict, company: str, original_url: str) -> JobDraft:
    if not isinstance(row, dict) or not row.get('id') or not row.get('name'):
        raise ValueError('SmartRecruiters row requires id and name')
    location = row.get('location') or {}
    location_text = ', '.join(filter(None, (location.get('city'), location.get('region'), location.get('country')))) if isinstance(location, dict) else None
    comp = row.get('company') or {}
    employment = row.get('typeOfEmployment') or {}
    sections = ((row.get('jobAd') or {}).get('sections') or {})
    body = '\n'.join(_html_to_text(v.get('text')) or '' for v in sections.values() if isinstance(v, dict)).strip()
    apply_url = row.get('applyUrl')
    # The source link is a stable posting location; application links (where provided)
    # should be preferred in the UI. The URL below is an adapter convention.
    return JobDraft(source_id='public_smartrecruiters', external_id=f"{company}:{row['id']}",
                    title=str(row['name']), original_url=original_url,
                    organization=comp.get('name') or company, region=location_text,
                    employment_type=employment.get('label') if isinstance(employment, dict) else None,
                    recruitment_type='채용', posted_on=_date_iso(row.get('releasedDate')),
                    body_text=body or None,
                    apply_url=_public_url(str(apply_url), {'jobs.smartrecruiters.com','www.smartrecruiters.com'}) if apply_url else None,
                    source_status='open')


class SmartRecruitersPublicCrawler(BaseCrawler):
    site_id = 'public_smartrecruiters'
    allowed_hosts = frozenset({'api.smartrecruiters.com'})

    def __init__(self, *, max_pages: int | None = None):
        self.boards = _boards('PUBLIC_SMARTRECRUITERS_COMPANIES', default_ids('smartrecruiters'))
        self.max_pages = max_pages or 2
        if not 1 <= self.max_pages <= 100:
            raise ValueError('max_pages must be 1..100')

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes, self.scope_errors = {}, []
        for company in self.boards:
            seen, complete = set(), False
            try:
                for page in range(self.max_pages):
                    doc = await public_json(fetcher, site=self.site_id,
                        url=f'https://api.smartrecruiters.com/v1/companies/{company}/postings',
                        params={'limit': 100, 'offset': page*100, 'destination': 'PUBLIC', 'country':os.getenv('PUBLIC_SR_COUNTRY','kr')},
                        listing_cache=getattr(self,'listing_cache',None))
                    rows = doc.get('content') if isinstance(doc, dict) else None
                    if not isinstance(rows, list):
                        raise ValueError(f'{company}: invalid SmartRecruiters postings payload')
                    for row in rows:
                        if not row.get('id') or not row.get('name'):
                            raise ValueError(f'{company}: missing ID or title in listing')
                        url = f'https://jobs.smartrecruiters.com/{company}/{row["id"]}'
                        key = f'{company}:{row["id"]}'
                        seen.add(key)
                        yield JobRef(external_id=key, url=url, listing_fingerprint=_fingerprint(row),
                                     payload={'company': company, 'data': row})
                    if len(rows) < 100:
                        complete = True
                        break
                if not complete:
                    self.scope_errors.append(f'{company}: pagination cap reached; scope reconciliation disabled')
            except Exception as exc:
                self.scope_errors.append(f'{company}: {type(exc).__name__}: {str(exc)[:140]}')
            else:
                if complete:
                    self.completed_scopes[company] = seen

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if not ref.payload:
            raise ValueError('Missing SmartRecruiters posting row')
        company = ref.payload['company']
        job_id = ref.payload['data']['id']
        detail = await fetcher.get_json(f'https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}')
        return parse_smartrecruiters(detail, company, ref.url)
