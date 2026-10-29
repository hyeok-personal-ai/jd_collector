"""Durable, atomic API request reservations shared by concurrent workers.

Counts attempts (including HTTP errors/retries) before sending any request.
Supports SQLite and PostgreSQL through their shared ON CONFLICT syntax.
For accurate accounting every request to the licensed API must pass a hook.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import text
from sqlalchemy.orm import Session
from collections.abc import Callable

KST = ZoneInfo("Asia/Seoul")


class ApiQuota:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def reserve(self, provider: str, *, limit: int = 450, now: datetime | None = None) -> int:
        if limit < 1 or limit > 500:
            raise ValueError("Request cap must be 1..500")
        current = now or datetime.now(KST)
        usage_day = current.astimezone(KST).date().isoformat()
        with self.session_factory() as db:
            db.execute(text("""
                INSERT INTO api_request_usage (source_id, usage_day, call_count)
                VALUES (:source_id, :usage_day, 0)
                ON CONFLICT (source_id, usage_day) DO NOTHING
            """), {"source_id": provider, "usage_day": usage_day})
            result = db.execute(text("""
                UPDATE api_request_usage
                SET call_count = call_count + 1
                WHERE source_id=:source_id AND usage_day=:usage_day
                  AND call_count < :limit
            """), {"source_id": provider, "usage_day": usage_day, "limit": limit})
            if result.rowcount != 1:
                db.rollback()
                raise PermissionError(f"Local API quota exhausted for {provider} on {usage_day} (cap {limit})")
            count = db.scalar(text("""
                SELECT call_count FROM api_request_usage
                WHERE source_id=:source_id AND usage_day=:usage_day
            """), {"source_id": provider, "usage_day": usage_day})
            db.commit()
            return int(count)
