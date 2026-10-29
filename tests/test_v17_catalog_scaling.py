import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.board_catalog import load_boards, company_name
from app.catalog_tools import parse_board_url, import_urls, apply_report
from app.crawlers.public_feeds import _boards, parse_greenhouse


def catalog(tmp_path, boards=None):
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'version': 2, 'boards': boards or []}), encoding='utf-8')
    return path


def test_catalog_40_curated_and_distinct():
    entries = load_boards()
    assert len(entries) == 40
    assert len({(b['provider'],b['id']) for b in entries}) == 40
    assert len([b for b in entries if b.get('focus') == 'kr_it']) >= 20
    ids={(b['provider'],b['id']) for b in entries}
    for key in [('greenhouse','krafton'),('greenhouse','lgairesearch'),
                ('greenhouse','dunamu'),('ashby','vessl-ai'),('ashby','hopae')]:
        assert key in ids


@pytest.mark.parametrize(('url','result'),[
    ('https://job-boards.greenhouse.io/krafton/jobs/123', ('greenhouse','krafton')),
    ('https://boards.greenhouse.io/lgairesearch', ('greenhouse','lgairesearch')),
    ('https://jobs.lever.co/palantir/uuid', ('lever','palantir')),
    ('https://jobs.eu.lever.co/example', ('lever','example')),
    ('https://jobs.ashbyhq.com/Cheiron/uuid', ('ashby','Cheiron')),
    ('https://jobs.smartrecruiters.com/RenesasElectronics/id', ('smartrecruiters','RenesasElectronics')),
])
def test_parse_public_urls(url, result):
    assert parse_board_url(url) == result


@pytest.mark.parametrize('url',[
    'https://evil.com/jobs/krafton',
    'http://job-boards.greenhouse.io/krafton',
    'https://job-boards.greenhouse.io.evil.com/krafton',
    'https://job-boards.greenhouse.io:8443/krafton',
    'https://alice:pass@jobs.lever.co/palantir',
    'https://job-boards.greenhouse.io/',
    'https://boards.greenhouse.io/embed/job_board?for=foo',
])
def test_no_untrusted_board_import(url):
    with pytest.raises(ValueError):
        parse_board_url(url)


def test_import_urls_dedup_disabled_by_default_then_activate(tmp_path, monkeypatch):
    path = catalog(tmp_path,[{'provider':'greenhouse','id':'krafton','company':'KRAFTON'}])
    urls=tmp_path/'new.csv'
    urls.write_text('url,company\nhttps://job-boards.greenhouse.io/krafton,KRAFTON\nhttps://jobs.ashbyhq.com/vessl-ai/123,VESSL AI\nhttps://jobs.ashbyhq.com/vessl-ai/456,VESSL AI\n',encoding='utf-8')
    before=path.read_text()
    result=import_urls(path,urls,dry_run=True)
    assert result['added']==1 and path.read_text()==before
    result=import_urls(path,urls)
    assert result['added']==1 and result['skipped_existing']==2
    monkeypatch.setenv('PUBLIC_BOARD_CATALOG',str(path))
    rows=load_boards(path)
    assert len(rows)==2
    assert rows[1]['enabled'] is False
    assert company_name('ashby','vessl-ai')=='VESSL AI'
    report=tmp_path/'verify.json'
    report.write_text(json.dumps([{'provider':'ashby','board':'vessl-ai','status':'failed'}]))
    assert apply_report(path,report)['enabled']==0
    assert load_boards(path)[1]['enabled'] is False
    report.write_text(json.dumps([{'provider':'ashby','board':'vessl-ai','status':'sample_ok'}]))
    assert apply_report(path,report)['enabled']==1
    assert load_boards(path)[1]['enabled'] is True


def test_import_many_boards_and_dedupe(tmp_path):
    path = catalog(tmp_path)
    urls=tmp_path/'more.txt'
    urls.write_text('\n'.join(f'https://job-boards.greenhouse.io/board{i}' for i in range(250)))
    assert import_urls(path,urls)['added']==250
    assert len(load_boards(path))==250
    assert len(_boards('TEST_BOARDS',','.join(f'board{i}' for i in range(250))))==250


def test_greenhouse_with_custom_company_url_is_not_discarded():
    job=parse_greenhouse({'id':123,'title':'Software Engineer Intern',
                          'absolute_url':'https://careers.example.com/jobs/123'}, 'krafton')
    assert job.original_url=='https://job-boards.greenhouse.io/krafton/jobs/123'


def test_cli_batches_only_one_kr_priority_company(tmp_path, monkeypatch):
    path=catalog(tmp_path,[
        {'provider':'greenhouse','id':'first','focus':'kr_it'},
        {'provider':'greenhouse','id':'second','focus':'global_it'},
        {'provider':'greenhouse','id':'third','focus':'kr_it'},
        {'provider':'greenhouse','id':'fourth','focus':'kr_it'},
    ])
    import app.cli as cli
    monkeypatch.setenv('PUBLIC_BOARD_CATALOG', str(path))
    received=[]
    async def fake_run(crawler, **kwargs):
        received.append((crawler.site_id,crawler.boards))
        class Result:
            status='completed'; seen_count=0; created_count=0; updated_count=0; failed_count=0
        return Result()
    monkeypatch.setattr(cli,'run_crawler',fake_run)
    for var in ('PUBLIC_GREENHOUSE_BOARDS','PUBLIC_ASHBY_BOARDS','PUBLIC_LEVER_SITES','PUBLIC_SMARTRECRUITERS_COMPANIES'):
        monkeypatch.delenv(var,raising=False)
    monkeypatch.setattr(sys,'argv',['cli','crawl-public','--catalog',str(path), '--focus','kr_it', '--batch-size','2', '--batch-index','1'])
    cli.main()
    assert received==[('public_greenhouse',['fourth'])]


def test_verify_only_disabled_and_focus_offline(tmp_path, monkeypatch):
    from app.verify_boards import verify
    path=catalog(tmp_path,[
        {'provider':'greenhouse','id':'one','enabled':True,'focus':'kr_it'},
        {'provider':'greenhouse','id':'two','enabled':False,'focus':'kr_it'},
        {'provider':'greenhouse','id':'three','enabled':False,'focus':'global_it'},
    ])
    monkeypatch.setenv('PUBLIC_BOARD_CATALOG',str(path))
    report=asyncio.run(verify(only_disabled=True,focus='kr_it',live=False))
    assert [row['id'] for row in report]==['two']
