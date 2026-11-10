"""Extractive, source-linked posting analysis. No generated eligibility claims."""

from .job_labels import label_job, experience_lines, EXP_HINT, JUNIOR, SENIOR
from .skill_matching import CATALOG, requirement_mentions

from .posting_sections import plain_body, extract_sections


def structured_analysis(job):
    raw=plain_body(job)
    sections=extract_sections(job)
    label=label_job(job)
    return {'job_id':job.id, 'title':job.title,'organization':job.organization,
            'source_url':job.original_url,'sections':sections,
            'technologies':requirement_mentions(job,list(CATALOG)),
            'career':{'level':label.career,'min_years':label.min_years,'max_years':label.max_years,
                      'reasons':label.reasons,
                      'evidence':[line.strip() for line in [job.title,*experience_lines(job)]
                                  if EXP_HINT.search(line) or JUNIOR.search(line) or SENIOR.search(line)]},
            'conditions':{'region':job.region,'employment_type':job.employment_type,
                          'deadline_on':str(job.deadline_on) if job.deadline_on else None,
                          'deadline_text':job.deadline_text,'source_status':job.source_status},
            'missing_sections':[k for k in ('duties','required','preferred','conditions') if not sections[k]],
            'method':'extractive_sections_v4',
            'note':'원문 구역·표현에서 추출했습니다. 미기재는 조건 없음이 아니며, 대안 조건은 원문 전체를 확인하세요.'}


def compare_postings(jobs):
    analyses=[structured_analysis(j) for j in jobs]
    common={}
    for category in ('required','preferred','mentioned'):
        sets=[set(a['technologies'][category]) for a in analyses]
        common[category]=sorted(set.intersection(*sets)) if sets else []
    return {'items':analyses,'common_technologies':common,
            'note':'공통 기술은 각 공고의 같은 구분에 나타난 기술입니다. 경력·대안 조건과 원문 근거를 함께 비교하세요.'}
