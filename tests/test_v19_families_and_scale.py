"""v1.9 offline regression for scalable discovery and conservative family adapters."""
import asyncio
import json
import sqlite3
from pathlib import Path
from app.site_family import identify, catalog_report
from app.company_catalog import load_companies
from app.scheduler import PollPlanner, active_boards
from app.crawlers.approved_company_listings import extract_links,load_listing_config,ApprovedCompanyListingCrawler
import pytest


@pytest.fixture(autouse=True)
def ensure_default_board_catalog(monkeypatch):
    monkeypatch.delenv('PUBLIC_BOARD_CATALOG',raising=False)


def test_145_targets_five_stages_and_no_auto_crawling():
    rows=load_companies()
    assert len(rows)==145
    assert [sum(r['priority_stage']==stage for r in rows) for stage in range(1,6)]==[25,35,25,35,25]
    assert sum(r.get('url_verification')=='candidate_unverified' for r in rows)>=75
    report=catalog_report(rows)
    assert report['employer_targets']==145
    assert report['unique_portal_paths']<145
    assert sum('ats_provider' in r for r in rows)==15
    assert len(active_boards())==40  # No new unapproved website crawler enabled


@pytest.mark.parametrize('address,family,adapter',[
    ('https://jobs.lever.co/zoyi','lever','public_lever'),
    ('https://jobs.ashbyhq.com/furiosa-ai','ashby','public_ashby'),
    ('https://job-boards.greenhouse.io/moloco','greenhouse','public_greenhouse'),
    ('https://careers.smartrecruiters.com/example','smartrecruiters','public_smartrecruiters'),
    ('https://kakaopay.career.greetinghr.com/ko/main','greeting','domestic_ats'),
    ('https://payhere.ninehire.site/','ninehire','domestic_ats'),
    ('https://ridi.recruit.roundhr.com/','roundhr','approved_company_listings'),
])
def test_host_patterns(address,family,adapter):
    result=identify(address)
    assert (result.family,result.recommended_adapter)==(family,adapter)
    assert result.auto_enabled is (family in ('lever','ashby','greenhouse','smartrecruiters'))


def test_custom_domain_fingerprint_and_no_naive_text_guess():
    html='<html><body>나인하이어, 그리팅 도입사 채용 설명</body></html>'
    assert identify('https://careers.example.com/',html).family=='generic_html'
    html='<script src="https://assets.career.greetinghr.com/app/v1.js"></script>'
    detection=identify('https://careers.example.com/',html)
    assert detection.family=='greeting' and not detection.auto_enabled


def test_jsonld_and_spa_are_different_patterns():
    page='<script type="application/ld+json">{"@type":"JobPosting","title":"Backend"}</script>'
    assert identify('https://careers.example.com/post/1',page).family=='jobposting_jsonld'
    spa='<script id="__NEXT_DATA__" type="application/json">{}</script>'
    assert identify('https://careers.example.com/',spa).family=='client_rendered'
    assert identify('https://www.example.com/').family=='unknown'


def test_jsonld_itemlist_same_origin_only_and_valid_config(tmp_path):
    row={'company':'Demo','url':'https://jobs.example.com/careers',
         'detail_path_prefix':'/careers/job/','mode':'jsonld_itemlist','authorized':True}
    doc={'@context':'https://schema.org','@type':'ItemList','itemListElement':[
      {'@type':'ListItem','item':{'name':'Backend Engineer','url':'/careers/job/133'}},
      {'@type':'ListItem','item':{'name':'Other company','url':'https://evil.example/careers/job/1'}},
      {'@type':'ListItem','item':{'name':'Login','url':'/login'}},
      {'@type':'ListItem','item':{'name':'Duplicate','url':'/careers/job/133'}},
    ]}
    html='<script type="application/ld+json">'+json.dumps(doc)+'</script>'
    assert extract_links(html,row)==[('https://jobs.example.com/careers/job/133','Duplicate')]
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'listings':[row]}))
    assert load_listing_config(str(path))[0]['mode']=='jsonld_itemlist'
    with pytest.raises(PermissionError):
        _denied_config(tmp_path,row)


