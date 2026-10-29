import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.aggregate_store import AggregateStore
from app.crawlers.aggregators import InThisWorkPublicCrawler,wp_draft,zighang_draft,sitemap_urls


def post(i):
    return {'id':i,'link':f'https://inthiswork.com/archives/{i}','status':'publish','date':'2026-10-25T12:00:00',
        'modified':'2026-10-25T13:00:00','title':{'rendered':'테스트기업｜백엔드 개발자'},
        'categories':[191700167],'content':{'rendered':'<p>담당업무</p><p>Python 개발</p><img src="https://inthiswork.com/notice.png">'}}


def test_wp_uses_all_pages_and_recovers_without_refetching_done_pages(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    requests=[]
    class Fetcher:
        async def get_response(self,url):
            requests.append(url)
            page=2 if 'page=2&' in url else 1
            return SimpleNamespace(text=json.dumps([post(page)]),total=2,total_pages=2)
    async def discover():
        return [r async for r in InThisWorkPublicCrawler(store).discover(Fetcher())]
    first=asyncio.run(discover())
    assert {r.external_id for r in first}=={'1','2'}
    assert store.summary('inthiswork')['pages']=={'done':2}
    for r in first:store.mark('inthiswork',r.external_id,'done')
    requests.clear()
    assert asyncio.run(discover())==[]
    assert len(requests)==1
    assert store.summary('inthiswork')['complete']


def test_wp_changed_total_refreshes_all_pages_and_retires_removed_pages(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite');total=2;requests=[]
    class Fetcher:
        async def get_response(self,url):
            import re
            page=int(re.search(r'&page=(\d+)',url)[1]);requests.append(page)
            assert page<=total
            return SimpleNamespace(text=json.dumps([post(page)]),total=total,total_pages=total)
    async def scan():return [ref async for ref in InThisWorkPublicCrawler(store).discover(Fetcher())]
    for ref in asyncio.run(scan()):store.mark('inthiswork',ref.external_id,'done')
    total=3;requests.clear();asyncio.run(scan())
    assert requests==[1,2,3]
    assert store.meta('inthiswork','scanned_unique_count')==3
    total=1;requests.clear();refs=asyncio.run(scan())
    assert requests==[1]
    assert store.summary('inthiswork')['pages']['retired']==2
    assert all(ref.payload['refresh'] for ref in refs if ref.external_id!='1')


def test_wp_keeps_images_and_rejects_protected_posts():
    result=wp_draft(post(1))
    assert result.organization=='테스트기업'
    assert '\n' in result.body_text
    assert result.attachments[0].url.endswith('.png')
    protected=post(1);protected['content']['protected']=True
    assert wp_draft(protected).body_text is None
    assert wp_draft(protected).source_status=='unknown'


def test_sitemap_rejects_entities_and_preserves_dates():
    assert sitemap_urls('<urlset><url><loc>https://example.org/job</loc><lastmod>2026-10-25</lastmod></url></urlset>')==[('https://example.org/job','2026-10-25')]
    with pytest.raises(ValueError):sitemap_urls('<!DOCTYPE urlset><urlset/>')


def test_failed_inventory_has_cooldown_and_can_be_retried_after_revision(tmp_path):
    s=AggregateStore(tmp_path/'inventory.sqlite');s.pages('z',['page'])
    s.save_page('z','page',[{'id':'one','url':'https://example.org/one','revision':'1'}])
    s.mark('z','one','failed','temporary failure')
    assert s.due('z')==[]
    s.save_page('z','page',[{'id':'one','url':'https://example.org/one','revision':'2'}])
    assert len(s.due('z'))==1


def test_zighang_live_fixture_uses_structured_deadline_and_employer():
    path=Path('reports/v2.4/zighang-detail.html')
    if not path.exists():pytest.skip('Live fixture not packaged')
    result=zighang_draft(path.read_text(encoding='utf-8'),'test','https://www.zighang.com/recruitment/test')
    assert result.organization=='울산항만공사'
    assert result.deadline_on.isoformat()=='2026-11-05'
    assert result.region=='울산, KR'
    assert len(result.body_text)>100


def test_known_active_rechecks_precede_new_backlog(tmp_path):
    import time
    s=AggregateStore(tmp_path/'inventory.sqlite');s.pages('z',['page'])
    s.save_page('z','page',[{'id':'new','url':'https://example.org/new'},
        {'id':'known','url':'https://example.org/known'},{'id':'closed','url':'https://example.org/closed'}])
    s.mark('z','known','done',active=True);s.mark('z','closed','done',active=False)
    with s.db() as db:db.execute("UPDATE aggregate_refs SET checked=? WHERE state='done'",(time.time()-7*3600,))
    assert [r['id'] for r in s.due('z')]==['known','new']


def test_wp_explicit_deadline_overrides_active_category():
    row=post(1);row['content']['rendered']='<p>접수기간</p><p>2000. 01. 01 ~ 2000. 01. 15</p>'
    result=wp_draft(row)
    assert result.deadline_on.isoformat()=='2000-01-15'
    assert result.source_status=='closed'


def test_wp_external_career_redirect_keeps_source_identity():
    row=post(7);row['link']='https://careers.example.org/jobs'
    result=wp_draft(row)
    assert result.original_url=='https://inthiswork.com/archives/7'
    assert result.apply_url==row['link']


def test_wp_explicit_workplace_is_searchable_without_guessing_country():
    row=post(8);row['content']['rendered']='<p>근무형태 : 정규직(수습기간 3개월)</p><p>Ι 회사위치</p><p>경기도 수원시 영통구 창룡대로 76</p>'
    result=wp_draft(row)
    assert '수원' in result.region and result.employment_type.startswith('정규직')
    assert wp_draft(post(9)).region is None


def test_inventory_never_claims_complete_when_total_mismatches(tmp_path):
    s=AggregateStore(tmp_path/'inventory.sqlite');s.meta('s','expected',2);s.pages('s',['page'])
    s.save_page('s','page',[{'id':'one','url':'https://example.org/one'}]);s.mark('s','one','done')
    assert not s.summary('s')['complete']
    assert not s.summary('s')['count_matches']


def test_ashby_falls_back_to_separate_robots_permitted_public_page():
    from app.crawlers.public_feeds import AshbyPublicCrawler
    identifier='36763c16-8996-4c66-b693-d48329413678'
    class Fetcher:
        async def get_response(self,*args,**kwargs):raise PermissionError('API robots denied')
        async def get_json(self,*args,**kwargs):raise PermissionError('API robots denied')
        async def get_text(self,url,**kwargs):
            data={'organization':{'name':'Example'},'jobBoard':{'jobPostings':[{'id':identifier,'title':'Engineer'}]},
                  'posting':{'id':identifier,'title':'Engineer','descriptionHtml':'<p>Requirements: Python engineering experience</p>','locationName':'Seoul'}}
            return '<script>window.__appData = '+json.dumps(data)+';</script>'
    async def collect():
        crawler=AshbyPublicCrawler();crawler.boards=['example']
        refs=[r async for r in crawler.discover(Fetcher())]
        assert len(refs)==1
        draft=await crawler.parse(Fetcher(),refs[0])
        assert draft.organization=='Example' and draft.region=='Seoul'
        assert crawler.completed_scopes['example']=={'example:'+identifier}
    asyncio.run(collect())


def test_team_overview_is_not_a_job():
    from app.crawlers.company_public import parse_detail
    with pytest.raises(ValueError,match='not an individual vacancy'):
        parse_detail('<main><h1>Engineering</h1><p>지원 자격 '+('개발 업무 '*50)+'</p></main>',
            'https://example.org/careers/teams/product',{'id':'example','name':'Example'})


def test_removed_sitemap_url_is_explicitly_classified(tmp_path):
    from app.crawlers.aggregators import ZighangPublicCrawler
    from app.crawlers.base import JobRef,PostingUnavailable
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.pages('zighang',['page']);store.save_page('zighang','page',[{'id':'gone','url':'https://example.org/gone'}])
    crawler=ZighangPublicCrawler(store)
    class Fetcher:
        async def get_text(self,*a,**kw):raise RuntimeError('HTTP 404 from www.zighang.com')
    ref=JobRef('gone','https://example.org/gone')
    with pytest.raises(PostingUnavailable):asyncio.run(crawler.parse(Fetcher(),ref))
    crawler.on_unavailable(ref)
    assert store.summary('zighang')['inventory']=={'unavailable':1}
    assert store.due('zighang')==[]


def test_removed_known_posting_preserves_body_and_can_reopen():
    from sqlalchemy import create_engine,select
    from sqlalchemy.orm import sessionmaker
    from app.database import Base,Job
    from app.crawlers.base import BaseCrawler,JobRef,PostingUnavailable
    from app.runner import run_crawler
    from app.schema import JobDraft
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    class One(BaseCrawler):
        site_id='demo';allowed_hosts=frozenset({'example.org'})
        async def discover(self,fetcher):yield JobRef('gone','https://example.org/jobs/gone')
        async def parse(self,fetcher,ref):
            return JobDraft(source_id='demo',external_id='gone',original_url=ref.url,title='Backend Engineer',body_text='Python engineering requirements',source_status='open')
    class Gone(One):
        async def parse(self,fetcher,ref):raise PostingUnavailable('HTTP 404')
    async def collect():
        kwargs={'session_factory':factory,'init_database':False,'recheck_hours':0}
        await run_crawler(One(),**kwargs)
        await run_crawler(Gone(),**kwargs)
        with factory() as db:
            saved=db.scalar(select(Job));assert saved.source_status=='unknown'
            assert saved.body_text=='Python engineering requirements'
        await run_crawler(One(),**kwargs)
        with factory() as db:assert db.scalar(select(Job)).source_status=='open'
    asyncio.run(collect());engine.dispose()
