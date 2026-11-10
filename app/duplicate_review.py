"""Suggest candidates; human review groups links without deleting source records."""
import re
from sqlalchemy import select
from .database import Job
from .schema import JobOut
from .control import ControlStore
from .company_identity import normalize_company
from .duplicates import canonical_apply_url, _title_parts


def compatible_pair(a,b):
    """Contradictions rule out a semantic duplicate before any grouping review."""
    from .job_labels import label_job
    left,right=label_job(a),label_job(b)
    if (left.career=='intern') != (right.career=='intern'):return False
    if left.min_years is not None and right.max_years is not None and left.min_years>right.max_years:return False
    if right.min_years is not None and left.max_years is not None and right.min_years>left.max_years:return False
    roles={'backend':r'백엔드|back.?end','frontend':r'프론트|front.?end',
           'data':r'데이터|data','mobile':r'모바일|mobile|android|ios',
           'qa':r'\bqa\b|테스트|test engineer','infra':r'devops|\bsre\b|인프라|infrastructure'}
    a_roles={key for key,pattern in roles.items() if re.search(pattern,a.title,re.I)}
    b_roles={key for key,pattern in roles.items() if re.search(pattern,b.title,re.I)}
    return not (a_roles and b_roles and a_roles.isdisjoint(b_roles))


def candidates(db,job):
    groups,rejected=ControlStore().duplicate_reviews()
    grouped={i for members in groups.values() if job.id in members for i in members}
    excluded=grouped|{b if a==job.id else a for a,b in rejected if job.id in (a,b)}
    company=normalize_company(job.organization)
    exact=canonical_apply_url(job.apply_url)
    matches={}
    company_ids=[]
    for other in db.scalars(select(Job).where(Job.id!=job.id)):
        if other.id in excluded:continue
        same_company=bool(company and company==normalize_company(other.organization))
        compatible=compatible_pair(job,other) if same_company else False
        if same_company and compatible:company_ids.append(other.id)
        reason=None
        if exact and exact==canonical_apply_url(other.apply_url):reason='same_application_url'
        elif same_company and compatible and _title_parts(job.title)[0]==_title_parts(other.title)[0]:reason='same_company_title'
        if reason:
            matches[other.id]={'job':JobOut.model_validate(other).model_dump(mode='json'),'reason':reason,'similarity':None}
    coverage=None
    if company_ids:
        try:
            from .vector_index import nearest
            result=nearest(db,job,[Job.id.in_(company_ids)],limit=30)
            coverage=result['coverage']
            for row in result['items']:
                other=row['job']
                if row['similarity']<.9:continue
                # Different explicit cohort/year identifiers are a material distinction.
                numbers=lambda title:set(re.findall(r'(?<!\w)\d+(?!\w)',title))
                if numbers(job.title)!=numbers(other['title']):continue
                matches.setdefault(other['id'],{**row,'reason':'same_company_semantic_candidate'})
        except ImportError:pass
    return {'items':list(matches.values()),'coverage':coverage,
            'note':'동일 지원 URL·회사/제목·의미 유사도로 찾은 후보입니다. 근무지·경력·마감일을 대조한 뒤 묶으세요. 원문과 지원 기록은 각각 유지됩니다.'}
