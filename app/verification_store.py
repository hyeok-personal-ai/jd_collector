"""Persist verified sample reports independently from job ingest."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .database import SourceVerification
from .verification import report_summary


def store_report(session: Session, report: dict) -> int:
    if report['mode'] == 'catalog_only':
        raise ValueError('Catalog URL guesses are not verification runs')
    record = SourceVerification(source_key=report['source_key'], family=report['family'],
        mode=report['mode'], status=report['status'],
        checked_at=datetime.fromisoformat(report['checked_at']),
        discovered=report['discovered'], sample_attempted=report['sample_attempted'],
        sample_parsed=report['sample_parsed'], report=report)
    session.add(record)
    session.flush()
    return record.id


def latest_reports(session: Session, limit: int = 500) -> list[dict]:
    """Exactly one latest row per source; SQL window works on SQLite/PostgreSQL.

    Prevents loading every historical verification record into Python and
    does not silently ignore older sources after 30,000 history rows.
    """
    rank = func.row_number().over(
        partition_by=SourceVerification.source_key,
        order_by=(SourceVerification.checked_at.desc(), SourceVerification.id.desc())
    ).label('rn')
    ranked = select(SourceVerification.id.label('pk'),rank).subquery()
    statement = (select(SourceVerification)
        .join(ranked, SourceVerification.id == ranked.c.pk)
        .where(ranked.c.rn == 1)
        .order_by(SourceVerification.checked_at.desc(), SourceVerification.id.desc())
        .limit(limit))
    return [{**row.report,'verification_id':row.id} for row in session.scalars(statement).all()]


def verification_overview(session: Session) -> dict:
    reports = latest_reports(session, 10000)
    result = report_summary(reports)
    result['offline_samples'] = sum(r['mode'] == 'offline_fixture' for r in reports)
    return result
