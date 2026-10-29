"""Explainable, conservative entry-level and IT filters.

No model calls; ambiguous jobs remain unknown rather than being silently hidden.
Labels are derived after normalization and can be recalculated offline.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, asdict
from .schema import JobDraft

IT_TERMS = re.compile(r'백엔드|프론트엔드|풀스택|개발자|소프트웨어|프로그래머|프로그래밍|웹\s*개발|앱\s*개발|서버\s*개발|모바일\s*개발|SW\s*개발|데이터 엔지니어|데이터 사이언|머신러닝|딥러닝|인공지능|AI engineer|ML engineer|software engineer|programmer|frontend|front.?end|back.?end|full.?stack|developer|devops|SRE\b|cloud engineer|platform engineer|security engineer|security researcher|QA engineer|data engineer|data scientist|machine learning|embedded|임베디드|컴파일러|보안 엔지니어|IT 인프라|시스템 엔지니어', re.I)
NON_IT_TITLES = re.compile(r'영업|마케팅|마케터|디자이너|회계|재무|인사|홍보|세일즈|고객지원|사업개발|조리사|간호사|매장관리|회계담당|경영지원|account executive|sales(?!force)|marketing|human resources|recruiter',re.I)
SENIOR = re.compile(r'시니어|수석|책임|팀장|리드\b|senior|staff\b|principal\b|lead\b|director|head of|manager', re.I)
JUNIOR = re.compile(r'신입|신입사원|주니어|인턴|인턴십|경력무관|경력 무관|무경력|new grad|new graduate|entry.level|junior|\bintern(?:ship)?\b|early career|graduate program',re.I)
INTERN = re.compile(r'인턴|\bintern(?:ship)?\b', re.I)
YEAR_RANGE = re.compile(r'(?:경력\s*)?(\d{1,2})\s*(?:년|years?|yrs?)?\s*[~\-–]\s*(\d{1,2})\s*(?:년|years?|yrs?)',re.I)
YEAR_MIN = re.compile(r'(?:경력\s*)?(\d{1,2})\s*(?:년|years?|yrs?)\s*(?:이상|이상의|이상인|부터|\+|or more|experience required|minimum)',re.I)
YEAR_UPTO = re.compile(r'(?:경력\s*)?(\d{1,2})\s*(?:년|years?|yrs?)\s*(?:이하|미만|까지|or less)',re.I)
YEAR_PLUS = re.compile(r'(\d{1,2})\s*\+\s*(?:년|years?|yrs?)', re.I)
EN_MIN = re.compile(r'(?:at least|minimum of|minimum)\s*(\d{1,2})\s*(?:years?|yrs?)',re.I)
# Restrict experience extraction to vacancy title and nearby qualification lines.
EXP_HINT = re.compile(r'경력|experience|years? (?:of |in )|yrs?',re.I)
HARDWARE_TITLE = re.compile(r'\bHW\b|하드웨어|회로\s*설계|기구\s*설계|기계\s*설계|센서|신약|제제|레이다|레이더|\bSAR\b', re.I)
SOFTWARE_TITLE = re.compile(r'소프트웨어|펌웨어|프로그래|\bSW\b|software|firmware|programmer|백엔드|프론트엔드|웹\s*개발|앱\s*개발', re.I)
CORE_DEVELOPMENT_ROLE = re.compile(r'백엔드|프론트엔드|풀스택|프로그래머|프로그래밍|펌웨어|firmware|programmer|frontend|front.?end|back.?end|full.?stack|웹\s*개발|앱\s*개발|소프트웨어\s*(?:개발|엔지니어)|software\s+(?:engineer|developer)|(?:Java|Python|C\+\+|C#|TypeScript|Kotlin|Swift|React|Node|Flutter|Unity|Unreal)\s*(?:개발|developer|engineer)',re.I)
NON_SOFTWARE_ROLE = re.compile(r'contents?\s+developer|콘텐츠\s*(?:개발|기획|운영)|작가\s*발굴|프로듀싱|법인장|기술\s*영업|영업\s*마케팅|business\s+develop',re.I)

@dataclass(frozen=True)
class Labels:
    it_match: str      # yes/no/unknown
    career: str        # intern/entry/early/experienced/unknown
    min_years: int | None
    max_years: int | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        d = asdict(self)
        d['reasons'] = list(self.reasons)
        return d


def label_job(draft: JobDraft) -> Labels:
    title = draft.title or ''
    short = ' '.join(filter(None, [title, draft.recruitment_type, draft.employment_type]))
    # Short anchored snippets avoid taking years from employee benefits / company history.
    lines = (draft.body_text or '').splitlines()
    qualifiers = [line[max(0, m.start()-60):m.end()+180]
                  for line in lines for m in EXP_HINT.finditer(line)][:30]
    if not qualifiers and draft.body_text and len(draft.body_text) < 650:
        qualifiers = [draft.body_text]
    experience_text = short + '\n' + '\n'.join(qualifiers)
    reasons = []
    title_it = bool(IT_TERMS.search(title))
    non_it = bool(NON_IT_TITLES.search(title))
    core_role=bool(CORE_DEVELOPMENT_ROLE.search(title))
    if (NON_SOFTWARE_ROLE.search(title) or non_it) and not core_role:
        it = 'no'
        reasons.append('non_software_role_title')
    elif HARDWARE_TITLE.search(title) and not core_role:
        it = 'unknown'
        reasons.append('hardware_title_software_role_unconfirmed')
    elif title_it:
        it = 'yes'
        reasons.append('it_title')
    elif non_it:
        it = 'no'
        reasons.append('non_it_title')
    elif IT_TERMS.search((draft.body_text or '')[:600]):
        it = 'unknown'  # Tech appears only in description, may be a nontechnical role.
        reasons.append('it_body_only_ambiguous')
    else:
        it = 'unknown'
    lower = None
    upper = None
    found = YEAR_RANGE.search(experience_text)
    if found:
        lower, upper = int(found.group(1)), int(found.group(2))
        reasons.append('experience_range')
    else:
        found = YEAR_MIN.search(experience_text) or EN_MIN.search(experience_text) or YEAR_PLUS.search(experience_text)
        if found:
            lower = int(found.group(1))
            reasons.append('minimum_years')
        else:
            found = YEAR_UPTO.search(experience_text)
            if found:
                upper = int(found.group(1))
                reasons.append('maximum_years')
    if lower is not None and upper is not None and lower > upper:
        lower, upper = None, None
        reasons.append('invalid_range_ignored')
    if INTERN.search(short):
        career = 'intern'
        reasons.append('intern_signal')
    elif lower is not None and lower > 3:
        career = 'experienced'
        reasons.append('requires_over_3_years')
    elif lower is not None or upper is not None:
        career = 'early' if lower is None or lower <= 3 else 'experienced'
        reasons.append('years_compatible_with_entry')
    elif SENIOR.search(title):
        career = 'experienced'
        reasons.append('senior_title')
    elif JUNIOR.search(experience_text):
        career = 'entry'
        reasons.append('junior_signal')
    else:
        career = 'unknown'
    return Labels(it, career, lower, upper, tuple(reasons))
