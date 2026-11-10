import asyncio
from types import SimpleNamespace

import pytest

from app import cloud
from app.aggregate_store import AggregateStore
from app.database import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.parametrize('first_result', ['timeout', 'exception', 'completed'])
def test_two_sources_run_and_state_is_saved_after_each_failure(tmp_path, monkeypatch, first_result):
    engine = create_engine('sqlite:///' + str(tmp_path/'test.sqlite3'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    store = AggregateStore(tmp_path/'inventory.sqlite3')
    calls, published = [], []
    monkeypatch.setattr(cloud, 'require_private', lambda _: None)
    monkeypatch.setattr(cloud, 'init_db', lambda: None)
    monkeypatch.setattr(cloud, 'remote_snapshot', lambda _: ('sha', {}))
    monkeypatch.setattr(cloud, 'merge_jobs', lambda _: None)
    monkeypatch.setattr(cloud, 'SessionLocal', factory)
    monkeypatch.setattr(cloud, 'AggregateStore', lambda: store)
    monkeypatch.setattr(cloud, 'export_snapshot', lambda **kw: {'jobs':[], 'statuses':kw['statuses']})
    monkeypatch.setattr(cloud, 'publish_snapshot', lambda repo, doc: published.append(doc))

    async def run(crawler, **kwargs):
        calls.append(crawler.site_id)
        if crawler.site_id == 'inthiswork':
            if first_result == 'timeout':
                await asyncio.sleep(10)
            if first_result == 'exception':
                raise RuntimeError('Synthetic source failure')
        return SimpleNamespace(status='completed', seen_count=1, created_count=1, updated_count=0)

    monkeypatch.setattr(cloud, 'run_crawler', run)
    asyncio.run(cloud.collect_cloud('owner/private', budget=2))
    assert calls == ['inthiswork', 'zighang']
    statuses = published[0]['statuses']
    assert statuses['inthiswork']['status'] == {'timeout':'limited','exception':'failed','completed':'completed'}[first_result]
    assert statuses['zighang']['status'] == 'completed'
    engine.dispose()


def test_pending_count_has_no_display_cap(tmp_path):
    from app.crawlers.zighang_recent import recent_summary
    store = AggregateStore(tmp_path/'inventory.sqlite3')
    with store.db() as db:
        db.executemany("INSERT INTO aggregate_refs(source,id,url,state) VALUES ('zighang_recent',?,'https://example.org','pending')",
                       ((str(i),) for i in range(100001)))
    assert recent_summary(store)['eligible_pending'] == 100001
