"""Private GitHub state transport. Credentials stay in gh/keyring or GITHUB_TOKEN."""
import argparse
import asyncio
import base64
import gzip
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from .aggregate_store import AggregateStore
from .classification_store import update_classification
from .control import data_dir
from .database import Job, JobClassification, ListingState, SessionLocal, init_db, utcnow
from .deadline_policy import expire_deadlines
from .job_labels import label_job
from .locking import FileLock, writer_guard
from .repository import save_raw_posting, upsert_job
from .runner import mark_detail_checked, run_crawler, _utc
from .schema import JobDraft
from .crawlers.aggregators import InThisWorkPublicCrawler

MAX_BYTES = 100_000_000
STATE_PATH = 'snapshot.json.gz'
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')


class DevelopmentInThisWorkCrawler(InThisWorkPublicCrawler):
    """Filter public list titles before body persistence, while tracking known revisions."""
    def __init__(self, store=None, known_ids=()):
        super().__init__(store)
        self.known_ids = set(known_ids)

    async def discover(self, fetcher):
        from bs4 import BeautifulSoup
        async for ref in super().discover(fetcher):
            row = (ref.payload or {}).get('draft') or {}
            title = BeautifulSoup(row.get('title', {}).get('rendered', ''), 'html.parser').get_text(' ', strip=True)
            selected = ref.external_id in self.known_ids or (title and label_job(JobDraft(
                source_id=self.site_id, external_id=ref.external_id, original_url=ref.url, title=title)).it_match == 'yes')
            if selected:
                yield ref
            else:
                self.store.mark(self.site_id, ref.external_id, 'done', active=False, recheck_mode='ignored')


def gh(*args, payload=None):
    result = subprocess.run(['gh', *args], input=json.dumps(payload) if payload is not None else None,
                            text=True, encoding='utf-8', capture_output=True, timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        # Never propagate authenticated command output or source content to the UI/log.
        raise RuntimeError('GitHub request failed; check gh login and repository permission')
    return result.stdout


def require_private(repo):
    if not REPO.fullmatch(repo):
        raise ValueError('Invalid repository name')
    info = json.loads(gh('api', f'repos/{repo}'))
    if info.get('private') is not True or info.get('visibility') != 'private':
        raise PermissionError('Cloud collection and data transfer require a private repository')


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed > utcnow() + timedelta(minutes=5):
        raise ValueError('Invalid source observation time')
    return parsed


def validate_job(row):
    draft = JobDraft.model_validate(row['job'])
    host = urlsplit(draft.original_url).hostname
    path = urlsplit(draft.original_url).path
    if draft.source_id == 'zighang':
        valid = (host in ('zighang.com', 'www.zighang.com') and
                 re.fullmatch(r'[0-9a-f-]{36}', draft.external_id) and path == '/recruitment/' + draft.external_id)
    elif draft.source_id == 'inthiswork':
        valid = host == 'inthiswork.com' and draft.external_id.isdigit() and path.rstrip('/') == '/archives/' + draft.external_id
    else:
        valid = False
    if not valid or len(json.dumps(row, ensure_ascii=False)) > 1_000_000:
        raise ValueError('Invalid cloud posting identity or size')
    for url in [draft.original_url, draft.apply_url, *(a.url for a in draft.attachments)]:
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Invalid posting link')
    return draft, stamp(row['checked_at'])


def decode_snapshot(blob):
    if len(blob) > 25_000_000:
        raise ValueError('Oversized compressed snapshot')
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Oversized snapshot')
    doc = json.loads(raw)
    if doc.get('version') != 1 or not isinstance(doc.get('jobs'), list) or len(doc['jobs']) > 50_000:
        raise ValueError('Unsupported cloud snapshot')
    stamp(doc['generated_at'])
    seen = set()
    for row in doc['jobs']:
        draft, _ = validate_job(row)
        key = (draft.source_id, draft.external_id)
        if key in seen:
            raise ValueError('Duplicate cloud posting identity')
        seen.add(key)
    return doc


def export_snapshot(factory=SessionLocal, store=None, statuses=None, *, include_tracked=False):
    store = store or AggregateStore()
    jobs = []
    with factory() as db:
        query = select(Job).where(Job.source_id.in_(['zighang', 'inthiswork']))
        if not include_tracked:
            query = query.join(JobClassification, (Job.source_id == JobClassification.source_id) &
                (Job.external_id == JobClassification.external_id)).where(JobClassification.it_match == 'yes')
        for job in db.scalars(query):
            state = db.scalar(select(ListingState).where(ListingState.source_id == job.source_id,
                                                        ListingState.external_id == job.external_id))
            checked = max(_utc(job.updated_at), _utc(state.last_detail_checked_at)) if state else _utc(job.updated_at)
            draft = JobDraft.model_validate({k: getattr(job, k) for k in JobDraft.model_fields})
            jobs.append({'job': draft.model_dump(mode='json'), 'checked_at': checked.isoformat(),
                         'revision': state.listing_fingerprint if state else None})
    keys = {(j['job']['source_id'], j['job']['external_id']) for j in jobs}
    with store.db() as db:
        focus = [dict(r) for r in db.execute("SELECT * FROM aggregate_focus WHERE source='zighang' AND focus='development'")]
        focus_keys = {(r['source'], r['id']) for r in focus}
        refs = [dict(r) for r in db.execute("SELECT r.* FROM aggregate_refs r WHERE source='inthiswork' OR (source='zighang' AND EXISTS (SELECT 1 FROM aggregate_focus f WHERE f.source=r.source AND f.id=r.id AND f.focus='development'))")
                if r['source'] == 'inthiswork' or (r['source'], r['id']) in keys | focus_keys]
        for row in refs:
            if row['source'] == 'inthiswork' and row['recheck_mode'] == 'ignored':
                row['payload'] = None  # Preserve scan position without retaining non-development bodies.
        pages = [dict(r) for r in db.execute("SELECT * FROM aggregate_pages WHERE source IN ('zighang_company','inthiswork')")]
        meta = [dict(r) for r in db.execute("SELECT * FROM aggregate_meta WHERE source='inthiswork'")]
    return {'version': 1, 'generated_at': utcnow().isoformat(), 'jobs': jobs,
            'inventory': {'aggregate_refs': refs, 'aggregate_focus': focus, 'aggregate_pages': pages, 'aggregate_meta': meta},
            'source_status': statuses or {}}


def encode_snapshot(doc):
    raw = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Snapshot exceeds state budget')
    return gzip.compress(raw, mtime=0)


def merge_jobs(doc, factory=SessionLocal):
    # Validate the entire batch before opening a write transaction.
    parsed = [(row, *validate_job(row)) for row in doc['jobs']]
    counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'skipped_newer_local': 0, 'skipped_non_development': 0}
    with factory() as db:
        for row, draft, checked in parsed:
            old = db.scalar(select(Job).where(Job.source_id == draft.source_id, Job.external_id == draft.external_id))
            state = db.scalar(select(ListingState).where(ListingState.source_id == draft.source_id,
                                                        ListingState.external_id == draft.external_id))
            fresh = max(_utc(old.updated_at), _utc(state.last_detail_checked_at)) if old and state else (
                _utc(old.updated_at) if old else None)
            if fresh and fresh >= checked:
                counts['skipped_newer_local'] += 1
                continue
            if not old and label_job(draft).it_match != 'yes':
                counts['skipped_non_development'] += 1
                continue
            counts[upsert_job(db, draft)] += 1
            save_raw_posting(db, draft)
            update_classification(db, draft)
            from .crawlers.base import JobRef
            mark_detail_checked(db, draft.source_id, JobRef(draft.external_id, draft.original_url,
                                listing_fingerprint=row.get('revision')), now=checked)
            db.flush()
            saved = db.scalar(select(Job).where(Job.source_id == draft.source_id, Job.external_id == draft.external_id))
            # Import time is not evidence that the source was checked again.
            saved.updated_at = checked
            saved.last_seen_at = checked
        expire_deadlines(db)
        db.commit()
    return counts


