from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import api
from app.aggregate_store import AggregateStore
from app.database import Base, get_session
from app.deadline_policy import today_kst
from app.repository import upsert_job
from app.schema import JobDraft


def test_progress_separates_open_unknown_and_historical_backlog(monkeypatch, tmp_path):
    monkeypatch.setenv('RADAR_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('RADAR_WORKER', raising=False)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    def session():
        with factory() as db:
            yield db
    monkeypatch.setattr(api, 'init_db', lambda: None)
    with factory() as db:
        for identifier, state, deadline in [('live','open',today_kst()),
                ('expired','open',today_kst()-timedelta(days=1)), ('unverified','unknown',None)]:
            upsert_job(db, JobDraft(source_id='inthiswork', external_id=identifier, title=identifier,
                original_url='https://inthiswork.com/'+identifier, source_status=state, deadline_on=deadline))
        db.commit()
    store = AggregateStore()
    store.pages('inthiswork', ['page'])
    store.save_page('inthiswork', 'page', [{'id':i,'url':'https://inthiswork.com/'+i}
                                         for i in ('live','expired','unverified')])
    for identifier in ('live','expired','unverified'):
        store.mark('inthiswork', identifier, 'done')
    store.meta('inthiswork', 'expected', 3)
    import time
    store.meta('inthiswork','last_scan',time.time())
    store.pages('zighang', ['history'])
    store.save_page('zighang', 'history', [{'id':'old','url':'https://zighang.com/recruitment/old'}])
    api.app.dependency_overrides[get_session] = session
    try:
        with TestClient(api.app) as client:
            response = client.get('/api/aggregate-status')
        assert response.status_code == 200
        wp, zighang = response.json()['items']
        assert wp['saved'] == {'open':1, 'closed':1, 'unknown':1, 'total':3}
        assert wp['remaining_open'] == 0
        assert zighang['inventory']['pending'] == 1
        assert zighang['remaining_open'] is None
        assert zighang['saved']['open'] == 0
    finally:
        api.app.dependency_overrides.clear()
        engine.dispose()
