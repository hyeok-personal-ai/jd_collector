from datetime import date,datetime,timezone
import pytest
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from app.database import Base,Job,PostingPresence
from app.schema import JobDraft
from app.repository import upsert_job
from app.runner import mark_detail_checked,needs_detail
from app.crawlers.base import JobRef
from app.deadline_policy import review_mode,expire_deadlines
from app.aggregate_store import AggregateStore
from app.lifecycle import mark_present


@pytest.fixture
def factory():
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    yield sessionmaker(engine,expire_on_commit=False)
    engine.dispose()


def draft(identifier='one',**fields):
    return JobDraft(source_id='demo',external_id=identifier,title='Engineer',
        original_url='https://example.org/jobs/'+identifier,source_status='open',**fields)


@pytest.mark.parametrize('body,text,deadline,expected',[
    ('지원 안내',None,date(2099,12,31),'fixed'),
    ('지원자가 충분하면 조기 마감될 수 있습니다.',None,date(2099,12,31),'flexible'),
    ('채용 시 마감',None,None,'flexible'),
    ('상시 채용 중입니다.',None,None,'flexible'),
    ('인프라 상시 운영 업무',None,date(2099,12,31),'fixed'),
    ('Applications may close early',None,date(2099,12,31),'flexible'),
    ('지원 안내','상시',None,'flexible'),
    ('지원 안내',None,None,'unknown'),
])
def test_review_policy_uses_deadline_and_explicit_recruiting_conditions(body,text,deadline,expected):
    assert review_mode(draft(body_text=body,deadline_text=text,deadline_on=deadline))==expected


def test_fixed_posting_ignores_elapsed_time_but_reads_explicit_revisions(factory):
    ref=JobRef('one','https://example.org/jobs/one',listing_fingerprint='v1')
    with factory() as db:
        upsert_job(db,draft(deadline_on=date(2099,12,31)))
        mark_detail_checked(db,'demo',ref,now=datetime(2000,1,1,tzinfo=timezone.utc));db.commit()
        now=datetime(2030,1,1,tzinfo=timezone.utc)
        assert not needs_detail(db,'demo',ref,now=now,recheck_hours=6)
        assert needs_detail(db,'demo',JobRef('one',ref.url,listing_fingerprint='v2'),now=now,recheck_hours=6)


def test_early_close_is_rechecked_even_with_a_fixed_date(factory):
    ref=JobRef('one','https://example.org/jobs/one')
    with factory() as db:
        upsert_job(db,draft(deadline_on=date(2099,12,31),body_text='충원 시 조기 마감 가능합니다.'))
        mark_detail_checked(db,'demo',ref,now=datetime(2000,1,1,tzinfo=timezone.utc));db.commit()
        assert needs_detail(db,'demo',ref,now=datetime(2030,1,1,tzinfo=timezone.utc),recheck_hours=6)


def test_deadline_closes_locally_after_the_day_and_preserves_saved_content(factory):
    with factory() as db:
        upsert_job(db,draft('expired',deadline_on=date(2030,1,1),body_text='원래 공고 본문'))
        upsert_job(db,draft('today',deadline_on=date(2030,1,2)))
        db.commit()
        assert expire_deadlines(db,today=date(2030,1,2))==1
        db.commit()
        jobs={j.external_id:j for j in db.scalars(select(Job))}
        assert jobs['expired'].source_status=='closed'
        assert jobs['expired'].body_text=='원래 공고 본문'
        assert jobs['today'].source_status=='open'
        assert expire_deadlines(db,today=date(2030,1,2))==0


def test_inventory_excludes_fixed_postings_but_keeps_new_and_flexible(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite');store.pages('s',['page'])
    store.save_page('s','page',[{'id':i,'url':'https://example.org/'+i,'revision':'v1'} for i in ('fixed','flexible','unknown','new')])
    for mode in ('fixed','flexible','unknown'):
        store.mark('s',mode,'done',active=True,recheck_mode=mode,deadline_on='2099-12-31' if mode=='fixed' else None)
    with store.db() as db:db.execute('UPDATE aggregate_refs SET checked=0')
    assert [r['id'] for r in store.due('s')]==['flexible','unknown','new']
    # A real source revision still creates fresh work, including a deadline extension.
    store.save_page('s','page',[{'id':'fixed','url':'https://example.org/fixed','revision':'v2'}])
    assert 'fixed' in {r['id'] for r in store.due('s')}
    store.mark('s','fixed','done',active=True,recheck_mode='fixed',deadline_on='2099-12-31')
    store.save_page('s','page',[{'id':'fixed','url':'https://example.org/updated','revision':'v2'}])
    assert 'fixed' in {r['id'] for r in store.due('s')}


def test_presence_does_not_reopen_a_posting_past_its_deadline(factory):
    with factory() as db:
        upsert_job(db,draft(deadline_on=date(2000,1,1)))
        db.commit();expire_deadlines(db);db.commit()
        db.add(PostingPresence(source_id='demo',external_id='one',scope_id='scope',closed_by_absence=True))
        db.commit();mark_present(db,'demo','one','scope');db.commit()
        assert db.scalar(select(Job)).source_status=='closed'
