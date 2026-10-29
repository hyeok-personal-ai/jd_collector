from pathlib import Path
import asyncio
import json
import pytest
from app.crawlers.approved_company_listings import load_listing_config, extract_links, ApprovedCompanyListingCrawler

BASE = {'company':'Example Inc','url':'https://careers.example.com/jobs/',
        'selector':'a.job','detail_path_prefix':'/jobs/detail/','authorized':True}


def test_link_whitelist_and_dedupe():
    html='''<html><a class="job" href="/jobs/detail/123">Backend Engineer</a>
      <a class="job" href="https://evil.example/jobs/detail/321">Not permitted</a>
      <a class="job" href="javascript:alert(1)">Unsafe</a>
      <a class="job" href="/jobs/detail/123#fragment">Duplicate</a>
      <a class="job" href="/jobs/">Generic listing</a></html>'''
    assert extract_links(html,BASE)==[('https://careers.example.com/jobs/detail/123','Duplicate')]


def test_config_requires_explicit_authorization(tmp_path):
    row={**BASE,'authorized':False}
    file=tmp_path/'listing.json';file.write_text(json.dumps({'listings':[row]}))
    with pytest.raises(PermissionError):load_listing_config(str(file))
    row['authorized']=True
    file.write_text(json.dumps({'listings':[row]}))
    assert load_listing_config(str(file))[0]['url']==BASE['url']


def test_offline_discover_and_parse_without_making_network_requests(tmp_path,monkeypatch):
    file=tmp_path/'listing.json';file.write_text(json.dumps({'listings':[BASE]}))
    monkeypatch.setenv('APPROVED_LISTINGS_FILE',str(file))
    crawler=ApprovedCompanyListingCrawler()
    listing='<a class="job" href="/jobs/detail/123">Backend Engineer</a>'
    detail='<html><h1>Backend Engineer</h1><main><h1>Backend Engineer</h1><p>Design and build API systems and services for customers and colleagues.</p></main></html>'
    class FixtureFetcher:
        async def get_text(self,url,*,capture=False):
            assert url.startswith('https://careers.example.com/jobs/')
            return listing if url.endswith('/jobs/') else detail
    async def check():
        refs=[ref async for ref in crawler.discover(FixtureFetcher())]
        assert len(refs)==1
        job=await crawler.parse(FixtureFetcher(),refs[0])
        assert job.title=='Backend Engineer'
        assert job.organization=='Example Inc'
        assert job.source_id=='approved_company_listings'
        assert len(job.body_text)>40
        assert not crawler.completed_scopes
    asyncio.run(check())


def test_config_rejects_private_ip(tmp_path):
    row={**BASE,'url':'https://127.0.0.1/jobs/'}
    f=tmp_path/'private.json';f.write_text(json.dumps({'listings':[row]}))
    with pytest.raises(ValueError):load_listing_config(str(f))
