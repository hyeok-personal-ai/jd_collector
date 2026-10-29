"""v2.1 QA contract: parsing confidence must never masquerade as live success."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import pytest

from app.verification import make_report, inspect_draft, report_summary
from app.verification_cli import offline_snapshots, live_domestic, live_public
from app.verification_store import store_report,latest_reports,verification_overview
from app.schema import JobDraft
from app.database import Base
from app.site_family import identify

ROOT = Path(__file__).parent.parent


def sample(**updates):
    content=dict(source_id='demo',external_id='10',title='Backend Engineer',
        organization='Example',original_url='https://example.org/jobs/10',
        body_text='A long public job description',region='Seoul',apply_url='https://example.org/jobs/10')
    content.update(updates)
    return JobDraft(**content)


def test_offline_snapshot_explicitly_not_live():
    reports=offline_snapshots(ROOT/'fixtures/v21_snapshots.json',sample_limit=3)
    assert len(reports)==2
    assert all(r['mode']=='offline_fixture' and r['reachable'] is None for r in reports)
    assert all(r['status']=='sample_ok' for r in reports)
    assert sum(r['sample_parsed'] for r in reports)==2
    assert sum(r['reference_fields_checked'] for r in reports)==10
    assert sum(r['reference_fields_correct'] for r in reports)==10
    assert reports[0]['discovered']==2 and reports[0]['sample_attempted']==1


def test_only_annotated_fields_are_accuracy_evidence():
    check=inspect_draft(sample(),reference={'title':'Backend Engineer','region':'부산'})
    assert check['reference_comparisons']['title']['match'] is True
    assert check['reference_comparisons']['region']['match'] is False
    report=make_report(source_key='demo',url='https://example.org',family='generic_html',
        mode='offline_fixture',discovered=1,attempted=1,checks=[check])
    assert report['reference_exact_match_rate']==0.5
    assert report['required_coverage']==1
    assert report['optional_coverage']<1
    assert '부산' not in json.dumps(report,ensure_ascii=False)  # never store truth values


def test_no_gold_data_no_accuracy_rate():
    report=make_report(source_key='x',url='https://example.org',family='ashby',
        mode='live_public',discovered=1,attempted=1,checks=[inspect_draft(sample())])
    assert report['reference_exact_match_rate'] is None
    assert report['reference_fields_checked']==0

@pytest.mark.parametrize('counts,expected',[
    ({'discovered':0,'attempted':0},'empty_unverified'),
    ({'discovered':4,'attempted':0},'list_only'),
    ({'discovered':3,'attempted':2,'errors':['bad']},'failed'),
])
def test_empty_and_fail_closed(counts,expected):
    r=make_report(source_key='x',url='https://example.org',family='greeting',mode='live_domestic',**counts)
    assert r['status']==expected


def test_partial_if_some_details_failed():
    r=make_report(source_key='x',url='https://example.org',family='greeting',
        mode='live_domestic',discovered=2,attempted=2,checks=[inspect_draft(sample())],errors=['detail 2 failed'])
    assert r['status']=='partial'


def test_status_persistence_only_not_catalog(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path/"app.db"}')
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    r=make_report(source_key='greeting:sample',url='https://example.org',family='greeting',
        mode='offline_fixture',discovered=1,attempted=1,checks=[inspect_draft(sample())])
    with sessions() as db:
        store_report(db,r)
        db.commit()
    with sessions() as db:
        r2=make_report(source_key='greeting:sample',url='https://example.org',family='greeting',
            mode='live_domestic',discovered=0,errors=['HTTP 403'])
        store_report(db,r2)
        db.commit()
    with sessions() as db:
        latest=latest_reports(db)
        assert len(latest)==1 and latest[0]['status']=='failed'
        assert verification_overview(db)['live_sample_ok']==0
        with pytest.raises(ValueError):
            store_report(db,{'mode':'catalog_only'})
    engine.dispose()


def test_untrusted_earlier_sample_cannot_overwrite_live_failed_status(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path/"t.db"}')
    Base.metadata.create_all(engine)
    Session=sessionmaker(engine)
    from app.database import SourceVerification
    r=make_report(source_key='x',url='https://example.org',family='greeting',mode='offline_fixture',
                  discovered=1,attempted=1,checks=[inspect_draft(sample())])
    with Session() as s:
        store_report(s,r)
        store_report(s,{**r,'mode':'live_domestic','status':'failed','errors':['HTTP 403']})
        s.commit()
    with Session() as s:
        assert latest_reports(s)[0]['status']=='failed'
    engine.dispose()


def test_offline_snapshot_rejects_traversal(tmp_path):
    list_path=ROOT/'fixtures/domestic_greeting_list.html'
    manifest=tmp_path/'input.json'
    manifest.write_text(json.dumps({'boards':[{'family':'greeting','company':'Acme',
        'url':'https://x.career.greetinghr.com/ko/position',
        'list_file':'../private.html'}]}))
    (tmp_path.parent/'private.html').write_text('<a>private</a>')
    with pytest.raises(ValueError,match='snapshot'):
        offline_snapshots(manifest)


def test_reference_can_only_compare_supported_fields():
    with pytest.raises(ValueError,match='Invalid reference'):
        inspect_draft(sample(),reference={'password':'secret'})


def test_live_domestic_requires_operator_access(monkeypatch):
    monkeypatch.delenv('ALLOW_HTML_DOMESTIC_ATS',raising=False)
    with pytest.raises(PermissionError):
        asyncio.run(live_domestic())


def test_live_public_no_network_fake_failed(monkeypatch):
    import app.verification_cli as vc
    class FailingFetch:
        def __init__(self, allowed_hosts): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    class FakeCrawler:
        allowed_hosts=frozenset({'api.lever.co'})
        def __init__(self, max_pages=None): pass
        async def discover(self, f):
            raise PermissionError('robots.txt denied')
            yield
    monkeypatch.setattr(vc,'PROVIDERS',{'lever':FakeCrawler})
    monkeypatch.setattr(vc,'PoliteFetcher',FailingFetch)
    monkeypatch.setattr(vc,'load_boards',lambda:[{'provider':'lever','id':'x','enabled':True}])
    reports=asyncio.run(vc.live_public(max_boards=1))
    assert reports[0]['status']=='failed'
    assert reports[0]['reachable'] is None
    assert reports[0]['sample_parsed']==0


def test_live_domestic_empty_list_is_failure(monkeypatch,tmp_path):
    import app.verification_cli as vc
    config=tmp_path/'authorized.json'
    config.write_text(json.dumps({'boards':[{'family':'ninehire','company':'페이히어',
        'url':'https://payhere.ninehire.site/recruit','authorized':True}]}))
    monkeypatch.setenv('DOMESTIC_ATS_FILE',str(config))
    monkeypatch.setenv('ALLOW_HTML_DOMESTIC_ATS','1')
    class EmptyFetcher:
        def __init__(self, hosts): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get_text(self,url): return '<html><h1>채용중</h1></html>'
    monkeypatch.setattr(vc,'PoliteFetcher',EmptyFetcher)
    reports=asyncio.run(vc.live_domestic(max_boards=1))
    assert reports[0]['status']=='failed'
    assert reports[0]['reachable'] is True
    assert reports[0]['discovered']==0


def test_api_verification_report_endpoints(tmp_path,monkeypatch):
    from app import api
    from app.database import get_session
    engine=create_engine(f'sqlite:///{tmp_path/"web.db"}',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    Local=sessionmaker(engine)
    r=make_report(source_key='test',url='https://example.org',family='greeting',
        mode='offline_fixture',discovered=1,attempted=1,checks=[inspect_draft(sample())])
    with Local() as s:
        store_report(s,r)
        s.commit()
    def override():
        with Local() as s: yield s
    api.app.dependency_overrides[get_session]=override
    monkeypatch.setattr(api,'init_db',lambda:None)
    with TestClient(api.app) as client:
        res=client.get('/api/verification/summary')
        assert res.status_code==200
        assert res.json()['offline_samples']==1
        rows=client.get('/api/verification/recent').json()['items']
        assert len(rows)==1 and rows[0]['mode']=='offline_fixture'
        assert client.get('/').status_code==200
        assert '출처별 수집 현황' in client.get('/').text
    api.app.dependency_overrides.clear()
    engine.dispose()


def test_catalog_url_detection_does_not_grant_rights():
    g=identify('https://x.career.greetinghr.com/ko/position')
    assert g.family=='greeting' and g.auto_enabled is False


def test_short_detail_is_review_not_success():
    tiny=inspect_draft(sample(body_text=None,organization=None))
    r=make_report(source_key='x',url='https://example.org',family='ashby',
        mode='live_public',discovered=1,attempted=1,checks=[tiny])
    assert r['status']=='needs_review' and r['required_coverage']==0.5


def test_145_targets_catalog_offline_without_fake_verification():
    from app.company_catalog import load_companies
    from app.site_family import catalog_report
    rows=load_companies()
    r=catalog_report(rows)
    assert len(rows)==145
    assert r['employer_targets']==145
    assert sum(r['stages'].values())==145