def restore_inventory(doc, store):
    # Used ONLY by the isolated cloud process, never to replace local queue/preferences.
    with store.db() as db:
        for table in ('aggregate_refs', 'aggregate_focus', 'aggregate_pages', 'aggregate_meta'):
            columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})')]
            for row in doc.get('inventory', {}).get(table, []):
                if row.get('source') not in ('zighang', 'inthiswork', 'zighang_company'):
                    raise ValueError('Invalid cloud inventory source')
                names = [c for c in columns if c in row]
                db.execute(f"INSERT OR REPLACE INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})",
                           [row[c] for c in names])
    store.meta('zighang', 'collection_mode', 'development')


def remote_snapshot(repo, previous=None):
    require_private(repo)
    meta = json.loads(gh('api', f'repos/{repo}/contents/{STATE_PATH}?ref=state'))
    if meta['sha'] == previous:
        return meta['sha'], None
    if meta.get('size', 0) > 25_000_000:
        raise ValueError('Oversized remote state')
    if meta.get('encoding') != 'base64':
        meta = json.loads(gh('api', f'repos/{repo}/git/blobs/{meta["sha"]}'))
    blob = base64.b64decode(meta['content'])
    # Git's blob hash independently checks the downloaded binary content.
    digest = hashlib.sha1(b'blob ' + str(len(blob)).encode() + b'\0' + blob).hexdigest()
    if digest != meta['sha']:
        raise ValueError('Cloud blob integrity mismatch')
    return meta['sha'], decode_snapshot(blob)