def _denied_config(tmp_path,row):
    p=tmp_path/'deny.json';p.write_text(json.dumps({'listings':[{**row,'authorized':False}]}))
    return load_listing_config(str(p))


def test_list_item_mode_discover_then_parse_offline(tmp_path,monkeypatch):
    row={'company':'Demo','url':'https://jobs.example.com/careers',
         'detail_path_prefix':'/careers/job/','mode':'jsonld_itemlist','authorized':True}
    p=tmp_path/'config.json';p.write_text(json.dumps({'listings':[row]}))
    monkeypatch.setenv('APPROVED_LISTINGS_FILE',str(p))
    listing='<script type="application/ld+json">'+json.dumps({'@type':'ItemList','itemListElement':[
        {'@type':'ListItem','item':{'name':'Junior Backend Developer','url':'/careers/job/133'}}
    ]})+'</script>'
    detail='<html><h1>Junior Backend Developer</h1><main>'+('Python backend engineering services. '*4)+'</main></html>'
    class FakeFetcher:
        async def get_text(self,url,*,capture=False):
            return listing if url.endswith('/careers') else detail
    async def run():
        c=ApprovedCompanyListingCrawler()
        refs=[r async for r in c.discover(FakeFetcher())]
        assert len(refs)==1
        parsed=await c.parse(FakeFetcher(),refs[0])
        assert parsed.organization=='Demo' and parsed.title=='Junior Backend Developer'
    asyncio.run(run())


def test_adaptive_cooldown_fewer_calls_for_unchanged(tmp_path):
    planner=PollPlanner(tmp_path/'state.sqlite')
    board={**active_boards()[0], 'interval':2*3600}
    now=100
    intervals=[]
    for i in range(6):
        lease=planner.acquire(board,now=now)
        assert lease
        planner.complete(board,token=lease,success=True,now=now,change_count=0)
        next_due=next(r['next_due'] for r in planner.plan(now=now) if r['key']==board['key'])
        intervals.append(next_due-now)
        now=next_due+1
    assert intervals[2]>intervals[1]*1.7
    assert intervals[5]>intervals[2]*1.7
    lease=planner.acquire(board,now=now)
    assert lease
    planner.complete(board,token=lease,success=True,now=now,change_count=2)
    reset=next(r for r in planner.plan(now=now) if r['key']==board['key'])
    assert reset['unchanged_streak']==0
    assert reset['next_due']-now<intervals[5]


def test_scheduler_migrates_existing_db(tmp_path):
    path=tmp_path/'old.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('''CREATE TABLE poll_state (source_key TEXT PRIMARY KEY, last_success REAL,
          next_due REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
          lease_until REAL NOT NULL DEFAULT 0,last_status TEXT NOT NULL DEFAULT 'never',
          lease_token TEXT,last_message TEXT)''')
    planner=PollPlanner(path)
    with sqlite3.connect(path) as c:
        cols={r[1] for r in c.execute('PRAGMA table_info(poll_state)')}
        assert 'unchanged_streak' in cols
    assert planner.plan()


def test_api_exposes_family_and_honest_link_status():
    from fastapi.testclient import TestClient
    from app.api import app
    with TestClient(app) as client:
        res=client.get('/api/company-catalog?stage=2')
        assert res.status_code==200
        data=res.json()
        assert data['total']==35
        assert all('site_family' in c for c in data['companies'])
        assert any(c['source_type']=='discovery_homepage' for c in data['companies'])
        summary=client.get('/api/site-family-summary')
        assert summary.status_code==200
        assert summary.json()['employer_targets']==145
        assert '채용 URL' in client.get('/').text


def test_no_unreviewed_config_mode_or_html_cross_origin(tmp_path):
    base={'authorized':True,'company':'Demo','url':'https://jobs.example.com/careers',
          'detail_path_prefix':'/careers/job/'}
    config=tmp_path/'wrong.json'
    config.write_text(json.dumps({'listings':[{**base,'mode':'hidden_api'}]}))
    with pytest.raises(ValueError, match='mode'):
        load_listing_config(str(config))
    config.write_text(json.dumps({'listings':[{**base,'mode':'html_css'}]}))
    with pytest.raises(ValueError, match='selector'):
        load_listing_config(str(config))
