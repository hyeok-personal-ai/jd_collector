"""Update derived filters offline from existing DB without source requests or LLM."""
from __future__ import annotations
import argparse
from sqlalchemy import select
from .database import Job, SessionLocal, init_db
from .schema import JobDraft
from .classification_store import update_classification


def relabel_all(session_factory=SessionLocal) -> int:
    init_db()
    n=0
    with session_factory() as db:
        for job in db.scalars(select(Job).order_by(Job.id)).yield_per(200):
            draft=JobDraft.model_validate({name:getattr(job,name) for name in JobDraft.model_fields})
            update_classification(db,draft)
            n+=1
            if n % 200 == 0:
                db.flush()
        db.commit()
    return n

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.parse_args()
    print(f'Relabeled {relabel_all()} locally stored jobs; no network calls')
