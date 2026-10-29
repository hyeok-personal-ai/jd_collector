import asyncio
import copy
import json
import time
from types import SimpleNamespace
import pytest
import httpx
from app.fetcher import PoliteFetcher,BrowserChallengeError
from app.aggregate_store import AggregateStore
from app.crawlers.aggregators import ZighangPublicCrawler
from app.crawlers.base import JobRef,PostingUnavailable
from app.crawlers.zighang_public import company_candidates,company_urls,COMPANY_SOURCE,development_candidates

ID='00000000-0000-0000-0000-000000000001'
CID='00000000-0000-0000-0000-000000000002'
URL='https://www.zighang.com/recruitment/'+ID
COMPANY='https://www.zighang.com/company/'+CID


def posting(**fields):
    return {'id':ID,'title':'백엔드 개발자','company':{'id':CID,'name':'기업'},'status':'ACTIVE',
            'endDate':'2099-12-31','deadlineType':'마감일','depthOnes':['IT_개발'],
            'content':{'type':'doc','content':[{'type':'text','text':'API 서비스 개발 업무'}]},**fields}


def page(row):
    stream='a:'+json.dumps({'jobs':[row]},ensure_ascii=False)+'\n'
    return '<script>self.__next_f.push('+json.dumps([1,stream],ensure_ascii=False)+')</script>'


@pytest.mark.parametrize('status',[200,403])
def test_wordpress_challenge_stops_before_any_content_request(status):
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(status,text='<script>window.HASHCASH_CONFIG={};</script><script src="/__challenge/challenge.js"></script>')
    async def collect():
        async with PoliteFetcher(frozenset({'example.org'}),transport=httpx.MockTransport(handler)) as f:
            await f.get_text('https://example.org/wp-json/wp/v2/posts')
    with pytest.raises(BrowserChallengeError,match='site operator bot allowance required'):asyncio.run(collect())
    assert requests==['/robots.txt']


def test_company_snapshot_avoids_per_post_http_and_ignores_view_changes(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite');store.meta('zighang','collection_mode','development')
    store.pages(COMPANY_SOURCE,[COMPANY])
    class Fetcher:
        def __init__(self):self.requests=[]
        async def get_text(self,url,**kwargs):
            self.requests.append(url);assert url==COMPANY
            return page(posting())
    f=Fetcher();crawler=ZighangPublicCrawler(store)
    async def collect():
        refs=[ref async for ref in crawler.discover(f)]
        assert len(refs)==1
        draft=await crawler.parse(f,refs[0])
        assert draft.body_text=='API 서비스 개발 업무'
    asyncio.run(collect())
    assert f.requests==[COMPANY]
    store.mark('zighang',ID,'done',active=True,recheck_mode='fixed',deadline_on='2099-12-31')
    changed=posting(views=999)
    store.add_focus('zighang',company_candidates({ID:changed},COMPANY))
    assert store.due('zighang',focus='development')==[]
    changed['content']['content'][0]['text']='改定 API 개발 업무'
    store.add_focus('zighang',company_candidates({ID:changed},COMPANY))
    assert [row['id'] for row in store.due('zighang',focus='development')]==[ID]


def test_expired_company_snapshot_falls_back_to_original_detail(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite');crawler=ZighangPublicCrawler(store)
    row=company_candidates({ID:posting()},COMPANY)[0]
    row['payload']['observed_at']=time.time()-3601
    class Fetcher:
        async def get_text(self,url,**kwargs):
            assert url==URL
            raise RuntimeError('HTTP 404')
    with pytest.raises(PostingUnavailable):asyncio.run(crawler.parse(Fetcher(),JobRef(ID,URL,payload=row['payload'])))


def test_company_updates_known_closure_but_does_not_import_new_history():
    expired=posting(status='EXPIRED')
    assert company_candidates({ID:expired},COMPANY)==[]
    selected=company_candidates({ID:expired},COMPANY,{ID})
    assert selected[0]['payload']['public_row']['status']=='EXPIRED'
    assert company_urls({ID:expired})==[COMPANY]


def test_official_job_family_excludes_content_programming_strategy():
    row=posting(title='콘텐츠 프로그래밍 전략 매니저',depthOnes=['기획_전략_경영'])
    assert development_candidates({ID:row})==[]


def test_company_discovery_refresh_has_cooldown_and_does_not_starve(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite');store.pages(COMPANY_SOURCE,['first','second'])
    store.mark_discovery_page(COMPANY_SOURCE,'first')
    assert store.due_discovery_pages(COMPANY_SOURCE)==['second']
    store.mark_discovery_page(COMPANY_SOURCE,'second',state='failed',error='temporary failure')
    assert store.due_discovery_pages(COMPANY_SOURCE)==[]
    with store.db() as db:db.execute('UPDATE aggregate_pages SET checked=0 WHERE url=?',('first',))
    assert store.due_discovery_pages(COMPANY_SOURCE)==['first']


def test_pending_company_pages_continue_when_detail_queue_is_empty(monkeypatch,tmp_path):
    from app import service,database
    from app.crawlers.registry import CRAWLERS
    store=AggregateStore(tmp_path/'inventory.sqlite');store.meta('zighang','collection_mode','development')
    store.pages(COMPANY_SOURCE,[COMPANY])
    monkeypatch.setitem(CRAWLERS,'zighang',lambda:ZighangPublicCrawler(store))
    async def collected(*args,**kwargs):return SimpleNamespace(status='completed',id=1,error_summary=None)
    monkeypatch.setattr(service,'run_crawler',collected)
    record=SimpleNamespace(status='completed',error_summary=None)
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args):return record
        def commit(self):pass
    monkeypatch.setattr(database,'SessionLocal',Session)
    result=asyncio.run(service.collect_task({'spec':{'kind':'aggregate','source':'zighang'}},{'max_items':100}))
    assert result.status=='limited'
    assert record.status=='limited'
