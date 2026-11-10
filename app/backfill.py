"""Update derived filters offline from existing DB without source requests or LLM."""
from __future__ import annotations
import argparse
from sqlalchemy import select, text
from .database import Job, AnalysisRevision, SessionLocal, init_db
from .schema import JobDraft
from .classification_store import update_classification


def relabel_all(session_factory=SessionLocal, *, initialize=True) -> int:
    if initialize:init_db()
    n=0
    with session_factory() as db:
        if db.get_bind().dialect.name=='sqlite':db.execute(text('BEGIN IMMEDIATE'))
        for job in db.scalars(select(Job).order_by(Job.id)).yield_per(200):
            draft=JobDraft.model_validate({name:getattr(job,name) for name in JobDraft.model_fields})
            update_classification(db,draft)
            n+=1
            if n % 200 == 0:
                db.flush()
        db.commit()
    return n


def ensure_current_labels(session_factory=SessionLocal):
    from .job_labels import LABEL_VERSION
    with session_factory() as db:
        revision=db.get(AnalysisRevision,'classification')
        if revision and revision.version==LABEL_VERSION:return 0
    count=relabel_all(session_factory,initialize=False)
    with session_factory() as db:
        db.merge(AnalysisRevision(key='classification',version=LABEL_VERSION));db.commit()
    return count

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.parse_args()
    print(f'Relabeled {relabel_all()} locally stored jobs; no network calls')
