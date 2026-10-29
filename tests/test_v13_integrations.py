"""Contract tests: synthetic fixtures based on published API fields, not live integration claims."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.crawlers.base import JobRef
from app.crawlers.official_apis import (SaraminApiCrawler, WantedApiCrawler,
    parse_saramin_job, parse_wanted_job, saramin_jobs, wanted_rows)
from app.crawlers.real_sites import (listing_refs, detail_draft)
from app.database import Base, Job, ListingPage
from app.incremental import ListingCache
from app.duplicates import similar_job_candidates
from app.fetcher import PoliteFetcher


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "v13.db"}')
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def saramin_doc():
    return {'job-search': {'jobs': {'count': 1, 'start': 0, 'total': 1, 'job': [{
        'id': 36314892, 'active': 1, 'posting-timestamp': 1791748800,
        'modification-timestamp': 1791748899, 'expiration-timestamp': 1793386800,
        'company': {'name': {'#text': '가상주식회사'}},
        'position': {'title': {'#text': '주니어 백엔드 개발자'},
                     'location': {'#text': '서울'}, 'job-type': {'#text': '정규직'},
                     'experience-level': {'code': '1'}},
    }]}}}


def test_saramin_official_payload_mapping():
    rows = saramin_jobs(saramin_doc())
    draft = parse_saramin_job(rows[0])
    assert draft.title == '주니어 백엔드 개발자'
    assert draft.organization == '가상주식회사'
    assert draft.region == '서울'
    assert draft.employment_type == '정규직'
    assert draft.source_status == 'open'
    assert draft.deadline_on is not None
    assert draft.body_text is None  # API summary is not full detail HTML.


def test_saramin_api_requires_key_and_preserves_revision(monkeypatch):
    crawler = SaraminApiCrawler(max_pages=1)
    class MockFetcher:
        async def get_json(self, url, *, params=None):
            assert params['sort'] == 'ud' and params['job_mid_cd'] == '22'
            assert params['access-key'] == 'test-only'
            return saramin_doc()
    async def collect():
        return [x async for x in crawler.discover(MockFetcher())]
    monkeypatch.delenv('SARAMIN_ACCESS_KEY', raising=False)
    with pytest.raises(PermissionError):
        asyncio.run(collect())
    monkeypatch.setenv('SARAMIN_ACCESS_KEY', 'test-only')
    refs = asyncio.run(collect())
    assert refs[0].listing_fingerprint == '1791748899'
    assert asyncio.run(crawler.parse(None, refs[0])).title == '주니어 백엔드 개발자'


def test_wanted_adapter_never_guesses_endpoints(monkeypatch):
    c = WantedApiCrawler()
    class FakeFetcher:
        async def get_json(self, url, *, headers=None):
            assert url == 'https://openapi.wanted.jobs/partner-approved-endpoint'
            assert headers == {'Authorization': 'Bearer fake-key'}
            return {'jobs': [{'id': 9901, 'title': 'Junior Software Engineer',
                              'company': {'name': '가상기술'}, 'location': '서울'}]}
    async def collect():
        return [x async for x in c.discover(FakeFetcher())]
    monkeypatch.delenv('WANTED_API_KEY', raising=False)
    monkeypatch.delenv('WANTED_APPROVED_LIST_URL', raising=False)
    with pytest.raises(PermissionError):
        asyncio.run(collect())
    monkeypatch.setenv('WANTED_AUTH_VALUE', 'Bearer fake-key')
    monkeypatch.setenv('WANTED_AUTH_HEADER', 'Authorization')
    monkeypatch.setenv('WANTED_APPROVED_LIST_URL', 'https://openapi.wanted.jobs/partner-approved-endpoint')
    refs = asyncio.run(collect())
    assert asyncio.run(c.parse(None, refs[0])).organization == '가상기술'
    monkeypatch.setenv('WANTED_APPROVED_LIST_URL', 'https://attacker.example/jobs')
    with pytest.raises(ValueError):
        asyncio.run(collect())
    with pytest.raises(ValueError):
        wanted_rows({'unexpected': []})


def test_html_adapters_are_stable_and_fail_closed():
    incruit = '<a href="/jobs/2609100000943?utm_source=site">Backend Recruit</a>'
    assert listing_refs('incruit', incruit, 'https://lab.incruit.com/navi/jobs')[0].external_id == '2609100000943'
    assert listing_refs('incruit', '<div id="root"></div>', 'https://lab.incruit.com/navi/jobs') == []
    assert listing_refs('zighang', '<div id="app"></div>', 'https://zighang.com/recruitment') == []
    z = listing_refs('zighang', '<a href="/recruitment/some-job-88291">채용</a>', 'https://zighang.com/recruitment')
    assert z[0].external_id == 'some-job-88291'
    with pytest.raises(ValueError):
        detail_draft('incruit', JobRef('2609100000943', 'https://lab.incruit.com/jobs/2609100000943'), '<div id="root"></div>')


def test_conditional_listing_fetch_and_full_audit(factory, monkeypatch):
    monkeypatch.setenv('LISTING_FULL_SCAN_HOURS', '72')
    cache = ListingCache(factory)
    url = 'https://inthiswork.com/it'
    assert cache.headers_for('inthiswork', url) == {}
    assert cache.update('inthiswork', url, status=200, text='test-list', etag='"test-etag"')
    assert cache.headers_for('inthiswork', url) == {'If-None-Match': '"test-etag"'}
    assert cache.update('inthiswork', url, status=304) is False
    with factory() as db:
        row = db.scalar(select(ListingPage))
        row.checked_at = datetime.now(timezone.utc) - timedelta(hours=35)
        db.commit()
    assert cache.update('inthiswork', url, status=200, text='test-list') is False
    with factory() as db:
        row = db.scalar(select(ListingPage))
        assert row.checked_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) - timedelta(hours=34)
    assert cache.update('inthiswork', url, status=200, text='modified-list') is True
    with factory() as db:
        row = db.scalar(select(ListingPage))
        row.checked_at = datetime.now(timezone.utc) - timedelta(hours=73)
        db.commit()
    assert cache.headers_for('inthiswork', url) == {}
    assert cache.update('inthiswork', url, status=200, text='modified-list') is True


def job(jid, source, title, org='(주)페이크컴퍼니', deadline=date(2026,10,30), region='서울'):
    return SimpleNamespace(id=jid, source_id=source, title=title, organization=org,
        deadline_on=deadline, region=region, original_url=f'https://example.org/jobs/{jid}')


def test_cross_source_similarity_avoids_unsafe_merge():
    jobs = [
        job(1, 'inthiswork', '신입 백엔드 개발자 채용'),
        job(2, 'wanted', '신입 백엔드 개발자 채용'),
        job(3, 'saramin', '시니어 프론트엔드 개발자 채용'),
        job(4, 'jobkorea', '신입 백엔드 개발자 채용', deadline=date(2026,11,10)),
        job(5, 'incruit', '신입 백엔드 개발자 채용', org='다른기업'),
    ]
    candidates = similar_job_candidates(jobs)
    assert len(candidates) == 1
    assert {x['id'] for x in candidates[0]['jobs']} == {1, 2}
    assert 'deadline_equal' in candidates[0]['reason']


def test_fetcher_robots_conditional_and_304(monkeypatch):
    events = []
    def handler(request):
        events.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /private\n')
        if request.url.path == '/feed':
            return httpx.Response(304, headers={'ETag':'abc'})
        return httpx.Response(403, text='blocked')
    async def go():
        async with PoliteFetcher(frozenset({'example.org'}), delay=0) as f:
            await f.client.aclose()
            f.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            r = await f.get_response('https://example.org/feed', headers={'If-None-Match':'abc'})
            assert r.status == 304 and r.text is None
            with pytest.raises(PermissionError):
                await f.get_text('https://example.org/private')
            with pytest.raises(RuntimeError):
                await f.get_text('https://example.org/blocked')
    asyncio.run(go())
    assert [e.url.path for e in events] == ['/robots.txt','/feed','/blocked']


def test_saramin_page_index_not_offset(monkeypatch):
    monkeypatch.setenv('SARAMIN_ACCESS_KEY', 'synthetic')
    calls = []
    class Stub:
        async def get_json(self, url, *, params):
            calls.append(params['start'])
            row = saramin_doc()['job-search']['jobs']['job'][0]
            if params['start'] <= 1:
                # Need 100 rows to move to following page. Distinct IDs.
                return {'job-search': {'jobs': {'job': [{**row, 'id': 90000000 + params['start']*100 + i}
                    for i in range(100)]}}}
            return {'job-search': {'jobs': {'job': []}}}
    async def collect():
        return [r async for r in SaraminApiCrawler(max_pages=3).discover(Stub())]
    refs = asyncio.run(collect())
    assert len(refs) == 200
    assert calls == [0, 1, 2]


def test_incruit_optional_js_render_with_robots_precheck(monkeypatch, factory):
    """Rendering is opt-in; static 200 permission preflight is mandatory."""
    from app.crawlers.real_sites import IncruitCrawler
    from app.fetcher import FetchResult
    monkeypatch.setenv('RENDER_JS', '1')
    class Stub:
        calls = []
        async def get_response(self, url, headers=None):
            self.calls.append('preflight')
            return FetchResult(status=200, text='<div id="root"></div>')
        async def render_html(self, url):
            self.calls.append('render')
            return '<a href="/jobs/2609100000943">백엔드 개발자 채용</a>'
    async def collect():
        c = IncruitCrawler(max_pages=1)
        c.listing_cache = ListingCache(factory)
        f = Stub()
        refs = [r async for r in c.discover(f)]
        return refs, f.calls
    refs, calls = asyncio.run(collect())
    assert refs[0].external_id == '2609100000943'
    assert calls == ['preflight','render']


def test_incruit_opt_in_rendered_detail_is_saved(monkeypatch):
    from app.crawlers.real_sites import IncruitCrawler
    from app.fetcher import FetchResult
    monkeypatch.setenv('RENDER_JS', '1')
    class Stub:
        captured = {}
        calls = []
        async def get_response(self, url, headers=None):
            self.calls.append('preflight')
            return FetchResult(200, '<div id="root"></div>')
        async def render_html(self, url):
            self.calls.append('render')
            return '''<main><h1>Software Engineer 신입 채용</h1><article>
                모집부문: 서버 개발자. 자격요건: Python, PostgreSQL, FastAPI 및
                백엔드 서비스 운영 경험. 지원자격: 신입. 담당업무: 서버 API 개발
                </article></main>'''
        def capture_rendered_html(self, url, html):
            self.captured[url] = html
    f = Stub()
    ref = JobRef('2609100000943','https://lab.incruit.com/jobs/2609100000943')
    draft = asyncio.run(IncruitCrawler(max_pages=1).parse(f, ref))
    assert 'Python' in draft.body_text
    assert ref.url in f.captured
    assert f.calls == ['preflight', 'render']
