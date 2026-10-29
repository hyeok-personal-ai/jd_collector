import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.board_catalog import load_boards, default_ids
from app.crawlers.company_pages import configured_pages, parse_jsonld, CompanyJsonLdCrawler
from app.crawlers.public_feeds import AshbyPublicCrawler
from app.database import Base, Job, JobClassification, PostingPresence
from app.repository import upsert_job
from app.classification_store import update_classification
from app.lifecycle import mark_present, reconcile_scope
from app.job_labels import label_job
from app.schema import JobDraft
from app.evaluate import evaluate
from app.normalization import MissingFields, normalize_draft


def sessions(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "test.sqlite"}')
    Base.metadata.create_all(engine)
    return engine,sessionmaker(bind=engine,expire_on_commit=False)


def test_catalog_expanded_and_valid():
    boards = load_boards()
    assert len(boards) >= 14
    assert 'friendliai' in default_ids('ashby')
    assert all(item['evidence'].startswith('https://') for item in boards)
    assert len({(a['provider'],a['id']) for a in boards}) == len(boards)


def test_company_jsonld_parses_raw_html():
    html = Path('fixtures/company/sample_jobposting.html').read_text(encoding='utf-8')
    drafts = parse_jsonld(html,'https://careers.example.org/jobs/001','Sample Tech')
    assert len(drafts) == 1
    d = drafts[0]
    assert d.title == '주니어 백엔드 개발자 모집'
    assert d.organization == '샘플테크'
    assert d.deadline_on.isoformat() == '2026-10-31'
    assert d.region == '서울, KR'
    assert '경력 1~3년' in d.body_text
    assert label_job(d).career in ('early','entry')


def test_company_jsonld_injection_and_bad_urls_fall_back():
    html='<script type="application/ld+json">'+json.dumps({
        '@type':'JobPosting','title':'Software Engineer','url':'https://evil.test/steal',
        'description':'<b>Python Engineer</b>','identifier':{'value':'123'}})+'</script>'
    rows=parse_jsonld(html,'https://careers.example.org/jobs/123','Sample Tech')
    assert len(rows)==1 and rows[0].original_url=='https://careers.example.org/jobs/123'
    assert rows[0].body_text=='Python Engineer'


def test_company_page_permission_is_opt_in(tmp_path, monkeypatch):
    path=tmp_path/'company.json'
    path.write_text(json.dumps({'pages':[{'url':'https://careers.example.org/jobs/1','company':'Example'}]}))
    with pytest.raises(PermissionError):
        configured_pages(str(path))
    path.write_text(json.dumps({'pages':[{'url':'https://careers.example.org/jobs/1','company':'Example','authorized':True}]}))
    monkeypatch.setenv('COMPANY_JOB_PAGES_FILE',str(path))
    crawler=CompanyJsonLdCrawler()
    assert crawler.allowed_hosts==frozenset({'careers.example.org'})


def test_classification_db_persist_and_stays_separate(tmp_path):
    engine,factory=sessions(tmp_path)
    draft=JobDraft(source_id='test',external_id='1',title='IT 신입 백엔드 개발자',
                   original_url='https://example.org/jobs/1')
    with factory() as db:
        upsert_job(db,draft)
        update_classification(db,draft)
        db.commit()
        row=db.scalar(select(JobClassification))
        assert row.it_match=='yes' and row.career=='entry'
    engine.dispose()


def test_lifecycle_two_scans_then_reopen(tmp_path):
    engine,factory=sessions(tmp_path)
    job=JobDraft(source_id='public_ashby',external_id='acme:abc',title='Software Engineer',
                 original_url='https://jobs.ashbyhq.com/acme/abc',source_status='open')
    now=datetime(2026,10,12,tzinfo=timezone.utc)
    with factory() as db:
        upsert_job(db,job)
        mark_present(db,'public_ashby','acme:abc','acme')
        state=db.scalar(select(PostingPresence))
        state.last_present_at=now-timedelta(hours=50)
        db.commit()
        assert reconcile_scope(db,'public_ashby','acme',{'acme:other'},now=now)==0
        assert db.scalar(select(Job)).source_status=='open'
        assert reconcile_scope(db,'public_ashby','acme',{'acme:other'},now=now+timedelta(hours=1))==1
        assert db.scalar(select(Job)).source_status=='closed'
        mark_present(db,'public_ashby','acme:abc','acme')
        assert db.scalar(select(Job)).source_status=='open'
        assert db.scalar(select(PostingPresence)).missing_scans==0
    engine.dispose()


