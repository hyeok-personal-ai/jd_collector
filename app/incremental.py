"""Incremental listing fetch: conditional HTTP and stable content hashes.

A 304 / byte-identical 200 avoids re-parsing the list. These optimizations do
not claim to discover silent changes; an explicit backfill can bypass them.
"""
from __future__ import annotations
import hashlib
import os
from datetime import timedelta
from collections.abc import Callable
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import ListingPage, utcnow


class ListingCache:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def headers_for(self, site: str, url: str) -> dict[str, str]:
        with self.session_factory() as db:
            row = db.scalar(select(ListingPage).where(ListingPage.source_id == site, ListingPage.url == url))
            if row is None:
                return {}
            hours = float(os.getenv('LISTING_FULL_SCAN_HOURS', '72'))
            # Periodic full scans are required to catch silent detail changes.
            if hours == 0 or row.checked_at is None or (utcnow() - _utc(row.checked_at)) >= timedelta(hours=hours):
                return {}
            headers = {}
            if row.etag:
                headers["If-None-Match"] = row.etag
            if row.last_modified:
                headers["If-Modified-Since"] = row.last_modified
            return headers

    def update(self, site: str, url: str, *, status: int, text: str | None = None,
               etag: str | None = None, last_modified: str | None = None) -> bool:
        """Returns True if listing needs parsing; update only after valid response.

        In particular 200 with an unrecognized HTML layout MUST NOT be persisted.
        """
        if status not in (200, 304):
            raise ValueError(f"Unsupported listing HTTP status {status}")
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest() if text is not None else None
        with self.session_factory() as db:
            row = db.scalar(select(ListingPage).where(ListingPage.source_id == site, ListingPage.url == url))
            if status == 304:
                if row is None:
                    raise ValueError('304 without cached listing')
                # Preserve last *full listing scan* time for mandatory periodic audit.
                return False
            if digest is None:
                raise ValueError('200 requires content')
            hours = float(os.getenv('LISTING_FULL_SCAN_HOURS', '72'))
            due = row is not None and (hours == 0 or utcnow() - _utc(row.checked_at) >= timedelta(hours=hours))
            changed = row is None or row.content_hash != digest or due
            if row is None:
                row = ListingPage(source_id=site, url=url, content_hash=digest)
                db.add(row)
            else:
                row.content_hash = digest
            if etag is not None:
                row.etag = etag
            if last_modified is not None:
                row.last_modified = last_modified
            # Do not slide the mandatory full-scan deadline on identical 200s.
            if changed:
                row.checked_at = utcnow()
            db.commit()
            return changed

    def count_known(self, site: str, ids: list[str]) -> int:
        if not ids:
            return 0
        from .database import Job
        with self.session_factory() as db:
            return len(db.scalars(select(Job.external_id).where(Job.source_id == site, Job.external_id.in_(ids))).all())


    def count_known_unchanged(self, site: str, refs: list) -> int:
        """Do NOT stop pagination merely because IDs exist.

        A known but revised job must keep pagination alive. Unknown revision
        tokens also prevent the early-stop optimization.
        """
        if not refs:
            return 0
        from .database import Job, ListingState
        with self.session_factory() as db:
            known_ids = set(db.scalars(select(Job.external_id).where(
                Job.source_id == site, Job.external_id.in_([r.external_id for r in refs]))).all())
            state = {s.external_id: s.listing_fingerprint for s in db.scalars(
                select(ListingState).where(ListingState.source_id == site,
                    ListingState.external_id.in_([r.external_id for r in refs]))).all()}
        return sum(1 for ref in refs if ref.external_id in known_ids and
                   ref.listing_fingerprint is not None and
                   state.get(ref.external_id) == ref.listing_fingerprint)


def _utc(value):
    from datetime import timezone
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
