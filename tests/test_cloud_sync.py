import copy
import gzip
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import cloud
from app.database import Base, Job, ListingState, utcnow
from app.schema import JobDraft
from app.repository import upsert_job

ID = '00000000-0000-0000-0000-000000000001'


def document(**changes):
    draft = JobDraft(source_id='zighang', external_id=ID, title='백엔드 개발자',
        original_url='https://www.zighang.com/recruitment/' + ID,
        body_text='Python API 개발 업무', source_status='open', **changes)
    return {'version': 1, 'generated_at': utcnow().isoformat(), 'jobs': [
        {'job': draft.model_dump(mode='json'), 'checked_at': (utcnow() - timedelta(minutes=1)).isoformat(), 'revision': 'r1'}],
        'inventory': {}, 'source_status': {}}


@pytest.fixture
def factory(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'local.sqlite3'))
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_import_is_idempotent_and_updates_existing_id(factory):
    doc = document()
    assert cloud.merge_jobs(doc, factory)['created'] == 1
    with factory() as db:
        original_id = db.scalar(select(Job)).id
    assert cloud.merge_jobs(doc, factory)['skipped_newer_local'] == 1
    doc['jobs'][0]['checked_at'] = utcnow().isoformat()
    doc['jobs'][0]['job']['source_status'] = 'closed'
    assert cloud.merge_jobs(doc, factory)['updated'] == 1
    with factory() as db:
        job = db.scalar(select(Job))
        assert job.id == original_id  # Control DB bookmarks continue to reference this row.
        assert job.source_status == 'closed'
        assert db.scalar(select(ListingState)).listing_fingerprint == 'r1'


def test_old_snapshot_cannot_overwrite_newer_local_status(factory):
    doc = document()
    draft = JobDraft.model_validate(doc['jobs'][0]['job']).model_copy(update={'source_status': 'closed'})
    with factory() as db:
        upsert_job(db, draft)
        db.commit()
    assert cloud.merge_jobs(doc, factory)['skipped_newer_local'] == 1
    with factory() as db:
        assert db.scalar(select(Job)).source_status == 'closed'


def test_import_applies_local_deadline_expiry(factory):
    doc = document(deadline_on='2000-01-01')
    cloud.merge_jobs(doc, factory)
    with factory() as db:
        assert db.scalar(select(Job)).source_status == 'closed'


def test_entire_batch_validated_before_writing(factory):
    doc = document()
    bad = copy.deepcopy(doc['jobs'][0])
    bad['job']['source_id'] = 'untrusted'
    doc['jobs'].append(bad)
    with pytest.raises(ValueError):
        cloud.merge_jobs(doc, factory)
    with factory() as db:
        assert db.scalar(select(Job)) is None


