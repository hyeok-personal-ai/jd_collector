"""Synthetic layouts based on documented public URL patterns; not live HTML captures."""
from __future__ import annotations
import asyncio
import json
from datetime import date
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
import pytest

from app.access_policy import access_status
from app.crawlers.domestic_ats import (load_domestic_config, extract_domestic_links,
    parse_domestic_detail, diagnose_listing, DomesticATSCrawler)
from app.database import Base, Job, RawHtml
from app.fetcher import FetchResult
from app.runner import run_crawler
from app.site_family import identify

ROOT = Path(__file__).parent.parent
GREETING={'family':'greeting','company':'김캐디','url':'https://kimcaddie.career.greetinghr.com/ko/position','authorized':True}
NINEHIRE={'family':'ninehire','company':'페이히어','url':'https://payhere.ninehire.site/recruit','authorized':True}


def read(name):
    return (ROOT/'fixtures'/name).read_text(encoding='utf-8')


def test_greeting_list_dedup_subdomain_and_application_guard():
    links=extract_domestic_links(read('domestic_greeting_list.html'),GREETING)
    assert len(links)==2
    assert [r[0] for r in links]==['206177','198607']
    assert all('/apply' not in url and 'other.' not in url for _,url,_ in links)
    assert all('?' not in url for _,url,_ in links)


def test_greeting_detail_field_extraction():
    draft=parse_domestic_detail(read('domestic_greeting_detail.html'),
        'https://kimcaddie.career.greetinghr.com/ko/o/206177', GREETING)
    assert draft.title.startswith('백엔드')
    assert draft.organization=='김캐디'
    assert draft.deadline_on==date(2026,12,31)
    assert draft.employment_type=='정규직'
    assert draft.region=='서울특별시 서초구'
    assert '경력사항: 신입' in draft.body_text
    assert draft.source_status=='unknown'  # No inferred active flag


def test_ninehire_url_pattern_and_detail():
    links=extract_domestic_links(read('domestic_ninehire_list.html'),NINEHIRE)
    assert len(links)==1
    assert links[0][0]=='3ETue9oP'
    assert links[0][1]=='https://payhere.ninehire.site/job_posting/3ETue9oP'
    job=parse_domestic_detail(read('domestic_ninehire_detail.html'),links[0][1],NINEHIRE)
    assert job.employment_type=='정규직' and job.region.startswith('부산')
    assert job.deadline_on is None and job.source_status=='unknown'


def test_greeting_scope_is_company_stable():
    a=parse_domestic_detail(read('domestic_greeting_detail.html'),
        'https://kimcaddie.career.greetinghr.com/ko/o/206177', GREETING)
    other={**GREETING,'url':'https://other.career.greetinghr.com/ko/position'}
    b=parse_domestic_detail(read('domestic_greeting_detail.html'),
        'https://other.career.greetinghr.com/ko/o/206177', other)
    assert a.external_id != b.external_id
    assert a.source_id==b.source_id=='domestic_ats'


def test_refuse_login_form_short_html_and_cross_company():
    with pytest.raises(ValueError):
        parse_domestic_detail('<h1>회원가입</h1><form>비밀번호</form>',
            'https://kimcaddie.career.greetinghr.com/ko/o/206177',GREETING)
    with pytest.raises(ValueError):
        parse_domestic_detail(read('domestic_greeting_detail.html'),
            'https://evil.example/ko/o/206177', GREETING)
    with pytest.raises(ValueError):
        parse_domestic_detail(read('domestic_greeting_detail.html'),
            'https://kimcaddie.career.greetinghr.com/ko/o/206177/apply', GREETING)


