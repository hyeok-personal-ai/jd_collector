from sqlalchemy import select
from sqlalchemy.orm import Session
from .job_labels import label_job
from .database import JobClassification, utcnow
from .schema import JobDraft


def update_classification(db: Session, draft: JobDraft) -> None:
    label = label_job(draft)
    obj = db.scalar(select(JobClassification).where(JobClassification.source_id == draft.source_id,
                                                    JobClassification.external_id == draft.external_id))
    if obj is None:
        obj = JobClassification(source_id=draft.source_id, external_id=draft.external_id)
        db.add(obj)
    obj.it_match = label.it_match
    obj.career = label.career
    obj.min_years = label.min_years
    obj.max_years = label.max_years
    obj.reasons = list(label.reasons)
    obj.updated_at = utcnow()
