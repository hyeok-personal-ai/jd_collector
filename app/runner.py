"""Per-source runner: incremental detail checks, optional cached LLM, isolated failures."""
import logging
import os
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import CrawlRun, Job, ListingState, SessionLocal, init_db, utcnow
from .crawlers.base import BaseCrawler, JobRef, PostingUnavailable
from .fetcher import PoliteFetcher
from .incremental import ListingCache
from .access_policy import require_access
from .quotas import ApiQuota
from .normalization import normalize_draft
from .repository import upsert_job, save_raw_posting, save_raw_html
from .classification_store import update_classification
from .lifecycle import mark_present, reconcile_scope
from .deadline_policy import review_mode, expire_deadlines

log = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    # SQLite may deserialize a timezone-aware timestamp without tzinfo.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def needs_detail(session: Session, site_id: str, ref: JobRef, *, now: datetime, recheck_hours: float) -> bool:
    """Avoid refetches within a window, but recheck and pick up explicit revisions."""
    job = session.scalar(select(Job).where(Job.source_id == site_id, Job.external_id == ref.external_id))
    state = session.scalar(select(ListingState).where(ListingState.source_id == site_id,
                                                      ListingState.external_id == ref.external_id))
    if job is None or state is None:
        return True
    if job.original_url != ref.url:
        return True
    if ref.listing_fingerprint is not None and ref.listing_fingerprint != state.listing_fingerprint:
        return True
    # A changed URL or advertised revision above remains fresh evidence to read.
    # Elapsed time alone never reopens a fixed-deadline posting's detail page.
    if review_mode(job) in ('fixed', 'closed'):
        return False
    if _utc(state.last_detail_checked_at) + timedelta(hours=recheck_hours) <= now:
        return True
    job.last_seen_at = now
    return False


def mark_detail_checked(session: Session, site_id: str, ref: JobRef, *, now: datetime) -> None:
    state = session.scalar(select(ListingState).where(ListingState.source_id == site_id,
                                                     ListingState.external_id == ref.external_id))
    if state is None:
        session.add(ListingState(source_id=site_id, external_id=ref.external_id,
                                 listing_fingerprint=ref.listing_fingerprint,
                                 last_detail_checked_at=now))
    else:
        state.listing_fingerprint = ref.listing_fingerprint
        state.last_detail_checked_at = now


async def run_crawler(crawler: BaseCrawler, **kwargs) -> CrawlRun:
    from .locking import writer_guard
    factory = kwargs.get('session_factory', SessionLocal)
    async with writer_guard(factory):
        try:
            return await _run_crawler(crawler, **kwargs)
        except BaseException:
            from sqlalchemy import update
            try:
                with factory() as session:
                    session.execute(update(CrawlRun).where(CrawlRun.source_id == crawler.site_id,
                        CrawlRun.status == 'running').values(status='interrupted', finished_at=utcnow(),
                        error_summary='Collection interrupted before completion'))
                    session.commit()
            except Exception:
                log.exception('Could not record interrupted run')
            raise


