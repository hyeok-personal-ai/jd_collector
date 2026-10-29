import asyncio
from types import SimpleNamespace
import pytest
from app.aggregate_store import AggregateStore
from app.crawlers.aggregators import ZighangPublicCrawler
from app.development_focus import candidate_links
from app.job_labels import label_job
from app.schema import JobDraft

ID='885ec013-4342-4a18-81f7-dcb9ec6d6c85'
URL='https://www.zighang.com/recruitment/'+ID


def test_public_link_input_is_canonical_and_deduplicated():
    assert candidate_links([URL,URL.replace('www.','')+'?utm_source=share']) == [{'id':ID,'url':URL}]


@pytest.mark.parametrize('url',['https://evil.example/recruitment/'+ID,
    'https://api.zighang.com/recruitment/'+ID, 'http://zighang.com/recruitment/'+ID,
    'https://zighang.com@evil.example/recruitment/'+ID])
def test_candidate_import_cannot_expand_fetch_hosts(url):
    with pytest.raises(ValueError):candidate_links([url])


def test_focus_keeps_general_inventory_but_never_fetches_it(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.pages('zighang',['page'])
    store.save_page('zighang','page',[{'id':'general','url':'https://example.org/general'},
        {'id':ID,'url':URL,'revision':'source-revision'}])
    store.add_focus('zighang',[{'id':ID,'url':URL}])
    store.meta('zighang','collection_mode','development')
    import time
    store.meta('zighang_recent','last_scan',time.time())
    class NoNetwork:
        async def get_text(self,*args,**kwargs):raise AssertionError('Focused discovery must not scan broad sitemaps')
    async def discover():
        return [r async for r in ZighangPublicCrawler(store).discover(NoNetwork())]
    refs=asyncio.run(discover())
    assert [r.external_id for r in refs]==[ID]
    assert refs[0].listing_fingerprint=='source-revision'
    assert store.summary('zighang')['inventory']['pending']==2
    assert store.focus_summary('zighang')=={'pending':1}
    store.mark('zighang',ID,'done',active=True,recheck_mode='fixed',deadline_on='2099-12-31')
    store.add_focus('zighang',[{'id':ID,'url':URL}])
    assert asyncio.run(discover())==[]


def test_finished_focus_does_not_loop_over_deferred_general_backlog(monkeypatch,tmp_path):
    from app import service
    from app.crawlers.registry import CRAWLERS
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.pages('zighang',['page'])
    store.save_page('zighang','page',[{'id':'general','url':'https://example.org/general'}])
    store.meta('zighang','collection_mode','development')
    monkeypatch.setitem(CRAWLERS,'zighang',lambda:ZighangPublicCrawler(store))
    async def collected(*args,**kwargs):return SimpleNamespace(status='completed')
    monkeypatch.setattr(service,'run_crawler',collected)
    result=asyncio.run(service.collect_task({'spec':{'kind':'aggregate','source':'zighang'}},{'max_items':100}))
    assert result.status=='completed'


@pytest.mark.parametrize('title',['게임 클라이언트 프로그래머','웹개발 신입','SW 개발 엔지니어','Python programmer'])
def test_programming_titles_are_searchable_as_development(title):
    assert label_job(JobDraft(source_id='demo',external_id='1',title=title,original_url='https://example.org')).it_match=='yes'


@pytest.mark.parametrize('title,expected',[('위성·레이더 HW 개발 엔지니어 (FPGA·회로설계)','unknown'),
    ('SAR/레이다 시스템 엔지니어','unknown'), ('임베디드 펌웨어·하드웨어 소프트웨어 개발자','yes')])
def test_hardware_roles_need_explicit_software_evidence(title,expected):
    assert label_job(JobDraft(source_id='demo',external_id='1',title=title,original_url='https://example.org')).it_match==expected


@pytest.mark.parametrize('title,expected',[
    ('네이버웹툰|작가 발굴 및 프로듀싱 (Contents Developer)','no'),
    ('SD바이오센서|CGMS 센서 개발자','unknown'),
    ('기구설계 개발자 채용','unknown'),
    ('모듈 소재/부품 개발 엔지니어 채용','unknown'),
    ('일본법인 영업 마케팅 - 소프트웨어 국내 1위기업','no'),
    ('소프트웨어 회사 일본 법인장','no'),
    ('IT 인프라 기술영업 경력직','no'),
    ('센서 회사|Java Developer','yes'),
    ('영업 플랫폼 백엔드 개발자','yes')])
def test_developer_word_and_company_industry_do_not_override_the_actual_role(title,expected):
    assert label_job(JobDraft(source_id='demo',external_id='1',title=title,original_url='https://example.org')).it_match==expected
