"""Use known deadlines locally; revisit postings whose closure can be unpredictable."""
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from sqlalchemy import update
from .database import Job, SessionLocal, utcnow
from .locking import FileLock


FLEXIBLE_TEXT = re.compile(r'상시|수시|채용\s*시|충원\s*시|조기\s*마감|until\s+filled|rolling', re.I)
FLEXIBLE_BODY = re.compile(
    r'조기\s*마감|상시\s*(?:채용|모집|접수)|수시\s*(?:채용|모집)|'
    r'채용\s*(?:완료\s*)?시\s*(?:마감|까지)|충원\s*(?:시|완료)|'
    r'until\s+(?:the\s+)?(?:position\s+is\s+)?filled|rolling\s+(?:applications?|recruitment)|'
    r'(?:may|can)\s+(?:be\s+)?clos(?:e|ed)\s+early', re.I)


def today_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def review_mode(job):
    if job.source_status == 'closed':
        return 'closed'
    if FLEXIBLE_TEXT.search(job.deadline_text or '') or FLEXIBLE_BODY.search(job.body_text or ''):
        return 'flexible'
    return 'fixed' if job.deadline_on else 'unknown'


def review_metadata(job):
    mode = review_mode(job)
    return {'recheck_mode': mode,
            'deadline_on': job.deadline_on.isoformat() if job.deadline_on else None,
            'active': mode != 'closed' and (not job.deadline_on or job.deadline_on >= today_kst())}


def expire_deadlines(session, *, today=None):
    """Deadline day remains open; close from the next Korean calendar day."""
    result = session.execute(update(Job).where(Job.deadline_on < (today or today_kst()),
        Job.source_status != 'closed').values(source_status='closed', updated_at=utcnow(), content_hash=''))
    return result.rowcount


def expire_local_deadlines():
    """Best-effort idle maintenance; never wait behind a running crawler."""
    with SessionLocal() as session:
        url = session.get_bind().url
    lock = None
    if url.get_backend_name() == 'sqlite' and url.database and url.database != ':memory:':
        lock = FileLock(str(Path(url.database).resolve()) + '.writer.lock')
        if not lock.acquire():
            return 0
    try:
        with SessionLocal() as session:
            count = expire_deadlines(session)
            session.commit()
            return count
    finally:
        if lock:
            lock.release()
