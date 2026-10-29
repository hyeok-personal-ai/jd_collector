"""Re-run rule/model normalization from stored parsed content. Never touches source sites.

Usage: python -m app.renormalize --model qwen3:4b
"""
import argparse
import asyncio

from sqlalchemy import select
from .database import RawPosting, Job, SessionLocal, init_db
from .classification_store import update_classification
from .normalization import normalize_draft
from .repository import upsert_job
from .schema import JobDraft


async def run(*, model: str | None = None) -> tuple[int, int]:
    from .locking import writer_guard
    async with writer_guard(SessionLocal):
        return await _run(model=model)


async def _run(*, model: str | None = None) -> tuple[int, int]:
    init_db()
    with SessionLocal() as session:
        raw_ids = list(session.scalars(select(RawPosting.id).order_by(RawPosting.id)))
    processed = updated = 0
    for raw_id in raw_ids:
        with SessionLocal() as session:
            raw = session.get(RawPosting, raw_id)
            draft = JobDraft.model_validate(raw.payload)
        normalized = await normalize_draft(draft, SessionLocal, llm_model=model)
        with SessionLocal() as session:
            current = session.scalar(select(Job).where(Job.source_id == normalized.source_id,
                                                        Job.external_id == normalized.external_id))
            if current is not None:
                # Offline repair is not new evidence of reopening a disappeared job.
                normalized = normalized.model_copy(update={'source_status': current.source_status})
            result = upsert_job(session, normalized, update_last_seen=False)
            update_classification(session, normalized)
            if result in ("created", "updated"):
                updated += 1
            session.commit()
        processed += 1
    return processed, updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Renormalize cached source postings without crawling")
    parser.add_argument("--model", default=None, help="Ollama model, e.g. qwen3:4b; blank uses environment")
    args = parser.parse_args()
    processed, updated = asyncio.run(run(model=args.model))
    print(f"processed={processed} changed={updated}")
