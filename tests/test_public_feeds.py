"""No network tests for publicly documented, unauthenticated feed adapters."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.access_policy import access_status
from app.crawlers.public_feeds import (
    GreenhousePublicCrawler, LeverPublicCrawler, AshbyPublicCrawler,
    SmartRecruitersPublicCrawler, _boards, parse_greenhouse, parse_lever,
    parse_ashby, parse_smartrecruiters,
)
from app.crawlers.registry import CRAWLERS
from app.database import Base
from app.repository import upsert_job
from app.runner import needs_detail, mark_detail_checked

GH = {'id':8639423002,'title':'AI Agent Software Engineer, Intern',
      'absolute_url':'https://job-boards.greenhouse.io/get-delight/jobs/8639423002',
      'content':'<p>초급 개발자 인턴</p>', 'location':{'name':'Seoul, South Korea'},
      'updated_at':'2026-10-12T07:00:00Z'}
LEVER = {'id':'abc-123','text':'Junior Backend Engineer',
         'hostedUrl':'https://jobs.lever.co/insiderone/abc-123',
         'applyUrl':'https://jobs.lever.co/insiderone/abc-123/apply',
         'categories':{'location':'Seoul','commitment':'Intern'},
         'descriptionPlain':'Python and backend service development',
         'createdAt':1791763200000}
ASHBY = {'title':'AI Engineer Intern', 'jobUrl':'https://jobs.ashbyhq.com/Acme/abcd',
         'applyUrl':'https://jobs.ashbyhq.com/Acme/abcd/application',
         'descriptionPlain':'Building AI agents', 'location':'Seoul',
         'employmentType':'Intern', 'isListed':True,
         'publishedAt':'2026-10-12T01:00:00.000Z'}
SMART = {'id':'12','name':'Software Engineer', 'company':{'name':'ExampleTech'},
         'location':{'city':'Seoul','country':'KR'},'releasedDate':'2026-10-12T09:00:00Z',
         'applyUrl':'https://jobs.smartrecruiters.com/Acme/12/apply',
         'jobAd':{'sections':{'jobDescription':{'text':'<p>백엔드 엔지니어 채용</p>'}}},
         'typeOfEmployment': {'label': 'Full-time'}}

class FakeFetcher:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.mapping[url]


def collect(crawler, fetcher):
    async def go():
        return [x async for x in crawler.discover(fetcher)]
    return asyncio.run(go())


def test_public_sources_need_no_credentials(monkeypatch):
    for env in ('SARAMIN_ACCESS_KEY','WANTED_AUTH_VALUE','WANTED_AUTH_HEADER','WANTED_APPROVED_LIST_URL'):
        monkeypatch.delenv(env, raising=False)
    for key in ('public_greenhouse','public_lever','public_ashby','public_smartrecruiters'):
        assert access_status(key)[0]
        assert key in CRAWLERS
    assert not access_status('saramin')[0]


def test_greenhouse_discovery_no_detail_requests(monkeypatch):
    monkeypatch.setenv('PUBLIC_GREENHOUSE_BOARDS','get-delight')
    crawler = GreenhousePublicCrawler()
    url = 'https://boards-api.greenhouse.io/v1/boards/get-delight/jobs'
    fake = FakeFetcher({url:{'jobs':[GH]}})
    refs = collect(crawler, fake)
    assert len(refs) == 1 and len(fake.calls) == 1
    assert fake.calls[0][1]['params'] == {'content':'true'}
    draft = asyncio.run(crawler.parse(fake, refs[0]))
    assert draft.organization == 'delight.ai (Sendbird)'
    assert draft.body_text == '초급 개발자 인턴'
    assert draft.external_id == 'get-delight:8639423002'
    assert draft.posted_on is None  # updated_at must NOT be misused as posted date
    assert len(fake.calls) == 1


def test_lever_row_and_pagination(monkeypatch):
    monkeypatch.setenv('PUBLIC_LEVER_SITES','insiderone')
    crawler = LeverPublicCrawler(max_pages=3)
    url = 'https://api.lever.co/v0/postings/insiderone'
    fake = FakeFetcher({url:[LEVER]})
    refs = collect(crawler, fake)
    assert len(refs)==1 and len(fake.calls)==1
    draft = asyncio.run(crawler.parse(fake, refs[0]))
    assert draft.employment_type == 'Intern' and draft.posted_on is not None
    assert draft.external_id == 'insiderone:abc-123'


def test_ashby_omits_unlisted(monkeypatch):
    monkeypatch.setenv('PUBLIC_ASHBY_BOARDS','Acme')
    crawler = AshbyPublicCrawler()
    url='https://api.ashbyhq.com/posting-api/job-board/Acme'
    hidden = {**ASHBY,'jobUrl':'https://jobs.ashbyhq.com/Acme/hidden','isListed':False}
    fake=FakeFetcher({url:{'jobs':[ASHBY,hidden]}})
    refs=collect(crawler,fake)
    assert len(refs)==1
    draft=asyncio.run(crawler.parse(fake,refs[0]))
    assert draft.employment_type == 'Intern' and draft.body_text == 'Building AI agents'
    assert len(fake.calls)==1


def test_smartrecruiters_details_only_for_new(monkeypatch):
    monkeypatch.setenv('PUBLIC_SMARTRECRUITERS_COMPANIES','Acme')
    crawler=SmartRecruitersPublicCrawler()
    lst='https://api.smartrecruiters.com/v1/companies/Acme/postings'
    detail=lst+'/12'
    fake=FakeFetcher({lst:{'content':[{'id':'12','name':'Software Engineer'}]},detail:SMART})
    refs=collect(crawler,fake)
    assert len(fake.calls)==1
    draft=asyncio.run(crawler.parse(fake,refs[0]))
    assert len(fake.calls)==2
    assert draft.region == 'Seoul, KR'
    assert draft.body_text == '백엔드 엔지니어 채용'
    assert draft.original_url == refs[0].url


def test_ref_fingerprint_changed_triggers_refresh(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path / "db.sqlite"}')
    Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine)
    ref=collect(GreenhousePublicCrawler(),FakeFetcher({
        f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs': {'jobs': [GH] if board=='get-delight' else []}
        for board in ['get-delight','seoulrobotics','marqvision','moloco']
    }))[0]
    now=datetime(2026,10,12,tzinfo=timezone.utc)
    with sessions() as db:
        upsert_job(db,parse_greenhouse(GH,'get-delight'))
        mark_detail_checked(db,'public_greenhouse',ref,now=now)
        db.commit()
    with sessions() as db:
        assert not needs_detail(db,'public_greenhouse',ref,now=now,recheck_hours=24)
        from app.crawlers.base import JobRef
        rev=JobRef(ref.external_id,ref.url,'changed',ref.payload)
        assert needs_detail(db,'public_greenhouse',rev,now=now,recheck_hours=24)
    engine.dispose()


def test_board_tokens_not_urls_or_paths(monkeypatch):
    monkeypatch.setenv('PUBLIC_GREENHOUSE_BOARDS','../../secret')
    with pytest.raises(ValueError):
        GreenhousePublicCrawler()
    monkeypatch.setenv('PUBLIC_GREENHOUSE_BOARDS','get-delight,get-delight')
    assert GreenhousePublicCrawler().boards==['get-delight']


def test_provider_link_validation():
    with pytest.raises(ValueError):
        parse_greenhouse({**GH,'absolute_url':'http://job-boards.greenhouse.io/get-delight/jobs/1'},'get-delight')
    with pytest.raises(ValueError):
        parse_lever({**LEVER,'hostedUrl':'https://evil.example/jobs'},'insiderone')
    with pytest.raises(ValueError):
        parse_ashby({**ASHBY,'applyUrl':'javascript:alert(1)'},'Acme')


def test_smart_recruiters_metadata():
    row = {'id':'99','name':'Senior Dev','company':{'name':'Company'},'location':{'city':'Busan'},
           'typeOfEmployment':{'label':'Permanent'}}
    draft=parse_smartrecruiters(row,'Company','https://jobs.smartrecruiters.com/Company/99')
    assert draft.title=='Senior Dev' and draft.region=='Busan' and draft.body_text is None
