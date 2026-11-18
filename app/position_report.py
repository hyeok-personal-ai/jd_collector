"""Company and role briefs grounded in the locally collected postings."""
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from .company_catalog import load_companies
from .company_identity import normalize_company
from .database import Job
from .deadline_policy import today_kst
from .posting_analysis import structured_analysis
from .qualification import assess_analysis


def public_link(value):
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))


def hiring_context(db, job, company, organizations, catalog):
    names = organizations.get(company, [])
    official = [entry for entry in catalog if normalize_company(entry['name']) == company
                and entry.get('source_type') == 'official_careers']
    peers = []
    if names:
        query = (select(Job).where(Job.organization.in_(names), Job.id != job.id,
                                  Job.source_status == 'open')
                 .order_by(Job.last_seen_at.desc(), Job.id.desc()).limit(120))
        today = today_kst()
        peers = [other for other in db.scalars(query)
                 if other.deadline_on is None or other.deadline_on >= today]
    # Count probable cross-source copies once. Keep the original records intact.
    seen = {' '.join(job.title.casefold().split())}
    counts = {key: Counter() for key in ('required', 'preferred')}
    examples = []
    for other in peers:
        title = ' '.join(other.title.casefold().split())
        if title in seen:
            continue
        seen.add(title)
        analysis = structured_analysis(other)
        for key, counter in counts.items():
            counter.update(set(analysis['technologies'][key]))
        examples.append({'title': other.title, 'source': public_link(other.original_url),
                         'last_seen_at': other.last_seen_at.isoformat()})
        if len(examples) == 10:
            break
    return {
        'name': job.organization,
        'official_careers': list(dict.fromkeys(entry['url'] for entry in official)),
        'sample_size': len(examples),
        'sample_scope': '같은 회사로 식별된 최근 진행 공고 최대 10건. 같은 제목은 한 번 집계했습니다. 여러 직무가 포함될 수 있습니다.',
        'technology_mentions': {k: [{'skill': skill, 'postings': count} for skill, count in
                                    sorted(v.items(), key=lambda x: (-x[1], x[0]))[:8]]
                                for k, v in counts.items()},
        'related_postings': examples,
    }


def build_position_report(db, jobs, documents=(), candidate_facts=None):
    organizations = {}
    catalog = load_companies()
    for name in db.scalars(select(Job.organization).distinct()):
        if company := normalize_company(name):
            organizations.setdefault(company, []).append(name)
    variants = {}
    for document in documents:
        variants.setdefault(document.get('variant') or '미분류', []).append(document)
    items = []
    for job in jobs:
        analysis = structured_analysis(job)
        sections = analysis['sections']
        required = [r for r in sections['required'] if not r['non_required']]
        checks = [warning['message'] for warning in analysis.get('quality_warnings', [])]
        if not required:
            checks.append('상세 자격요건을 지원 페이지에서 확인하세요.')
        if not job.deadline_on:
            checks.append('접수 중인지 지원 페이지에서 확인하세요.')
        if not job.employment_type or not job.region:
            checks.append('고용 형태와 근무지를 확인하세요.')
        if job.source_status != 'open' or (job.deadline_on and job.deadline_on < today_kst()):
            checks.append('모집 상태 또는 접수 기한을 확인하세요.')
        matches = []
        for variant, selected in variants.items():
            assessment = assess_analysis(analysis, selected, candidate_facts=candidate_facts)
            matches.append({'variant': variant, **assessment})
        items.append({
            'job_id': job.id, 'title': job.title,
            'company': hiring_context(db, job, normalize_company(job.organization), organizations, catalog),
            'position': analysis,
            'suitable_for': [row['text'] for row in required[:4]],
            'decision_checks': checks,
            'apply_url': public_link(job.apply_url),
            'personal_assessments': matches,
        })
    return {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'scope': '수집 공고와 등록된 공식 채용 링크 기반. 회사 규모·재무·평판 등 별도 확인이 필요한 정보는 포함하지 않습니다.',
            'items': items}
