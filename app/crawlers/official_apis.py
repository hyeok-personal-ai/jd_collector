"""Licensed API adapters. Never guess undocumented endpoints or credentials.

Saramin follows its published Job Search JSON documentation. Wanted requires the
provider-approved feed URL and credentials. No private website APIs are used.
"""
from __future__ import annotations
import os
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from urllib.parse import urlsplit
from .base import BaseCrawler, JobRef
from ..schema import JobDraft
from ..fetcher import PoliteFetcher


def _string(value):
    if isinstance(value, dict):
        for key in ('name', '#text', 'text', 'value', 'title'):
            if value.get(key):
                return _string(value[key])
        return None
    return str(value).strip() if value is not None and str(value).strip() else None


def _timestamp_day(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul')).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_saramin_job(job: dict) -> JobDraft:
    if not isinstance(job, dict) or not str(job.get('id', '')).isdigit():
        raise ValueError('Saramin job requires numeric id')
    pos = job.get('position') or {}
    if not isinstance(pos, dict):
        raise ValueError('Invalid Saramin position data')
    title = _string(pos.get('title'))
    if not title:
        raise ValueError('Saramin job missing title')
    jid = str(job['id'])
    original = f'https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx={jid}'
    close_raw = job.get('close-type')
    close_type = _string(close_raw)
    close_code = str(close_raw.get('code', '')) if isinstance(close_raw, dict) else ''
    ongoing = close_code in {'2', '3', '4'} or close_type in {'채용시', '상시', '수시'}
    status = {1: 'open', 0: 'closed'}.get(int(job['active'])) if str(job.get('active')) in {'0','1'} else 'unknown'
    return JobDraft(
        source_id='saramin', external_id=jid, title=title, original_url=original,
        organization=_string((job.get('company') or {}).get('name') or (job.get('company') or {}).get('detail', {}).get('name')),
        region=_string(pos.get('location')),
        employment_type=_string(pos.get('job-type')),
        recruitment_type='채용',
        posted_on=_timestamp_day(job.get('posting-timestamp')),
        deadline_on=None if ongoing else _timestamp_day(job.get('expiration-timestamp')),
        deadline_text=close_type if ongoing else None,
        source_status=status,
        # Official job-search API gives summary metadata, not full HTML detail.
        body_text=None,
    )


def saramin_jobs(document: dict) -> list[dict]:
    if not isinstance(document, dict):
        raise ValueError('Invalid Saramin API root')
    if 'result' in document or ('code' in document and 'jobs' not in document):
        raise PermissionError('Saramin API returned an error; check access-key and quota')
    top = document.get('job-search', document)
    if not isinstance(top, dict):
        raise ValueError('Invalid Saramin API structure')
    jobs = top.get('jobs') or {}
    if not isinstance(jobs, dict):
        raise ValueError('Invalid Saramin jobs structure')
    rows = jobs.get('job', [])
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError('Invalid Saramin jobs array')
    return rows


class SaraminApiCrawler(BaseCrawler):
    site_id = 'saramin'
    allowed_hosts = frozenset({'oapi.saramin.co.kr'})
    API_URL = 'https://oapi.saramin.co.kr/job-search'

    def __init__(self, *, max_pages: int | None = None):
        self.max_pages = min(100, max_pages or int(os.getenv('MAX_LIST_PAGES', '2')))
        if self.max_pages < 1:
            raise ValueError('max_pages must be positive')
        self._backfill = os.getenv('CRAWL_BACKFILL', '') == '1'

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        key = os.getenv('SARAMIN_ACCESS_KEY', '').strip()
        if not key:
            raise PermissionError('SARAMIN_ACCESS_KEY not configured: apply at https://oapi.saramin.co.kr/')
        # '22' is IT개발·데이터 in published sample. Match original filter approximately;
        # category/experience mapping is not identical to the original HTML UI.
        params = {'access-key': key, 'sort': 'ud', 'start': 0,
                  'count': 100, 'job_mid_cd': '22', 'fields': 'posting-date expiration-date'}
        consecutive_old = 0
        for page in range(self.max_pages):
            # Official guide defines start as a zero-based PAGE INDEX.
            params['start'] = page
            quota = getattr(self, 'api_quota', None)
            before_request = (lambda: quota.reserve('saramin', limit=int(os.getenv('SARAMIN_DAILY_REQUEST_CAP', '450')))) if quota else None
            kwargs = {'before_request': before_request} if before_request else {}
            doc = await fetcher.get_json(self.API_URL, params=params, **kwargs)
            rows = saramin_jobs(doc)
            if not rows:
                break
            refs = []
            for job in rows:
                draft = parse_saramin_job(job)
                exp = (job.get('position') or {}).get('experience-level') or {}
                code = str(exp.get('code') if isinstance(exp, dict) else '')
                # Include fresh / no-experience / mixed jobs; reject known experienced-only.
                if code == '2':
                    continue
                mod = str(job.get('modification-timestamp') or '') or None
                # If a feed omits modification timestamps, fingerprint its row;
                # this catches edits without extra detail requests.
                if mod is None:
                    mod = 'row:' + hashlib.sha256(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                refs.append(JobRef(draft.external_id, draft.original_url, mod, payload=job))
            if not refs:
                if len(rows) < 100:
                    break
                continue
            known = self.listing_cache.count_known_unchanged(self.site_id, refs) if hasattr(self, 'listing_cache') else 0
            for ref in refs:
                yield ref
            # Run at least first page; stop only after 2 fully-known pages, unless backfilling.
            consecutive_old = consecutive_old + 1 if known == len(refs) else 0
            if consecutive_old >= 2 and not self._backfill:
                break
            if len(rows) < 100:
                break

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if ref.payload is None:
            raise ValueError('Saramin API reference must include feed payload')
        return parse_saramin_job(ref.payload)


def wanted_rows(data) -> list[dict]:
    """Accept only a configured provider feed with a documented item envelope.

    This is a *mapping template*, not a claim about the undocumented live JSON.
    """
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get('jobs', data.get('results', data.get('items', data.get('data'))))
        if isinstance(rows, dict):
            rows = rows.get('jobs', rows.get('items', rows.get('results')))
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError('Wanted API response not mapped; inspect licensed schema and update adapter')
    return rows


def parse_wanted_job(data: dict) -> JobDraft:
    if not isinstance(data, dict):
        raise ValueError('Invalid Wanted row')
    jid = str(data.get('id') or '')
    title = _string(data.get('title') or data.get('position'))
    if not jid.isdigit() or not title:
        raise ValueError('Wanted mapping requires numeric id and title; revise to match licensed response')
    company = data.get('company') or {}
    org = _string(company) if not isinstance(company, dict) else _string(company.get('name'))
    return JobDraft(source_id='wanted', external_id=jid, title=title,
                    original_url=f'https://www.wanted.co.kr/wd/{jid}', organization=org,
                    region=_string(data.get('location')),
                    body_text=_string(data.get('description')),
                    recruitment_type='채용')


class WantedApiCrawler(BaseCrawler):
    """Key + approved URL required; configurable endpoint intentionally not invented."""
    site_id = 'wanted'
    allowed_hosts = frozenset({'openapi.wanted.jobs'})

    def __init__(self, *, max_pages: int | None = None):
        self.max_pages = max_pages or 1
        if self.max_pages != 1:
            raise ValueError('Wanted API pagination not verified; max_pages=1 until approved docs mapped')

    async def discover(self, fetcher: PoliteFetcher) -> AsyncIterator[JobRef]:
        url = os.getenv('WANTED_APPROVED_LIST_URL', '').strip()
        # The public application page issues client-ID/client-secret, not a
        # documented universally usable Bearer token. Require exact auth header
        # supplied by the provider; never assume those credentials are tokens.
        auth_header = os.getenv('WANTED_AUTH_HEADER', '').strip()
        auth_value = os.getenv('WANTED_AUTH_VALUE', '').strip()
        if not url or not auth_header or not auth_value:
            raise PermissionError('Wanted: obtain licensed endpoint and auth method; set WANTED_APPROVED_LIST_URL, WANTED_AUTH_HEADER, WANTED_AUTH_VALUE')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'openapi.wanted.jobs' or parsed.username or parsed.password:
            raise ValueError('Wanted URL must be an approved https://openapi.wanted.jobs endpoint')
        # Explicit operator-set header only; no JWT exchange / route guessing.
        if auth_header.lower() not in {'authorization', 'x-api-key', 'x-client-id'}:
            raise ValueError('Review the issuer documentation for authentication header')
        document = await fetcher.get_json(url, headers={auth_header: auth_value})
        for row in wanted_rows(document):
            draft = parse_wanted_job(row)
            yield JobRef(draft.external_id, draft.original_url, payload=row)

    async def parse(self, fetcher: PoliteFetcher, ref: JobRef) -> JobDraft:
        if ref.payload is None:
            raise ValueError('Missing Wanted API payload')
        return parse_wanted_job(ref.payload)
