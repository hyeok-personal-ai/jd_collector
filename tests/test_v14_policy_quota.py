"""v1.4: policy fails closed, published Saramin JSON mapping, atomic API quota."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.access_policy import access_status, require_access
from app.quotas import ApiQuota
from app.crawlers.base import JobRef
from app.crawlers.official_apis import parse_saramin_job, SaraminApiCrawler
from app.fetcher import PoliteFetcher
from app.incremental import ListingCache
from app.repository import upsert_job
from app.runner import mark_detail_checked
from app.schema import JobDraft


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "quota.db"}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_all_live_sources_fail_closed_by_default(monkeypatch):
    for key in ['SARAMIN_ACCESS_KEY', 'WANTED_AUTH_VALUE', 'WANTED_AUTH_HEADER',
                'WANTED_APPROVED_LIST_URL', 'ALLOW_HTML_JOBKOREA',
                'ALLOW_HTML_INCRUIT', 'ALLOW_HTML_INTHISWORK', 'ALLOW_HTML_ZIGHANG']:
        monkeypatch.delenv(key, raising=False)
    assert access_status('demo')[0]
    for name in ['jobkorea', 'saramin', 'wanted', 'incruit']:
        assert not access_status(name)[0]
        with pytest.raises(PermissionError):
            require_access(name)
    assert access_status('inthiswork')[0] and access_status('zighang')[0]
    monkeypatch.setenv('ALLOW_HTML_INTHISWORK','1')
    assert access_status('inthiswork')[0]
    assert not access_status('jobkorea')[0]


def test_wanted_must_not_assume_client_secret_is_bearer(monkeypatch):
    from app.crawlers.official_apis import WantedApiCrawler
    monkeypatch.setenv('WANTED_API_KEY','client-secret-is-not-access-token')
    monkeypatch.setenv('WANTED_APPROVED_LIST_URL','https://openapi.wanted.jobs/approved')
    monkeypatch.delenv('WANTED_AUTH_HEADER', raising=False)
    monkeypatch.delenv('WANTED_AUTH_VALUE', raising=False)
    assert not access_status('wanted')[0]
    with pytest.raises(PermissionError):
        async def check():
            return [r async for r in WantedApiCrawler().discover(None)]
        asyncio.run(check())


def test_saramin_official_json_company_detail_and_close_type():
    # Shape copied from fields in the official published sample (fictional values).
    sample = {'id':'27614114', 'active':1,
              'company':{'detail':{'name':'샘플 기업'}},
              'position':{'title':'신입 개발자','location':{'code':'101', 'name':'서울 > 관악구'},
                          'job-type':{'name':'계약직'},'experience-level':{'code':1}},
              'close-type':{'code':'2','name':'채용시'},
              'posting-timestamp':'1791748800', 'expiration-timestamp':'1793386800'}
    draft = parse_saramin_job(sample)
    assert draft.organization == '샘플 기업'
    assert draft.deadline_on is None and draft.deadline_text == '채용시'
    assert draft.region == '서울 > 관악구'


def test_persisted_quota_and_daily_reset(factory):
    quota = ApiQuota(factory)
    now = datetime(2026,10,12,22,0,tzinfo=ZoneInfo('Asia/Seoul'))
    assert quota.reserve('saramin', limit=2, now=now) == 1
    assert ApiQuota(factory).reserve('saramin', limit=2, now=now) == 2
    with pytest.raises(PermissionError):
        quota.reserve('saramin', limit=2, now=now)
    tomorrow = datetime(2026,10,13,0,1,tzinfo=ZoneInfo('Asia/Seoul'))
    assert quota.reserve('saramin', limit=2, now=tomorrow) == 1


def test_quota_counts_http_retries(factory):
    events = []
    def handler(request):
        events.append(request.url.path)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        if events.count('/data') <= 1:
            return httpx.Response(503)
        return httpx.Response(200, json={'items':[]})
    async def go():
        async with PoliteFetcher(frozenset({'example.org'}), delay=0) as f:
            await f.client.aclose()
            f.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            res = await f.get_json('https://example.org/data', before_request=lambda: ApiQuota(factory).reserve('saramin', limit=2))
            assert res == {'items':[]}
            with pytest.raises(PermissionError):
                await f.get_json('https://example.org/data', before_request=lambda: ApiQuota(factory).reserve('saramin', limit=2))
    asyncio.run(go())
    assert events == ['/robots.txt','/data','/data']


def test_known_id_with_new_fingerprint_does_not_stop_pagination(factory):
    first = JobRef('123','https://example.org/jobs/123','revision-1')
    with factory() as db:
        upsert_job(db, JobDraft(source_id='saramin',external_id='123',title='초보 개발자', original_url=first.url))
        mark_detail_checked(db, 'saramin', first, now=datetime(2026,10,12,tzinfo=ZoneInfo('Asia/Seoul')))
        db.commit()
    cache = ListingCache(factory)
    assert cache.count_known_unchanged('saramin',[first]) == 1
    assert cache.count_known_unchanged('saramin',[JobRef('123',first.url,'revision-2')]) == 0
    assert cache.count_known_unchanged('saramin',[JobRef('123',first.url,None)]) == 0
