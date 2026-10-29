import asyncio
import json
import time
from types import SimpleNamespace
import httpx
import pytest
from app.aggregate_store import AggregateStore
from app.crawlers.aggregators import InThisWorkPublicCrawler
from app.source_diagnostics import response_evidence, failure_evidence


def post(i):
    return {'id':i,'modified':'2026-10-01','title':{'rendered':'백엔드 개발자'}}


def test_accessible_page_yielded_before_later_failure(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    class Fetcher:
        async def get_response(self,url):
            if '&page=2&' in url:raise PermissionError('HTTP 403: browser security challenge')
            return SimpleNamespace(text=json.dumps([post(1)]),total=2,total_pages=2)
    async def scan():
        crawler=InThisWorkPublicCrawler(store)
        stream=crawler.discover(Fetcher())
        first=await anext(stream)
        assert first.external_id=='1'
        crawler.on_success(first)
        with pytest.raises(PermissionError):await anext(stream)
    asyncio.run(scan())
    assert store.summary('inthiswork')['inventory']=={'done':1}
    assert store.summary('inthiswork')['pages']=={'done':1,'pending':1}
    assert not store.summary('inthiswork')['current_list_complete']


def test_incomplete_scan_never_treats_unvisited_post_as_removed(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.save_page('inthiswork','old',[{'id':'old','url':'https://inthiswork.com/archives/1','payload':post(1)}])
    store.mark('inthiswork','old','done')
    class Fetcher:
        async def get_response(self,url):
            return SimpleNamespace(text=json.dumps([post(2 if '&page=2&' in url else 1)]),total=3,total_pages=3)
    async def scan():return [r async for r in InThisWorkPublicCrawler(store,max_pages=1).discover(Fetcher())]
    refs=asyncio.run(scan())
    assert all(not ref.payload['refresh'] for ref in refs)
    assert 'old' not in {r.external_id for r in refs}
    assert store.pending_pages('inthiswork',1)


def test_response_evidence_omits_body_credentials_and_query():
    response=httpx.Response(403,request=httpx.Request('GET','https://inthiswork.com/robots.txt?secret=private&page=13&per_page=100'),
        headers={'set-cookie':'private','server':'nginx'},text='window.HASHCASH_CONFIG={}; /__challenge/ private')
    result=response_evidence(response)
    assert result['browser_challenge']
    assert 'private' not in json.dumps(result)
    assert result['path']=='/robots.txt'
    assert result['pagination']=={'page':13,'per_page':100}
    assert failure_evidence('robots.txt HTTP 403: browser security challenge')=={
        'http_status':403,'stage':'robots','kind':'browser_challenge'}
