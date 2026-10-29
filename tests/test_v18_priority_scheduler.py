from datetime import date
from types import SimpleNamespace
import json
import sqlite3
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.company_catalog import load_companies,get_statistics
from app.scheduler import PollPlanner, active_boards
from app.duplicate_index import normalize_company,indexed_similar_job_candidates
from app.api import app


@pytest.fixture(autouse=True)
def reset_external_catalog_override(monkeypatch):
    # Earlier suite tests override the process env without reverting it.
    monkeypatch.delenv('PUBLIC_BOARD_CATALOG', raising=False)


def fake_job(i,source,title,org='(주)네이버',region='서울',deadline=date(2026,11,1)):
    return SimpleNamespace(id=i,source_id=source,title=title,organization=org,
       region=region,deadline_on=deadline,original_url=f'https://example.org/jobs/{i}')


def test_catalog_stages_and_ref_integrity():
    rows=load_companies()
    assert len(rows)>=40
    assert {'naver','kakao','line_plus','coupang','woowahan','toss','daangn','zigbang','yanolja'}.issubset({r['id'] for r in rows})
    stats=get_statistics()
    assert stats['companies']==len(rows)
    assert stats['existing_ats_references']+stats['link_only']==len(rows)
    assert stats['stage_counts']['국내 서비스 대기업']>=7


def test_no_custom_sites_automatically_scraped():
    boards=active_boards()
    assert len(boards)==40
    assert not any('naver' in b['key'] or 'kakao' in b['key'] for b in boards)
    assert any(b['key']=='lever:zoyi' for b in boards)
    assert all(b['interval']>0 for b in boards)


def test_url_validation(tmp_path):
    data={'companies':[{**load_companies()[0], 'url':'http://127.0.0.1/admin'}]}
    path=tmp_path/'bad.json';path.write_text(json.dumps(data,ensure_ascii=False))
    with pytest.raises(ValueError):load_companies(path)


def test_duplicate_identifier_rejected(tmp_path):
    rows=load_companies()
    path=tmp_path/'bad.json';path.write_text(json.dumps({'companies':[rows[0],rows[0]]},ensure_ascii=False))
    with pytest.raises(ValueError):load_companies(path)


def test_plan_does_not_claim_link_only_collectors(tmp_path):
    planner=PollPlanner(tmp_path/'poll.sqlite')
    result=planner.plan(now=1000)
    assert result and all(r['due'] for r in result)
    assert result[0]['stage']<=result[-1]['stage']


def test_atomic_provider_lease_blocks_simultaneous_boards(tmp_path):
    path=tmp_path/'poll.sqlite'
    first=PollPlanner(path)
    second=PollPlanner(path)
    a,b=[row for row in active_boards() if row['provider']=='greenhouse'][:2]
    token=first.acquire(a,now=1000,lease_seconds=300)
    assert token
    assert second.acquire(a,now=1000,lease_seconds=300) is None
    assert second.acquire(b,now=1000,lease_seconds=300) is None
    first.complete(a,token=token,now=1100,success=True)
    assert second.acquire(b,now=1100,lease_seconds=300)


def test_success_cooldown_and_failure_backoff(tmp_path):
    planner=PollPlanner(tmp_path/'poll.sqlite')
    b=active_boards()[0]
    token=planner.acquire(b,now=100)
    planner.complete(b,token=token,now=200,success=True)
    assert planner.acquire(b,now=201) is None
    assert planner.plan(now=201)
    due=200+b['interval']+int(int(__import__('hashlib').sha256(b['key'].encode()).hexdigest()[:8],16)/0xffffffff*b['interval']*.1)
    token=planner.acquire(b,now=due+1)
    assert token
    planner.complete(b,token=token,now=due+1,success=False)
    assert planner.acquire(b,now=due+10) is None
    assert planner.acquire(b,now=due+302)


def test_expired_worker_cannot_overwrite_new_leased_worker(tmp_path):
    planner=PollPlanner(tmp_path/'poll.sqlite')
    board=active_boards()[0]
    old=planner.acquire(board,now=0,lease_seconds=10)
    new=planner.acquire(board,now=11,lease_seconds=10)
    assert new and new!=old
    with pytest.raises(ValueError):planner.complete(board,token=old,now=12,success=False)
    planner.complete(board,token=new,now=12,success=True)


def test_indexed_pair_detection_only_cross_source_and_evidence():
    jobs=[fake_job(1,'a','신입 백엔드 개발자'),fake_job(2,'b','신입 백엔드 개발자',org='NAVER'),
          fake_job(3,'c','프론트엔드 리드',org='네이버'),
          fake_job(4,'b','신입 백엔드 개발자',org='네이버',deadline=date(2026,12,1))]
    assert normalize_company('네이버')=='naver'
    result=indexed_similar_job_candidates(jobs,with_stats=True)
    # Title/region/deadline still must match; aliases assist blocking, do not force merging.
    assert result['compared']<=6
    assert not any({1,4}=={j['id'] for j in p['jobs']} for p in result['candidates'])


def test_bounded_comparisons_in_large_company():
    jobs=[fake_job(i, f'source_{i%3}', 'Software Backend Engineer', '네이버') for i in range(1,1001)]
    output=indexed_similar_job_candidates(jobs,max_candidates_per_job=8,with_stats=True)
    assert output['compared']<=8*len(jobs)
    assert output['compared']>0


def test_catalog_api_and_homepage():
    with TestClient(app) as client:
        data=client.get('/api/company-catalog?stage=1').json()
        assert data['total']==get_statistics()['stage_counts']['국내 서비스 대기업']
        assert all(x['priority_stage']==1 for x in data['companies'])
        assert client.get('/api/company-catalog?stage=0').status_code==422
        html=client.get('/').text
        assert 'companyStage' in html and 'loadCompanies' in html