def publish_snapshot(repo, doc, *, initial=False):
    require_private(repo)  # Fail closed if someone changed repository visibility.
    payload = {'message': 'Update private collector snapshot', 'branch': 'state',
               'content': base64.b64encode(encode_snapshot(doc)).decode('ascii')}
    if not initial:
        meta = json.loads(gh('api', f'repos/{repo}/contents/{STATE_PATH}?ref=state'))
        payload['sha'] = meta['sha']
    # Contents API uses an expected blob SHA. A concurrent update is rejected, never forced.
    result = json.loads(gh('api', '--method', 'PUT', f'repos/{repo}/contents/{STATE_PATH}', '--input', '-', payload=payload))
    return result['content']['sha']


def sync_status():
    config = data_dir() / 'cloud-config.json'
    status = data_dir() / 'cloud-sync.json'
    return {'configured': config.exists(), **(json.loads(status.read_text(encoding='utf-8')) if status.exists() else {})}


async def sync_once():
    config_path = data_dir() / 'cloud-config.json'
    if not config_path.exists():
        return {'configured': False}
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if not config.get('enabled'):
        return {'configured': True, 'status': 'disabled'}
    lock = FileLock(data_dir() / 'cloud-sync.lock')
    if not lock.acquire():
        return {'configured': True, 'status': 'busy'}
    status_path = data_dir() / 'cloud-sync.json'
    previous = sync_status()
    result = {**previous, 'configured': True, 'checked_at': utcnow().isoformat()}
    try:
        sha, doc = await asyncio.to_thread(remote_snapshot, config['repository'], previous.get('sha'))
        if doc:
            async with writer_guard(SessionLocal):
                counts = merge_jobs(doc)
            result.update(status='synced', sha=sha, imported_at=utcnow().isoformat(), counts=counts,
                          source_status=doc.get('source_status', {}), cloud_generated_at=doc['generated_at'])
        else:
            result['status'] = 'current'
        result.pop('error', None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.update(status='error', error=type(exc).__name__ + ': private cloud sync unavailable; check gh login/permission')
    finally:
        temp = status_path.with_suffix('.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temp.replace(status_path)
        lock.release()
    return result


async def sync_loop():
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A broken local config must not permanently stop future sync attempts.
            logging.getLogger(__name__).warning('Private cloud sync configuration unavailable')
        await asyncio.sleep(300)


async def collect_cloud(repo, budget=600):
    require_private(repo)
    init_db()
    _, doc = await asyncio.to_thread(remote_snapshot, repo)
    merge_jobs(doc)
    store = AggregateStore()
    restore_inventory(doc, store)
    from .crawlers.aggregators import ZighangPublicCrawler
    from .crawlers.zighang_public import COMPANY_SOURCE
    statuses = {}
    deadline = time.monotonic() + budget
    with SessionLocal() as db:
        known_wp = list(db.scalars(select(Job.external_id).where(Job.source_id == 'inthiswork')))
    for source, cls, limit in [('inthiswork', DevelopmentInThisWorkCrawler, 5000), ('zighang', ZighangPublicCrawler, 100)]:
        try:
            while time.monotonic() < deadline:
                allowance = min(180, deadline - time.monotonic()) if source == 'inthiswork' else deadline - time.monotonic()
                async with asyncio.timeout(max(1, allowance)):
                    crawler = cls(store, known_ids=known_wp) if source == 'inthiswork' else cls(store)
                    run = await run_crawler(crawler, max_items=limit, recheck_hours=6)
                statuses[source] = {'status': run.status, 'seen': run.seen_count,
                                    'created': run.created_count, 'updated': run.updated_count,
                                    'checked_at': utcnow().isoformat()}
                if run.status not in ('completed', 'limited'):
                    from .service import failure_status
                    statuses[source]['status'] = failure_status(run.error_summary or '')
                    statuses[source]['reason'] = 'source_access_or_parse_failure'
                    break
                if source != 'zighang' or not (store.due_discovery_pages(COMPANY_SOURCE, 1) or store.due('zighang', 1, focus='development')):
                    break
        except TimeoutError:
            statuses[source] = {'status': 'limited', 'reason': 'time_budget', 'checked_at': utcnow().isoformat()}
            if time.monotonic() >= deadline:
                break
    snapshot = export_snapshot(store=store, statuses=statuses, include_tracked=True)
    await asyncio.to_thread(publish_snapshot, repo, snapshot)
    print(json.dumps({'private_state_saved': True, 'development_jobs': len(snapshot['jobs']), 'sources': statuses}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['bootstrap', 'run', 'sync'])
    parser.add_argument('--repository')
    parser.add_argument('--budget', type=int, default=600)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.command == 'bootstrap':
        doc = export_snapshot()
        # First cloud run must verify source state instead of trusting a local check's age.
        for page in doc['inventory']['aggregate_pages']:
            page.update(checked=0)
        publish_snapshot(args.repository, doc, initial=True)
        print(json.dumps({'private_state_saved': True, 'development_jobs': len(doc['jobs'])}))
    elif args.command == 'run':
        asyncio.run(collect_cloud(args.repository, max(30, min(args.budget, 900))))
    else:
        init_db()
        print(json.dumps(asyncio.run(sync_once())))


if __name__ == '__main__':
    main()
