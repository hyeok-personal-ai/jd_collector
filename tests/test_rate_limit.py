import asyncio
import time
from email.utils import formatdate
import httpx
import pytest
from app.aggregate_store import AggregateStore
from app.rate_limit import record_cooldown, waiting_status, stop_on_rate_limit, KEY


def test_cooldown_backoff_retry_after_and_expiry(tmp_path):
    store=AggregateStore(tmp_path/'state.sqlite')
    first=record_cooldown(store,'inthiswork',now=1000)
    assert first['until']==1000+21600
    assert waiting_status(store,'inthiswork',now=1001)['status']=='cooldown'
    assert waiting_status(store,'inthiswork',now=first['until']) is None
    second=record_cooldown(store,'inthiswork','90000',now=first['until'])
    assert second['strikes']==2 and second['until']==first['until']+90000
    future=formatdate(second['until']+200000,usegmt=True)
    third=record_cooldown(store,'inthiswork',future,now=second['until'])
    assert third['until']==second['until']+200000
    assert waiting_status(store,'zighang',now=1001) is None


def test_429_stops_without_immediate_retries(tmp_path):
    store=AggregateStore(tmp_path/'state.sqlite')
    calls=[]
    async def scenario():
        async def transport(request):
            calls.append(str(request.url))
            return httpx.Response(429,headers={'Retry-After':'7200'})
        async def hook(response):await stop_on_rate_limit(response,store,'inthiswork')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport),event_hooks={'response':[hook]}) as client:
            with pytest.raises(RuntimeError,match='HTTP 429'):await client.get('https://example.org/list?page=17')
    asyncio.run(scenario())
    assert len(calls)==1
    assert waiting_status(store,'inthiswork')['resume_after']


def test_cloud_snapshot_round_trip_retains_cursor_and_cooldown(tmp_path):
    from app.cloud import export_snapshot, restore_inventory
    from app.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    store=AggregateStore(tmp_path/'first.sqlite')
    store.pages('inthiswork',['page17','page18'])
    store.mark_discovery_page('inthiswork','page17',state='done')
    for source in ('inthiswork','zighang'):record_cooldown(store,source)
    doc=export_snapshot(sessionmaker(engine),store)
    restored=AggregateStore(tmp_path/'next.sqlite')
    restore_inventory(doc,restored)
    assert restored.pending_pages('inthiswork')==['page18']
    for source in ('inthiswork','zighang'):assert waiting_status(restored,source)
    engine.dispose()
