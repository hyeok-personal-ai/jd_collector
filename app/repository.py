"""Idempotent save and change detection."""
import hashlib
import json
import gzip
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import Job, RawPosting, RawHtml, utcnow
from .schema import JobDraft


def upsert_job(session: Session, draft: JobDraft, *, update_last_seen: bool = True) -> str:
    """Returns created/updated/unchanged. Caller commits per item or per batch.

    Enforce a UNIQUE(source_id, external_id) in DB. Serialize jobs per source
    in the MVP; parallel writers will need an atomic ON CONFLICT upsert.
    """
    payload = draft.model_dump(mode="python")
    payload["attachments"] = [attachment.model_dump(mode="json") for attachment in draft.attachments]
    digest_data = draft.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(digest_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    existing = session.scalar(select(Job).where(Job.source_id == draft.source_id,
                                               Job.external_id == draft.external_id))
    now = utcnow()
    if existing is None:
        job = Job(**payload, content_hash=digest,
                  first_seen_at=now, last_seen_at=now, updated_at=now)
        session.add(job)
        return "created"
    if update_last_seen:
        existing.last_seen_at = now
    if existing.content_hash == digest:
        return "unchanged"
    for field, value in payload.items():
        setattr(existing, field, value)
    existing.content_hash = digest
    existing.updated_at = now
    return "updated"


def save_raw_posting(session: Session, draft: JobDraft) -> None:
    """Retain parser output for re-normalization without network access."""
    payload = draft.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    existing = session.scalar(select(RawPosting).where(RawPosting.source_id == draft.source_id,
                                                      RawPosting.external_id == draft.external_id))
    if existing is None:
        session.add(RawPosting(source_id=draft.source_id, external_id=draft.external_id,
                               payload=payload, payload_hash=digest, last_fetched_at=utcnow()))
    else:
        if existing.payload_hash != digest:
            existing.payload = payload
            existing.payload_hash = digest
        existing.last_fetched_at = utcnow()


def save_raw_html(session: Session, source_id: str, external_id: str, html: str) -> None:
    """Content-addressed update, compressed to avoid inflating the MVP DB."""
    raw = html.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    existing = session.scalar(select(RawHtml).where(RawHtml.source_id == source_id,
                                                   RawHtml.external_id == external_id))
    if existing is None:
        session.add(RawHtml(source_id=source_id, external_id=external_id,
                            html_gzip=gzip.compress(raw), sha256=digest))
    elif existing.sha256 != digest:
        existing.html_gzip = gzip.compress(raw)
        existing.sha256 = digest
        existing.stored_at = utcnow()
