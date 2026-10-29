import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.control import ControlStore
from app.database import Base, Job, JobClassification, RawPosting, get_session
from app.normalization import clean_draft
from app.job_labels import label_job
from app.schema import JobDraft
from app.repository import upsert_job, save_raw_posting
from app.crawlers.company_public import CompanyPublicCrawler, visible_links, parse_detail
from app.scheduler import PollPlanner, active_boards


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv('RADAR_DATA_DIR', str(tmp_path / 'control'))
    monkeypatch.delenv('RADAR_WORKER', raising=False)
    monkeypatch.delenv('PUBLIC_BOARD_CATALOG', raising=False)
    monkeypatch.setenv('NORMALIZATION_LLM_MODEL', '')


def draft(**fields):
    return JobDraft(source_id='demo', external_id='23', title='백엔드 개발자',
                    original_url='https://example.org/jobs/23', **fields)


def sessions():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.parametrize('separator', ['\n', ' '])
def test_long_body_experience_survives_normalization(separator):
    raw = draft(body_text=('회사 서비스 소개입니다. ' * 70) + separator + '지원 자격: 경력 5년 이상')
    assert label_job(clean_draft(raw)).career == 'experienced'


@pytest.mark.parametrize('status', ['partial', 'failed'])
def test_failed_cli_is_nonzero(monkeypatch, status):
    import app.cli as cli
    async def fake(*args, **kwargs):
        return SimpleNamespace(source_id='demo', status=status, seen_count=0, created_count=0,
                               updated_count=0, failed_count=1, error_summary='broken')
    monkeypatch.setattr(cli, 'run_crawler', fake)
    monkeypatch.setattr('sys.argv', ['cli', 'crawl', 'demo'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


def test_queue_deduplicates_and_recovers(tmp_path):
    store = ControlStore(tmp_path/'queue.sqlite')
    key = store.targets()[0]['key']
    assert store.enqueue([key]) == 1
    assert store.enqueue([key]) == 0
    first = store.claim()
    assert store.claim() is None
    store.recover()
    retried = store.claim()
    assert retried['id'] == first['id']
    assert retried['status']=='running' and retried['attempts']==2
    assert retried['finished_at'] is None and retried['error'] is None
    store.finish(retried, status='completed', seen=4, created=4)
    target = next(t for t in store.targets() if t['key'] == key)
    assert target['status'] == 'completed' and target['seen'] == 4
    assert target['next_due'] > target['checked_at']
    assert store.enqueue([key], due_only=True) == 0


def test_stop_cancels_waiting_not_running(tmp_path):
    store = ControlStore(tmp_path/'queue.sqlite')
    keys = [t['key'] for t in store.targets()[:2]]
    store.enqueue(keys)
    task = store.claim()
    assert store.cancel_pending() == 1
    assert store.snapshot()['counts']['running'] == 1
    store.finish(task, status='blocked', error='robots denied')
    assert store.snapshot()['counts']['blocked'] == 1


def test_robots_document_failure_has_cooldown_without_disabling_policy(tmp_path):
    store=ControlStore(tmp_path/'control.sqlite')
    store.save_settings({'interval_hours':6})
    key='inthiswork:all'
    store.enqueue([key]);task=store.claim()
    store.finish(task,status='blocked',error='PermissionError: robots.txt check returned 403 for https://inthiswork.com')
    target=next(t for t in store.targets() if t['key']==key)
    assert target['next_due']-target['checked_at']==1800
    assert store.enqueue([key],due_only=True)==0


def test_browser_challenge_requires_operator_allowance_and_longer_cooldown(tmp_path):
    store=ControlStore(tmp_path/'control.sqlite');store.save_settings({'interval_hours':6})
    store.enqueue(['inthiswork:all']);task=store.claim()
    store.finish(task,status='blocked',error='BrowserChallengeError: robots.txt HTTP 403: browser security challenge; site operator bot allowance required')
    target=next(t for t in store.targets() if t['key']=='inthiswork:all')
    assert target['next_due']-target['checked_at']==6*3600


def test_renew_lease_rejects_stale_owner(tmp_path):
    manager = PollPlanner(tmp_path/'poll.sqlite')
    board = active_boards()[0]
    old = manager.acquire(board, now=0, lease_seconds=10)
    assert manager.renew(board, token=old, now=9, lease_seconds=10)
    assert manager.acquire(board, now=11) is None
    new = manager.acquire(board, now=20)
    assert not manager.renew(board, token=old, now=21)
    assert manager.renew(board, token=new, now=21)


def test_renormalize_updates_labels_and_keeps_closed(monkeypatch):
    import app.renormalize as module
    engine, factory = sessions()
    raw = draft(source_status='open', body_text='지원 자격\n경력 5년 이상')
    with factory() as db:
        save_raw_posting(db, raw)
        upsert_job(db, raw.model_copy(update={'source_status': 'closed'}))
        db.commit()
    monkeypatch.setattr(module, 'SessionLocal', factory)
    monkeypatch.setattr(module, 'init_db', lambda: None)
    asyncio.run(module.run(model=''))
    with factory() as db:
        assert db.scalar(select(Job)).source_status == 'closed'
        assert db.scalar(select(JobClassification)).career == 'experienced'
    engine.dispose()


def test_company_links_reject_foreign_apply_and_script():
    html = '''<a href="/jobs/123">백엔드 개발자</a><a href="https://evil.test/jobs/1">개발자</a>
    <a href="javascript:alert(1)">개발자</a><a href="/jobs/123/apply">지원하기</a>'''
    links = visible_links(html, 'https://careers.example.org/')
    assert list(links) == ['https://careers.example.org/jobs/123']


def test_generic_detail_does_not_accept_company_brochure():
    company = {'name': 'Example', 'url': 'https://careers.example.org'}
    with pytest.raises(ValueError):
        parse_detail('<main><h1>회사 소개</h1>'+('좋은 회사입니다. '*50)+'</main>', company['url'], company)


def test_roundhr_public_detail_pattern():
    company = {'name': 'Example', 'url': 'https://sample.recruit.roundhr.com'}
    html = '<main><h1>백엔드 개발자</h1><p>담당 업무</p><p>'+('API 개발 및 서비스 운영. '*10)+'</p><p>지원 자격: 경력 3년 이상</p></main>'
    result = parse_detail(html, company['url']+'/c/abc123', company)
    assert result.organization == 'Example'
    assert result.source_status == 'unknown'


@pytest.fixture
def client(monkeypatch):
    import app.api as api
    engine, factory = sessions()
    def session():
        with factory() as db:
            yield db
    monkeypatch.setattr(api, 'init_db', lambda: None)
    api.app.dependency_overrides[get_session] = session
    with factory() as db:
        upsert_job(db, draft(organization='Example', body_text='공개 채용 공고', source_status='open'))
        db.commit()
    with TestClient(api.app) as value:
        yield value
    api.app.dependency_overrides.clear()
    engine.dispose()


HEADERS = {'X-Radar-Request': '1'}


def test_write_api_rejects_cross_origin(client):
    assert client.post('/api/collect', json={}).status_code == 403
    assert client.post('/api/collect', json={}, headers={**HEADERS, 'Origin': 'https://other.test'}).status_code == 403


def test_settings_validation_and_queue_api(client):
    assert client.patch('/api/settings', json={'max_items': 0}, headers=HEADERS).status_code == 422
    assert client.patch('/api/settings', json={'unknown': True}, headers=HEADERS).status_code == 422
    assert client.patch('/api/settings', json={'auto_collect': False}, headers=HEADERS).json()['auto_collect'] is False
    key = client.get('/api/targets').json()['items'][0]['key']
    assert client.post('/api/collect', json={'target_keys':[key]}, headers=HEADERS).json()['queued'] == 1
    assert client.post('/api/collect', json={'target_keys':[key]}, headers=HEADERS).json()['queued'] == 0
    assert client.post('/api/collect', json={'target_keys':['unknown']}, headers=HEADERS).status_code == 422


def test_focus_api_rejects_external_hosts_and_preserves_explicit_scope(client):
    assert client.post('/api/zighang-focus', json={'urls':['https://evil.example']},headers=HEADERS).status_code==422
    result=client.post('/api/zighang-focus',json={'mode':'development','urls':[
        'https://zighang.com/recruitment/885ec013-4342-4a18-81f7-dcb9ec6d6c85']},headers=HEADERS)
    assert result.status_code==200
    assert result.json()['exhaustive'] is False
    assert result.json()['inventory']=={'pending':1}
    assert client.get('/api/aggregate-status').json()['items'][1]['collection_mode']=='development'


def test_bookmark_search_and_csv(client):
    job = client.get('/api/jobs').json()['items'][0]
    assert client.put('/api/bookmarks/'+str(job['id']), json={'enabled':True}, headers=HEADERS).status_code == 200
    saved = client.get('/api/jobs?bookmarked_only=true').json()
    assert saved['total'] == 1 and saved['items'][0]['bookmarked']
    assert '백엔드 개발자' in client.get('/api/export.csv?bookmarked_only=true').text
    client.put('/api/bookmarks/'+str(job['id']), json={'enabled':False}, headers=HEADERS)
    assert client.get('/api/jobs?bookmarked_only=true').json()['total'] == 0


@pytest.mark.parametrize('query',['source_id=not-a-source','keyword=DOES_NOT_MATCH','it_only=true'])
def test_csv_uses_the_same_search_filters_as_the_list(client,query):
    import csv,io
    assert client.get('/api/jobs?'+query).json()['total']==0
    rows=list(csv.reader(io.StringIO(client.get('/api/export.csv?'+query).text.lstrip('\ufeff'))))
    assert len(rows)==1


def test_backup_restore_preserves_database(monkeypatch, tmp_path):
    import app.maintenance as maintenance
    jobdb = tmp_path/'jobs.sqlite'
    with sqlite3.connect(jobdb) as db:
        db.execute('CREATE TABLE example(value TEXT)')
        db.execute("INSERT INTO example VALUES ('before')")
    monkeypatch.setattr(maintenance, 'DATABASE_URL', 'sqlite:///'+str(jobdb))
    backup = maintenance.create_backup()
    with sqlite3.connect(jobdb) as db:
        db.execute("UPDATE example SET value='after'")
    result = maintenance.restore_backup(backup['path'])
    assert result['restored']
    with sqlite3.connect(jobdb) as db:
        assert db.execute('SELECT value FROM example').fetchone()[0] == 'before'


def test_worker_serialises_and_stores_result(monkeypatch, tmp_path):
    import app.service as service
    store = ControlStore(tmp_path/'queue.sqlite')
    store.save_settings({'auto_collect':False})
    store.enqueue([store.targets()[0]['key']])
    async def fake(task, settings):
        return SimpleNamespace(status='completed', seen_count=2, created_count=2, updated_count=0, error_summary='', id=7)
    async def recovery():
        pass
    monkeypatch.setattr(service, 'recover_runs', recovery)
    monkeypatch.setattr(service, 'expire_local_deadlines', lambda: 0)
    monkeypatch.setattr(service, 'collect_task', fake)
    async def exercise():
        worker = asyncio.create_task(service.serve_queue(store))
        for _ in range(100):
            if store.snapshot()['counts'].get('completed'):
                break
            await asyncio.sleep(.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(exercise())
    assert store.snapshot()['counts']['completed'] == 1
    assert store.snapshot()['recent'][0]['run_id'] == 7


def test_catalog_groups_shared_portals_once(tmp_path):
    store = ControlStore(tmp_path/'queue.sqlite')
    rows = store.targets()
    assert sum(len(t['spec']['company_ids']) for t in rows) == 145
    urls = [t['spec']['url'] for t in rows if t['spec']['kind'] == 'company']
    assert len(urls) == len(set(urls))


def test_bounded_collection_resumes_after_cached_items():
    from app.runner import run_crawler
    from app.crawlers.demo import DemoCrawler
    engine, factory = sessions()
    async def collect():
        first = await run_crawler(DemoCrawler(), max_items=1, session_factory=factory, init_database=False)
        second = await run_crawler(DemoCrawler(), max_items=1, session_factory=factory, init_database=False)
        third = await run_crawler(DemoCrawler(), max_items=1, session_factory=factory, init_database=False)
        assert first.status == second.status == 'limited'
        assert third.status == 'completed'
        assert first.created_count == second.created_count == third.created_count == 1
    asyncio.run(collect())
    engine.dispose()


def test_redirect_checks_robots_before_following():
    import httpx
    from app.fetcher import PoliteFetcher
    visited = []
    def transport(request):
        visited.append(request.url.path)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /private')
        return httpx.Response(302, headers={'Location':'/private'})
    async def probe():
        async with PoliteFetcher(frozenset({'example.org'}), delay=0, transport=httpx.MockTransport(transport)) as fetcher:
            with pytest.raises(PermissionError):
                await fetcher.get_text('https://example.org/start')
    asyncio.run(probe())
    assert '/private' not in visited


def test_cross_origin_redirect_is_not_requested():
    import httpx
    from app.fetcher import PoliteFetcher
    visited = []
    def transport(request):
        visited.append(request.url.host)
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        return httpx.Response(302, headers={'Location':'https://evil.test/private'})
    async def probe():
        async with PoliteFetcher(frozenset({'example.org'}), delay=0, transport=httpx.MockTransport(transport)) as fetcher:
            with pytest.raises(ValueError):
                await fetcher.get_text('https://example.org/start')
    asyncio.run(probe())
    assert set(visited) == {'example.org'}


def test_internal_is_not_intern_and_english_years_are_recognized():
    raw = draft(body_text='Minimum qualifications: 5+ years of software engineering experience')
    assert label_job(raw).career == 'experienced'
    internal = raw.model_copy(update={'title':'Internal Tools Developer', 'body_text':None})
    assert label_job(internal).career == 'unknown'


def test_detail_failure_at_budget_is_not_auto_continued():
    from app.runner import run_crawler
    from app.crawlers.demo import DemoCrawler
    class Broken(DemoCrawler):
        async def parse(self, fetcher, ref):
            raise ValueError('broken detail')
    engine, factory = sessions()
    result = asyncio.run(run_crawler(Broken(), max_items=1, session_factory=factory, init_database=False))
    assert result.status == 'partial'
    assert result.failed_count == 1
    engine.dispose()


def test_structured_and_visible_vacancy_share_one_identity():
    from app.crawlers.company_public import CompanyPublicCrawler
    company = {'id':'example','name':'Example','url':'https://example.org/'}
    crawler = CompanyPublicCrawler(company)
    html = '<script type="application/ld+json">' + json.dumps({
        '@type':'JobPosting','title':'Backend Engineer','url':'https://example.org/jobs/123',
        'identifier':{'value':'123'},'description':'Requirements: Python experience'}) + '</script><a href="/jobs/123">Backend Engineer</a>'
    class Fetcher:
        async def get_text(self, url):
            return html
    async def refs():
        return [ref async for ref in crawler.discover(Fetcher())]
    result = asyncio.run(refs())
    assert len(result) == 1
    assert result[0].external_id == crawler.stable('https://example.org/jobs/123')


def test_repeated_detail_failures_stop_the_batch():
    from app.runner import run_crawler
    from app.crawlers.demo import DemoCrawler
    from app.crawlers.base import JobRef
    class Broken(DemoCrawler):
        async def discover(self, fetcher):
            for i in range(20):
                yield JobRef(str(i), 'https://example.org/jobs/'+str(i))
        async def parse(self, fetcher, ref):
            raise ValueError('layout changed')
    engine, factory = sessions()
    result = asyncio.run(run_crawler(Broken(), max_items=100, session_factory=factory, init_database=False))
    assert result.status == 'partial'
    assert result.failed_count == result.seen_count == 5
    engine.dispose()


def test_company_identity_ignores_tracking_and_unicode_encoding():
    from app.crawlers.company_public import CompanyPublicCrawler
    crawler = CompanyPublicCrawler({'id':'test','name':'Test','url':'https://example.org/'})
    first='https://example.org/jobs/123/개발/'
    second='https://example.org/jobs/123/%EA%B0%9C%EB%B0%9C/?gh_jid=123&utm_source=career'
    assert crawler.stable(first) == crawler.stable(second)
    assert crawler.stable('https://example.org/view?job_id=1') != crawler.stable('https://example.org/view?job_id=2')