def test_public_per_board_failure_does_not_abort_next_board(monkeypatch):
    monkeypatch.setenv('PUBLIC_ASHBY_BOARDS','badboard,goodboard')
    good={'jobs':[{'id':'1','title':'Software Intern','jobUrl':'https://jobs.ashbyhq.com/goodboard/1',
                   'isListed':True,'descriptionHtml':'<p>Python</p>'}]}
    class Fake:
        async def get_json(self, url, **kwargs):
            if url.endswith('/badboard'):
                raise RuntimeError('network down')
            return good
    async def gather():
        c=AshbyPublicCrawler()
        refs=[ref async for ref in c.discover(Fake())]
        return refs,c
    refs,c=asyncio.run(gather())
    assert len(refs)==1
    assert set(c.completed_scopes)=={'goodboard'}
    assert len(c.scope_errors)==1


def test_offline_evaluation_gold_fixture():
    metrics=asyncio.run(evaluate())
    assert metrics['cases']==12 and metrics['fields_checked']>=30
    assert metrics['fields_correct']==metrics['fields_checked']
    assert metrics['mode']=='rules'


def test_unsupported_llm_values_rejected(tmp_path):
    engine,factory=sessions(tmp_path)
    raw=JobDraft(source_id='demo',external_id='a',title='백엔드 개발자 모집',
        original_url='https://example.org/jobs/a', body_text='서울 소재 개발자를 모집합니다.\n정규직 채용입니다.\n지원해주세요.')
    async def fake_extract(raw,model):
        return MissingFields(organization='없는회사',region='부산',deadline_on='2026-12-20',
                             employment_type='정규직',recruitment_type='채용')
    actual=asyncio.run(normalize_draft(raw,factory,llm_model='fake',extractor=fake_extract))
    assert actual.organization is None and actual.region is None and actual.deadline_on is None
    assert actual.employment_type=='정규직' and actual.recruitment_type=='채용'
    engine.dispose()


def test_search_api_entry_it_filters_and_status(tmp_path):
    from fastapi.testclient import TestClient
    from app.api import app
    from app.database import get_session
    engine, factory = sessions(tmp_path)
    with factory() as db:
        for eid,title in [('1','신입 백엔드 개발자'),('2','Senior Backend Engineer'),('3','인사팀 인턴')]:
            d=JobDraft(source_id='test',external_id=eid,title=title,
                       original_url='https://example.org/jobs/'+eid)
            upsert_job(db,d)
            update_classification(db,d)
        db.commit()
    def override():
        with factory() as db:
            yield db
    app.dependency_overrides[get_session]=override
    try:
        with TestClient(app) as client:
            x=client.get('/api/jobs',params={'entry_only':'true','it_only':'true'})
            assert x.status_code==200 and x.json()['total']==1
            assert x.json()['items'][0]['classification']['career']=='entry'
            assert client.get('/api/jobs',params={'entry_only':'true'}).json()['total']==2
            assert client.get('/api/status-summary').json()['tagged']==3
            assert len(client.get('/api/board-catalog').json())>=14
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_runner_persists_tags_and_scoped_errors(tmp_path, monkeypatch):
    import app.runner as runner
    monkeypatch.setenv('PUBLIC_ASHBY_BOARDS','onebad,onegood')
    engine, factory=sessions(tmp_path)
    class FakeFetcher:
        def __init__(self, hosts):
            pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get_json(self,url,**kwargs):
            if url.endswith('onebad'):
                raise RuntimeError('board not reachable')
            return {'jobs':[{'id':'one','title':'신입 Backend Engineer',
                             'jobUrl':'https://jobs.ashbyhq.com/onegood/one',
                             'location':'Seoul','isListed':True}]}
        def pop_captured_html(self,url):
            return None
    monkeypatch.setattr(runner,'PoliteFetcher',FakeFetcher)
    result=asyncio.run(runner.run_crawler(AshbyPublicCrawler(),session_factory=factory))
    assert result.status=='partial' and result.created_count==1 and result.failed_count==1
    with factory() as db:
        assert db.scalar(select(Job)).organization=='onegood'
        assert db.scalar(select(JobClassification)).career=='entry'
        assert db.scalar(select(PostingPresence)).scope_id=='onegood'
    engine.dispose()