async def _run_crawler(crawler: BaseCrawler, *, max_items: int | None = None,
                      session_factory: Callable[[], Session] = SessionLocal,
                      recheck_hours: float | None = None,
                      fetcher_factory: Callable[[frozenset[str]], PoliteFetcher] | None = None,
                      init_database: bool = True) -> CrawlRun:
    require_access(crawler.site_id)
    if init_database:
        init_db()
    recheck_hours = float(os.getenv("DETAIL_RECHECK_HOURS", "24")) if recheck_hours is None else recheck_hours
    if recheck_hours < 0:
        raise ValueError("recheck_hours must be nonnegative")
    with session_factory() as session:
        expire_deadlines(session)
        run = CrawlRun(source_id=crawler.site_id, started_at=utcnow())
        session.add(run)
        session.commit()
        run_id = run.id

    # Inject a source-neutral listing cache; offline demo simply ignores it.
    crawler.listing_cache = ListingCache(session_factory)
    crawler.api_quota = ApiQuota(session_factory)
    status = "completed"
    error_summary = None
    observed: set[str] = set()
    successful: dict[str, set[str]] = {}
    detail_attempts = 0
    consecutive_failures = 0
    async with (fetcher_factory or PoliteFetcher)(crawler.allowed_hosts) as fetcher:
        try:
            async for ref in crawler.discover(fetcher):
                await asyncio.sleep(0)  # Keep API and worker heartbeat responsive during feed-only imports.
                if ref.external_id in observed:
                    continue
                observed.add(ref.external_id)
                with session_factory() as session:
                    run = session.get(CrawlRun, run_id)
                    should_fetch = needs_detail(session, crawler.site_id, ref, now=utcnow(),
                                                recheck_hours=recheck_hours)
                    if should_fetch and max_items is not None and detail_attempts >= max_items:
                        if run.failed_count:
                            status = 'partial'
                        else:
                            status = 'limited'
                            error_summary = 'Detail budget reached; remaining postings continue in the next batch'
                        break
                    run.seen_count += 1
                    session.commit()
                scope = (ref.payload or {}).get('scope_id') or ref.external_id.split(':', 1)[0]
                if not should_fetch:
                    with session_factory() as session:
                        mark_present(session, crawler.site_id, ref.external_id, scope)
                        session.commit()
                    successful.setdefault(scope, set()).add(ref.external_id)
                    if hasattr(crawler, 'on_success'):
                        crawler.on_success(ref)
                    continue
                try:
                    detail_attempts += 1
                    raw = await crawler.parse(fetcher, ref)
                    if raw.source_id != crawler.site_id or raw.external_id != ref.external_id:
                        raise ValueError("Crawler returned mismatched source_id/external_id")
                    with session_factory() as session:
                        save_raw_posting(session, raw)
                        captured = fetcher.pop_captured_html(ref.url)
                        if captured is not None:
                            save_raw_html(session, crawler.site_id, ref.external_id, captured)
                        session.commit()
                    draft = await normalize_draft(raw, session_factory)
                    with session_factory() as session:
                        outcome = upsert_job(session, draft)
                        update_classification(session, draft)
                        mark_present(session, crawler.site_id, ref.external_id, scope)
                        successful.setdefault(scope, set()).add(ref.external_id)
                        mark_detail_checked(session, crawler.site_id, ref, now=utcnow())
                        run = session.get(CrawlRun, run_id)
                        if outcome == "created":
                            run.created_count += 1
                        elif outcome == "updated":
                            run.updated_count += 1
                        session.commit()
                    consecutive_failures = 0
                    if hasattr(crawler, 'on_success'):
                        crawler.on_success(ref)
                except PostingUnavailable as exc:
                    if hasattr(crawler,'on_unavailable'):
                        crawler.on_unavailable(ref)
                    with session_factory() as session:
                        existing=session.scalar(select(Job).where(Job.source_id==crawler.site_id,Job.external_id==ref.external_id))
                        if existing is not None:
                            if existing.source_status=='open':existing.source_status='unknown'
                            existing.deadline_text='원문 비공개/삭제 확인; 지원 상태 확인 필요'
                            existing.content_hash=''
                            existing.updated_at=utcnow()
                            session.commit()
                    consecutive_failures=0
                except Exception as exc:
                    log.exception("Failed posting %s (%s)", ref.external_id, crawler.site_id)
                    status = "partial"
                    consecutive_failures += 1
                    if hasattr(crawler, 'on_failure'):
                        crawler.on_failure(ref, exc)
                    with session_factory() as session:
                        run = session.get(CrawlRun, run_id)
                        run.failed_count += 1
                        run.error_summary = f"One or more detail pages failed; last: {type(exc).__name__}: {str(exc)[:200]}"
                        session.commit()
                    if consecutive_failures >= 5:
                        break
        except Exception as exc:
            log.exception("List discovery failed for %s", crawler.site_id)
            status = "failed"
            error_summary = f"{type(exc).__name__}: {str(exc)[:500]}"
    # Only completed, nonempty *company* scopes may infer closure. Limit-mode
    # and any parsing errors disable closure (never infer from a partial listing).
    errors = list(getattr(crawler, 'scope_errors', []))
    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        from .database import HttpMetric
        session.add_all(HttpMetric(**row) for row in getattr(fetcher, 'metrics', []))
        if errors:
            run.failed_count += len(errors)
            run.error_summary = '; '.join(errors[:4])[:1000]
            if status == 'completed':
                status = 'partial'
        if status == 'completed' and not run.failed_count:
            for scope, complete_set in getattr(crawler, 'completed_scopes', {}).items():
                # Require all validated rows in this successful snapshot to have
                # completed item processing, otherwise do not close any records.
                if complete_set and complete_set == successful.get(scope, set()):
                    reconcile_scope(session, crawler.site_id, scope, complete_set)
        run.finished_at = utcnow()
        run.status = status
        if error_summary:
            run.error_summary = error_summary
        session.commit()
        session.refresh(run)
        session.expunge(run)
        return run
