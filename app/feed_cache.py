"""Conditional GET for publicly documented ATS listings, backed by DB JSON.

304 reuses the last complete list; a forced 200 audits against stale caches.
Only invoked when a persistent DB session factory and HTTP response primitive exist.
"""
from __future__ import annotations
import json
import os
from datetime import timedelta, timezone
from urllib.parse import urlencode
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import PublicFeedCache, utcnow


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def public_json(fetcher, *, site: str, url: str, params: dict | None = None, listing_cache=None):
    """Return parsed JSON. Falls back to get_json for custom/offline fetchers."""
    if listing_cache is None or not hasattr(fetcher, 'get_response'):
        return await fetcher.get_json(url, params=params)
    key = url + ('?' + urlencode(sorted(params.items())) if params else '')
    sessions=listing_cache.session_factory
    with sessions() as db:
        saved=db.scalar(select(PublicFeedCache).where(PublicFeedCache.source_id==site,
                                                       PublicFeedCache.url==key))
        headers={}
        audit_hours=float(os.getenv('PUBLIC_FEED_FULL_AUDIT_HOURS','72'))
        if audit_hours < 0:
            raise ValueError('PUBLIC_FEED_FULL_AUDIT_HOURS cannot be negative')
        if saved and audit_hours and (utcnow()-_aware(saved.last_full_fetch_at))<timedelta(hours=audit_hours):
            if saved.etag:
                headers['If-None-Match']=saved.etag
            if saved.last_modified:
                headers['If-Modified-Since']=saved.last_modified
    result=await fetcher.get_response(url,params=params,headers=headers,accept='application/json')
    if result.status==304:
        if not headers or saved is None:
            raise ValueError('HTTP 304 received without a valid cached feed')
        return saved.payload
    if result.status!=200 or result.text is None:
        raise ValueError(f'Public list request did not return 200/304 ({site})')
    try:
        data=json.loads(result.text)
    except ValueError:
        raise ValueError(f'Invalid JSON document in public ATS feed {site}') from None
    expected = {'public_greenhouse':'jobs', 'public_ashby':'jobs',
                'public_smartrecruiters':'content'}
    if site=='public_lever':
        if not isinstance(data, list):
            raise ValueError('Invalid Lever listing array; not caching response')
    elif site in expected:
        if not isinstance(data,dict) or not isinstance(data.get(expected[site]),list):
            raise ValueError(f'Invalid {site} posting list; not caching response')
    if len(result.text)>4_000_000:
        return data  # Store no oversized responses; they will still be safely parsed.
    with sessions() as db:
        row=db.scalar(select(PublicFeedCache).where(PublicFeedCache.source_id==site,
                                                     PublicFeedCache.url==key))
        if row is None:
            row=PublicFeedCache(source_id=site,url=key,payload=data)
            db.add(row)
        else:
            row.payload=data
        row.etag=result.etag
        row.last_modified=result.last_modified
        row.last_full_fetch_at=utcnow()
        db.commit()
    return data