def test_rejects_duplicate_identity_future_time_and_bad_version():
    doc = document()
    assert cloud.decode_snapshot(cloud.encode_snapshot(doc))['jobs'] == doc['jobs']
    doc['jobs'].append(copy.deepcopy(doc['jobs'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        cloud.decode_snapshot(cloud.encode_snapshot(doc))
    doc = document()
    doc['generated_at'] = (utcnow() + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        cloud.decode_snapshot(cloud.encode_snapshot(doc))
    doc = document()
    doc['version'] = 2
    with pytest.raises(ValueError):
        cloud.decode_snapshot(cloud.encode_snapshot(doc))


def test_bounded_decompression_and_untrusted_links(monkeypatch):
    monkeypatch.setattr(cloud, 'MAX_BYTES', 50)
    with pytest.raises(ValueError, match='Oversized'):
        cloud.decode_snapshot(gzip.compress(b'x' * 1000))
    row = document()['jobs'][0]
    row['job']['apply_url'] = 'javascript:alert(1)'
    with pytest.raises(ValueError):
        cloud.validate_job(row)


def test_public_repository_is_rejected_before_any_data_upload(monkeypatch):
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        return json.dumps({'private': False, 'visibility': 'public'})
    monkeypatch.setattr(cloud, 'gh', fake)
    with pytest.raises(PermissionError):
        cloud.publish_snapshot('owner/public-example', document())
    assert calls == [('api', 'repos/owner/public-example')]


def test_remote_hash_mismatch_is_rejected(monkeypatch):
    import base64
    def fake(*args, **kwargs):
        if args[1] == 'repos/owner/private-example':
            return json.dumps({'private': True, 'visibility': 'private'})
        return json.dumps({'sha': '0' * 40, 'encoding': 'base64', 'content': base64.b64encode(b'wrong').decode()})
    monkeypatch.setattr(cloud, 'gh', fake)
    with pytest.raises(ValueError, match='integrity'):
        cloud.remote_snapshot('owner/private-example')


def test_github_error_does_not_reveal_command_output(monkeypatch):
    monkeypatch.setattr(cloud.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout='confidential content', stderr='private response'))
    with pytest.raises(RuntimeError) as error:
        cloud.gh('api', 'example')
    assert 'confidential' not in str(error.value)
    assert 'private response' not in str(error.value)


def test_snapshot_export_omits_non_development_and_unselected_backlog(factory, tmp_path):
    from app.classification_store import update_classification
    from app.aggregate_store import AggregateStore
    doc = document()
    cloud.merge_jobs(doc, factory)
    with factory() as db:
        draft = JobDraft(source_id='inthiswork', external_id='2', title='영업 담당자',
                         original_url='https://inthiswork.com/archives/2')
        upsert_job(db, draft)
        update_classification(db, draft)
        db.commit()
    store = AggregateStore(tmp_path / 'inventory.sqlite3')
    store.add_focus('zighang', [{'id': ID, 'url': doc['jobs'][0]['job']['original_url']}])
    store.pages('zighang', ['https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml'])
    store.save_page('zighang', 'https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml',
                    [{'id': 'deferred', 'url': 'https://example.org/deferred'}])
    exported = cloud.export_snapshot(factory, store)
    assert len(exported['jobs']) == 1
    assert [r['id'] for r in exported['inventory']['aggregate_refs']] == [ID]
    assert exported['inventory']['aggregate_pages'] == []


def test_unconfigured_sync_performs_no_network(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(cloud, 'data_dir', lambda: tmp_path)
    monkeypatch.setattr(cloud, 'gh', lambda *a, **kw: pytest.fail('Unexpected network'))
    assert asyncio.run(cloud.sync_once()) == {'configured': False}


def test_auto_sync_retries_after_configuration_failure(monkeypatch):
    import asyncio
    calls = []
    async def broken():
        calls.append('attempt')
        raise ValueError('Invalid local configuration')
    async def pause(_seconds):
        calls.append('retry scheduled')
        raise asyncio.CancelledError
    monkeypatch.setattr(cloud, 'sync_once', broken)
    monkeypatch.setattr(cloud.asyncio, 'sleep', pause)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cloud.sync_loop())
    assert calls == ['attempt', 'retry scheduled']


def test_wp_cloud_filter_preserves_pending_progress_without_non_dev_bodies(factory, tmp_path):
    import asyncio
    from app.aggregate_store import AggregateStore
    def post(identifier, title):
        return {'id': identifier, 'link': f'https://inthiswork.com/archives/{identifier}', 'status': 'publish',
                'date': '2026-10-01T12:00:00', 'modified': '2026-10-01T12:00:00',
                'title': {'rendered': title}, 'categories': [191700167],
                'content': {'rendered': '<p>Synthetic posting body</p>'}}
    rows = [post(1, '기업｜백엔드 개발자'), post(2, '기업｜영업 담당자'), post(3, '기업｜다른 직무로 변경')]
    requests = []
    class Fetcher:
        async def get_response(self, url):
            requests.append(url)
            return SimpleNamespace(text=json.dumps(rows), total=3, total_pages=1)
    store = AggregateStore(tmp_path / 'cloud-inventory.sqlite3')
    async def scan(s):
        return [r async for r in cloud.DevelopmentInThisWorkCrawler(s, known_ids=['3']).discover(Fetcher())]
    assert [r.external_id for r in asyncio.run(scan(store))] == ['1', '3']
    # Simulate the execution time limit before selected bodies were saved.
    snapshot = cloud.export_snapshot(factory, store)
    refs = {r['id']: r for r in snapshot['inventory']['aggregate_refs']}
    assert refs['1']['state'] == 'pending' and refs['1']['payload']
    assert refs['2']['recheck_mode'] == 'ignored' and refs['2']['payload'] is None
    restored = AggregateStore(tmp_path / 'next-run.sqlite3')
    cloud.restore_inventory(snapshot, restored)
    requests.clear()
    assert [r.external_id for r in asyncio.run(scan(restored))] == ['1', '3']
    assert len(requests) == 1
    assert restored.meta('inthiswork', 'scanned_unique_count') == 3
