"""Explainable technology mentions, not an assessment of applicant eligibility."""
import re
import threading
from functools import lru_cache
from sqlalchemy import case, func, select, exists, or_
from .database import Job, JobSkillIndex

CATALOG = {
    'Python': ['python','파이썬'], 'Java':['java','자바'],
    'Spring':['spring','spring boot','springboot','스프링'],
    'SQL':['sql'], 'JavaScript':['javascript','자바스크립트'],
    'TypeScript':['typescript','타입스크립트'],
    'Node.js':['node.js','nodejs','node js'], 'NestJS':['nestjs','nest.js','nest js'],
    'AWS':['aws','amazon web services'], 'NCP':['ncp','naver cloud','네이버 클라우드','네이버클라우드'],
    'PostgreSQL':['postgresql','postgres'], 'MySQL':['mysql'], 'Kafka':['kafka','카프카'],
    'Redis':['redis','레디스'], 'Nginx':['nginx','엔진엑스'], 'Docker':['docker','도커'],
    'Docker Compose':['docker compose','docker-compose'], 'Linux':['linux','리눅스'],
    'GitHub Actions':['github actions','github action','깃허브 액션'],
    'CI/CD':['ci/cd','ci cd','ci-cd','cicd','지속적 통합','지속적 배포'],
    'Kubernetes':['kubernetes','k8s','쿠버네티스'], 'Terraform':['terraform','테라폼'],
    'React':['react','react.js','reactjs','리액트'], 'Go':['golang','go'],
    'C++':['c++'], 'C#':['c#'], 'Django':['django','장고'], 'FastAPI':['fastapi'],
}
ALIASES={alias.casefold():name for name,aliases in CATALOG.items() for alias in [name,*aliases]}
BACKEND_TITLE=r'(?i)백엔드|back[ -]?end|서버|server|인프라|infra|devops|\bsre\b|플랫폼|platform|클라우드|cloud|시스템|system|풀스택|full[ -]?stack|소프트웨어|software'


def normalize_skills(raw):
    values=re.split(r'[,;\n]+',raw) if isinstance(raw,str) else raw
    if len(values)>30:raise ValueError('기술은 최대 30개까지 입력할 수 있습니다.')
    result=[]
    for value in values:
        value=value.strip()
        if not value:continue
        if len(value)>40:raise ValueError('기술 이름은 40자 이내로 입력하세요.')
        name=ALIASES.get(value.casefold(),value)
        if name.casefold() not in {v.casefold() for v in result}:result.append(name)
    return result


@lru_cache(maxsize=256)
def skill_pattern(name):
    aliases=CATALOG.get(name,[name])
    # ASCII identifier boundaries prevent Java matching JavaScript and Go matching Django.
    parts=[re.escape(a)+(r'(?!스크립트)' if name=='Java' and a=='자바' else '') for a in aliases]
    return r'(?i)(?<![a-z0-9_])(?:'+'|'.join(parts)+r')(?![a-z_+#])'


def score_expression(skills):
    text=func.coalesce(Job.title,'')+'\n'+func.coalesce(Job.body_text,'')
    def found(skill):
        if skill in CATALOG:
            return exists(select(JobSkillIndex.job_id).where(JobSkillIndex.job_id==Job.id,
                JobSkillIndex.terms.contains('|'+skill.casefold()+'|')))
        return text.regexp_match(skill_pattern(skill))
    return sum((case((found(skill),1),else_=0) for skill in skills),0)


def match_details(job,skills):
    text=(job.title or '')+'\n'+(job.body_text or '')
    matched=[skill for skill in skills if re.search(skill_pattern(skill),text)]
    return {'matched':matched,'not_mentioned':[s for s in skills if s not in matched],
            'count':len(matched),'selected_count':len(skills),
            'requirements': requirement_mentions(job, skills)}


_INDEX_LOCK=threading.Lock()

def ensure_skill_index(db):
    with _INDEX_LOCK:
        _refresh_skill_index(db)

def _refresh_skill_index(db):
    stale=db.scalars(select(Job).outerjoin(JobSkillIndex,JobSkillIndex.job_id==Job.id).where(
        or_(JobSkillIndex.job_id.is_(None),JobSkillIndex.content_hash!=Job.content_hash))).all()
    for job in stale:
        text=(job.title or '')+'\n'+(job.body_text or '')
        terms='|'+'|'.join(name.casefold() for name in CATALOG if re.search(skill_pattern(name),text))+'|'
        row=db.get(JobSkillIndex,job.id)
        if row:row.content_hash=job.content_hash;row.terms=terms
        else:db.add(JobSkillIndex(job_id=job.id,content_hash=job.content_hash,terms=terms))
    if stale:db.commit()


SECTION = re.compile(r"(?i)(자격\s*요건|필수\s*(?:요건|사항|조건)|지원\s*자격|requirements|minimum qualifications|우대\s*(?:사항|조건|요건)|preferred qualifications|nice to have|주요\s*업무|담당\s*업무|responsibilities|복리후생|혜택|채용\s*절차|팀\s*소개|기술\s*스택)")

def requirement_mentions(job, skills):
    """Conservative section-based evidence; a mention is not eligibility proof."""
    from bs4 import BeautifulSoup
    raw = job.body_text or ''
    if re.search(r'<(?:p|div|li|br|h[1-6])\b', raw, re.I):
        raw = BeautifulSoup(raw, 'html.parser').get_text('\n')
    parts = SECTION.split(raw)
    category = 'mentioned'
    evidence = []
    for part in parts:
        if SECTION.fullmatch(part):
            lower = part.lower()
            category = ('preferred' if re.search(r'우대|preferred|nice to have', lower)
                        else 'required' if re.search(r'자격|필수|requirements', lower) else 'mentioned')
            continue
        for line in re.split(r'[\n\r]+', part):
            line = line.strip()
            if not line: continue
            for skill in skills:
                match = re.search(skill_pattern(skill), line)
                if not match: continue
                kind = category
                # Explicit sentence-level preference overrides the surrounding section.
                if re.search(r'우대|preferred|nice to have', line, re.I): kind = 'preferred'
                elif re.search(r'필수|must have|required', line, re.I): kind = 'required'
                snippet = line[max(0, match.start()-70):match.end()+150]
                item = {'skill': skill, 'category': kind, 'evidence': snippet}
                if item not in evidence: evidence.append(item)
    for skill in skills:
        if re.search(skill_pattern(skill), job.title or '') and not any(e['skill']==skill for e in evidence):
            evidence.append({'skill':skill, 'category':'mentioned', 'evidence':job.title})
    return {'required': sorted({e['skill'] for e in evidence if e['category']=='required'}),
            'preferred': sorted({e['skill'] for e in evidence if e['category']=='preferred'}),
            'mentioned': sorted({e['skill'] for e in evidence if e['category']=='mentioned'}),
            'evidence': evidence[:100], 'method':'section_and_explicit_wording'}