def test_public_feed_conditional_304_uses_db_snapshot(tmp_path):
    from app.incremental import ListingCache
    from app.feed_cache import public_json
    from app.fetcher import FetchResult
    from app.database import PublicFeedCache
    engine,factory=sessions(tmp_path)
    class Fake:
        def __init__(self):
            self.requests=[]
        async def get_response(self,url,params=None,headers=None,accept=None):
            self.requests.append(dict(headers or {}))
            if len(self.requests)==1:
                return FetchResult(status=200,text='{"jobs":[{"id":1}]}',etag='"revision-one"')
            return FetchResult(status=304,text=None,etag='"revision-one"')
    async def go():
        fetcher=Fake()
        cache=ListingCache(factory)
        one=await public_json(fetcher,site='public_greenhouse',
                              url='https://boards-api.greenhouse.io/v1/boards/acme/jobs',
                              params={'content':'true'},listing_cache=cache)
        two=await public_json(fetcher,site='public_greenhouse',
                              url='https://boards-api.greenhouse.io/v1/boards/acme/jobs',
                              params={'content':'true'},listing_cache=cache)
        assert one==two=={'jobs':[{'id':1}]}
        assert fetcher.requests==[{}, {'If-None-Match':'"revision-one"'}]
    asyncio.run(go())
    with factory() as db:
        assert db.scalar(select(PublicFeedCache)).etag=='"revision-one"'
    engine.dispose()


def test_invalid_feed_does_not_poison_json_cache(tmp_path):
    from app.incremental import ListingCache
    from app.feed_cache import public_json
    from app.fetcher import FetchResult
    from app.database import PublicFeedCache
    engine,factory=sessions(tmp_path)
    class Fake:
        async def get_response(self,url,params=None,headers=None,accept=None):
            return FetchResult(status=200,text='{"error":"fake login"}',etag='"bad"')
    with pytest.raises(ValueError,match='not caching'):
        asyncio.run(public_json(Fake(),site='public_ashby',
                  url='https://api.ashbyhq.com/posting-api/job-board/acme',
                  listing_cache=ListingCache(factory)))
    with factory() as db:
        assert db.scalar(select(PublicFeedCache)) is None
    engine.dispose()


def test_company_jsonld_full_pipeline_and_html_saved(tmp_path,monkeypatch):
    import app.runner as runner
    from app.fetcher import FetchResult
    from app.database import RawHtml
    url='https://careers.example.org/jobs/001'
    config=tmp_path/'pages.json'
    config.write_text(json.dumps({'pages':[{'url':url,'company':'Sample Tech','authorized':True}]}))
    monkeypatch.setenv('COMPANY_JOB_PAGES_FILE',str(config))
    monkeypatch.setenv('ALLOW_HTML_COMPANY_JSONLD','1')
    engine,factory=sessions(tmp_path)
    class Fake:
        def __init__(self,allowed_hosts):
            assert 'careers.example.org' in allowed_hosts
            self.pages={}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def get_response(self, url,**kwargs):
            assert url=='https://careers.example.org/jobs/001'
            return FetchResult(status=200,text=Path('fixtures/company/sample_jobposting.html').read_text(encoding='utf-8'))
        def capture_rendered_html(self,url,html):
            self.pages[url]=html
        def pop_captured_html(self,url):
            return self.pages.pop(url,None)
    monkeypatch.setattr(runner,'PoliteFetcher',Fake)
    result=asyncio.run(runner.run_crawler(CompanyJsonLdCrawler(),session_factory=factory))
    assert result.status=='completed' and result.created_count==1
    with factory() as db:
        assert db.scalar(select(Job)).title=='주니어 백엔드 개발자 모집'
        assert db.scalar(select(RawHtml)) is not None
        assert db.scalar(select(JobClassification)).it_match=='yes'
    engine.dispose()


def test_board_verifier_requires_explicit_live_flag():
    from app.verify_boards import verify
    results=asyncio.run(verify(provider='ashby',max_boards=2))
    assert len(results)==2
    assert all(x['status']=='NOT_CHECKED' for x in results)