def test_config_requires_opt_in_and_provider_hostname(tmp_path):
    path=tmp_path/'boards.json'
    path.write_text(json.dumps({'boards':[{**GREETING,'authorized':False}]}))
    with pytest.raises(PermissionError):
        load_domestic_config(str(path))
    path.write_text(json.dumps({'boards':[GREETING,NINEHIRE]}))
    assert len(load_domestic_config(str(path)))==2
    path.write_text(json.dumps({'boards':[{**GREETING,'url':'https://jobs.example.org/ko/position'}]}))
    with pytest.raises(ValueError,match='hostname'):
        load_domestic_config(str(path))
    path.write_text(json.dumps({'boards':[{**GREETING,'url':'https://127.0.0.1/ko/position'}]}))
    with pytest.raises(ValueError):
        load_domestic_config(str(path))


def test_empty_list_and_dynamic_marker_are_not_fabricated():
    diagnosis=diagnose_listing('<html><script id="__NEXT_DATA__">{}</script><h2>공고</h2></html>',NINEHIRE)
    assert diagnosis['recognized_jobs']==0
    assert diagnosis['possible_client_rendering'] is True
    assert diagnosis['outcome']=='manual_render_or_selector_review'


def test_no_access_by_default():
    assert access_status('domestic_ats',{})[0] is False
    assert access_status('domestic_ats',{'ALLOW_HTML_DOMESTIC_ATS':'1'})[0] is True
    for url,family in [(GREETING['url'],'greeting'),(NINEHIRE['url'],'ninehire')]:
        x=identify(url)
        assert x.family==family and x.recommended_adapter=='domestic_ats' and x.auto_enabled is False


def test_adapter_pipeline_twice_skips_duplicate_details(tmp_path,monkeypatch):
    config=tmp_path/'boards.json'
    config.write_text(json.dumps({'boards':[GREETING]}),encoding='utf-8')
    monkeypatch.setenv('DOMESTIC_ATS_FILE',str(config))
    monkeypatch.setenv('ALLOW_HTML_DOMESTIC_ATS','1')
    monkeypatch.delenv('NORMALIZATION_LLM_MODEL',raising=False)
    crawl=DomesticATSCrawler()
    engine=create_engine(f'sqlite:///{tmp_path/"test.sqlite"}')
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    listing='<html><h2>채용 중</h2><a href="/ko/o/206177">백엔드 개발자 포지션</a></html>'
    detail=read('domestic_greeting_detail.html')
    calls=[]
    class FakeFetcher:
        def __init__(self, allowed_hosts):
            self._html={}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get_text(self,url,*,capture=False):
            calls.append(url)
            page= detail if url.endswith('/206177') else listing
            if capture: self._html[url]=page
            return page
        async def get_response(self,url,*,headers=None,accept=None):
            calls.append(url)
            if headers and headers.get('If-None-Match'):
                return FetchResult(304,None,etag='"v1"')
            return FetchResult(200,listing,etag='"v1"')
        def pop_captured_html(self,url):
            return self._html.pop(url,None)
    monkeypatch.setattr('app.runner.PoliteFetcher', FakeFetcher)
    monkeypatch.setattr('app.runner.init_db',lambda:None)
    one=asyncio.run(run_crawler(crawl,session_factory=sessions))
    two=asyncio.run(run_crawler(DomesticATSCrawler(),session_factory=sessions))
    assert (one.created_count,one.seen_count,one.status)==(1,1,'completed')
    assert (two.created_count,two.seen_count,two.status)==(0,0,'completed')
    assert len([c for c in calls if c.endswith('/206177')])==1
    with sessions() as session:
        assert len(session.scalars(select(Job)).all())==1
        assert len(session.scalars(select(RawHtml)).all())==1
    engine.dispose()


def test_missing_links_do_not_mark_jobs_closed(tmp_path,monkeypatch):
    config=tmp_path/'boards.json'
    config.write_text(json.dumps({'boards':[NINEHIRE]}))
    monkeypatch.setenv('DOMESTIC_ATS_FILE',str(config))
    class Fetcher:
        async def get_text(self,url): return '<html><h1>채용 공고</h1></html>'
    async def collect():
        c=DomesticATSCrawler()
        refs=[r async for r in c.discover(Fetcher())]
        return c, refs
    c,refs=asyncio.run(collect())
    assert refs==[]
    assert c.completed_scopes=={}
    assert len(c.scope_errors)==1
