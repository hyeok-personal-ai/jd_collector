"""Single-worker durable queue, automatically resumed by the local API lifespan."""
import asyncio
import logging
import time
from .control import ControlStore, data_dir
from .locking import FileLock
from .runner import run_crawler
from .deadline_policy import expire_local_deadlines

log = logging.getLogger(__name__)


async def collect_task(task, settings):
    spec = task['spec']
    if spec['kind'] == 'aggregate':
        from .crawlers.registry import CRAWLERS
        crawler=CRAWLERS[spec['source']]()
    elif spec['kind'] == 'public':
        from .crawlers.registry import CRAWLERS
        crawler = CRAWLERS['public_' + spec['provider']]()
        crawler.boards = [spec['board']]
    else:
        from .crawlers.company_public import CompanyPublicCrawler
        crawler = CompanyPublicCrawler(spec['company'], render=settings['render_js'])
    result=await run_crawler(crawler, max_items=5000 if spec['kind']=='aggregate' and spec['source']=='inthiswork' else settings['max_items'],
        **({'recheck_hours':6} if spec['kind']=='aggregate' else {}))
    if spec['kind']=='aggregate' and result.status=='completed':
        summary=crawler.store.summary(crawler.site_id)
        focused = spec['source']=='zighang' and crawler.store.meta('zighang','collection_mode')=='development'
        remaining = crawler.store.focus_summary('zighang').get('pending',0) if focused else (
            summary['pages'].get('pending') or summary['inventory'].get('pending'))
        if focused:
            from .crawlers.zighang_public import COMPANY_SOURCE
            from .crawlers.zighang_recent import pending_recent
            remaining=remaining or bool(pending_recent(crawler.store,1) or crawler.store.due_discovery_pages(COMPANY_SOURCE,1))
        if remaining:
            from .database import SessionLocal,CrawlRun
            with SessionLocal() as db:
                record=db.get(CrawlRun,result.id);record.status='limited'
                record.error_summary='Public inventory has remaining work; next batch scheduled'
                db.commit()
            result.status='limited';result.error_summary='Public inventory has remaining work; next batch scheduled'
    return result


def failure_status(error):
    text = error.lower()
    if any(w in text for w in ('robots', 'permissionerror', 'http 403', 'http 401', 'http 429')):
        return 'blocked'
    if any(w in text for w in ('not recognized', 'no recognized', 'layout', 'no validated')):
        return 'needs_review'
    return 'failed'


async def recover_runs():
    from sqlalchemy import update
    from .database import SessionLocal, CrawlRun, init_db, utcnow
    from .locking import writer_guard
    async with writer_guard(SessionLocal):
        init_db()
        with SessionLocal() as session:
            session.execute(update(CrawlRun).where(CrawlRun.status == 'running').values(
                status='interrupted', finished_at=utcnow(),
                error_summary='Previous process stopped; durable queue recovered at startup'))
            session.commit()


async def serve_queue(store=None):
    store = store or ControlStore()
    lock = FileLock(store.path.with_suffix('.worker.lock'))
    if not lock.acquire():
        log.warning('Another collection worker already owns this data directory')
        return
    current = None
    try:
        store.sync_catalog()
        store.recover()
        await recover_runs()
        last_schedule = 0
        last_local_check = 0
        while True:
            store.heartbeat()
            if time.monotonic() - last_local_check >= 60:
                expire_local_deadlines()
                last_local_check = time.monotonic()
            settings = store.settings()
            if settings['auto_collect'] and time.monotonic() - last_schedule >= 60:
                store.enqueue(due_only=True)
                last_schedule = time.monotonic()
            current = store.claim()
            if current is None:
                await asyncio.sleep(2)
                continue
            task = asyncio.create_task(collect_task(current, settings))
            try:
                deadline = time.monotonic() + 600
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=5)
                    store.heartbeat()
                    if not done and time.monotonic() > deadline:
                        raise TimeoutError('Collection exceeded 10 minute limit')
                result = await task
                status = result.status
                if status not in ('completed', 'limited'):
                    status = 'partial' if result.seen_count else failure_status(result.error_summary or '')
                store.finish(current, status=status, seen=result.seen_count, created=result.created_count,
                             updated=result.updated_count, error=result.error_summary or '', run_id=result.id)
                if status == 'limited' and store.settings()['auto_collect']:
                    store.enqueue([current['target_key']])
            except asyncio.CancelledError:
                store.finish(current, status='interrupted', error='Worker stopped; retry available')
                raise
            except Exception as exc:
                message = f'{type(exc).__name__}: {str(exc)[:800]}'
                store.finish(current, status=failure_status(message), error=message)
                log.exception('Collection failed: %s', current['target_key'])
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            current = None
    finally:
        lock.release()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve_queue())
