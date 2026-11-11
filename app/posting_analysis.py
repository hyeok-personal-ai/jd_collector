"""Extractive, source-linked posting analysis. No generated eligibility claims."""

from .job_labels import label_job, experience_lines, EXP_HINT, JUNIOR, SENIOR
from .skill_matching import CATALOG, requirement_mentions

from .posting_sections import plain_body, extract_sections
import re
from datetime import date


def quality_warnings(job, sections):
    raw = plain_body(job)
    warnings = []
    if job.deadline_on:
        for match in re.finditer(r'[^\r\n]*(?:접수\s*기간|지원\s*기간|마감\s*일|접수\s*마감)[^\r\n]*', raw):
            dates = []
            for value in re.finditer(r'(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?', match.group()):
                try: dates.append(date(*map(int, value.groups())))
                except ValueError: continue
            if dates and dates[-1] != job.deadline_on:
                warnings.append({'code':'deadline_conflict', 'message':f'저장 마감일 {job.deadline_on}과 본문의 접수 기한 {dates[-1]}이 다릅니다. 지원 페이지에서 확인하세요.',
                    'evidence':match.group(), 'start':match.start(), 'end':match.end(), 'source_url':job.original_url})
    for row in sections['duties']:
        if re.search(r'(?:경험자|이해\s*보유자|경험\s*보유자)(?:\s*\([^\n]*\))?\s*$', row['text']):
            warnings.append({'code':'qualification_in_duties',
                'message':'담당 업무 구역에 지원자 자격 표현이 있습니다. 별도 필수 조건인지 원문에서 확인하세요.',
                'evidence':row['text'], 'start':row['start'], 'end':row['end'], 'source_url':job.original_url})
    return warnings


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
            'quality_warnings':quality_warnings(job,sections),
            'method':'extractive_sections_v6',
            'provenance':{'content_hash':getattr(job,'content_hash',None),
                          'last_seen_at':str(getattr(job,'last_seen_at','') or ''),
                          'updated_at':str(getattr(job,'updated_at','') or ''),
                          'basis':'수집 원문. 마지막 수집 시각은 실시간 모집 상태 확인을 보장하지 않습니다.'},
            'note':'원문 구역·표현에서 추출했습니다. 미기재는 조건 없음이 아니며, 대안 조건은 원문 전체를 확인하세요.'}


def compare_postings(jobs):
    analyses=[structured_analysis(j) for j in jobs]
    common={}
    for category in ('required','preferred','mentioned'):
        sets=[set(a['technologies'][category]) for a in analyses]
        common[category]=sorted(set.intersection(*sets)) if sets else []
    return {'items':analyses,'common_technologies':common,
            'note':'공통 기술은 각 공고의 같은 구분에 나타난 기술입니다. 경력·대안 조건과 원문 근거를 함께 비교하세요.'}
