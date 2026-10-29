"""Presence tracking: absence closure only after *complete* repeated scope scans.

A failed/partial/empty snapshot is never proof that every job was closed.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import Job, PostingPresence, utcnow


def mark_present(db: Session, source_id: str, external_id: str, scope_id: str) -> None:
    state = db.scalar(select(PostingPresence).where(PostingPresence.source_id == source_id,
                                                    PostingPresence.external_id == external_id))
    job = db.scalar(select(Job).where(Job.source_id == source_id, Job.external_id == external_id))
    if not job:
        return
    if state is None:
        db.add(PostingPresence(source_id=source_id, external_id=external_id, scope_id=scope_id,
                               last_present_at=utcnow()))
    else:
        state.scope_id = scope_id
        state.missing_scans = 0
        state.last_present_at = utcnow()
        if state.closed_by_absence:
            from .deadline_policy import today_kst
            if job.source_status == 'closed' and (not job.deadline_on or job.deadline_on >= today_kst()):
                job.source_status = 'open'
            state.closed_by_absence = False


def reconcile_scope(db: Session, source_id: str, scope_id: str, seen: set[str], *,
                    min_missing_scans: int = 2, min_age_hours: float = 24,
                    now: datetime | None = None) -> int:
    """Only caller that proved a complete, nonempty scope scan may invoke this."""
    if not seen:
        return 0
    if min_missing_scans < 2:
        raise ValueError('Two independent scans required for disappearance closure')
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    closures = 0
    for state in db.scalars(select(PostingPresence).where(PostingPresence.source_id == source_id,
                                                         PostingPresence.scope_id == scope_id)).all():
        if state.external_id in seen:
            continue
        state.missing_scans += 1
        last = state.last_present_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if state.missing_scans < min_missing_scans or now - last < timedelta(hours=min_age_hours):
            continue
        job = db.scalar(select(Job).where(Job.source_id == source_id, Job.external_id == state.external_id))
        if job and job.source_status == 'open':
            job.source_status = 'closed'
            job.updated_at = now
            state.closed_by_absence = True
            closures += 1
    return closures
