"""Explainable technology mentions, not an assessment of applicant eligibility."""
import re
import threading
from functools import lru_cache
from sqlalchemy import case, func, select, exists, or_
from .database import Job, JobSkillIndex, AnalysisRevision

SKILL_INDEX_VERSION='technology-mentions-v6'

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
BACKEND_CORE=r'백엔드|back[ -]?end|서버|server|인프라|infra|devops|\bsre\b|풀스택|full[ -]?stack'
OTHER_ROLE=r'프론트엔드|front[ -]?end|모바일|mobile|\bandroid\b|\bios\b|\b[sw]?qa\b|quality assurance|테스트\s*엔지니어|data\s*engineer|데이터\s*엔지니어|compiler|컴파일러|코덱|graphics|그래픽|embedded|임베디드|hardware|하드웨어|firmware|펌웨어'
NON_ENGINEERING=r'변리사|사업개발|사업\s*개발|기획|마케팅|영업|sales|marketing|business development|recruiter|디자이너|designer'
ENGINEERING=r'개발|엔지니어|engineer|developer|devops|\bsre\b|administrator|관리자'
# Generic software/platform titles remain candidates, but an explicit frontend,
# mobile, QA or data role must not match solely through "software"/"platform".
BACKEND_TITLE=rf'(?i)^(?:[^|]*\|)?(?!.*(?:{NON_ENGINEERING}))(?:(?:.*(?:{BACKEND_CORE}))|(?!(?:.*(?:{OTHER_ROLE})))(?=.*(?:{ENGINEERING})).*(?:플랫폼|platform|클라우드|cloud|시스템|system|소프트웨어|software))'
INFRA_TITLE=rf'(?i)^(?:[^|]*\|)?(?!.*(?:{NON_ENGINEERING}))(?:(?:.*(?:인프라|infra|devops|\bsre\b))|(?!(?:.*(?:{OTHER_ROLE})))(?=.*(?:{ENGINEERING})).*(?:클라우드|cloud|플랫폼|platform|시스템|system))'


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
    parts=[re.escape(a)+(r'(?!스크립트)' if name=='Java' and a=='자바' else '')+
           (r'(?!\s+(?:further|beyond|above and beyond|the extra mile|hand in hand)\b)' if name=='Go' and a=='go' else '')
           for a in aliases]
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
    revision=db.get(AnalysisRevision,'skill_index')
    rebuild=revision is None or revision.version!=SKILL_INDEX_VERSION
    statement=select(Job)
    if not rebuild:
        statement=statement.outerjoin(JobSkillIndex,JobSkillIndex.job_id==Job.id).where(
            or_(JobSkillIndex.job_id.is_(None),JobSkillIndex.content_hash!=Job.content_hash))
    stale=db.scalars(statement).all()
    for job in stale:
        text=(job.title or '')+'\n'+(job.body_text or '')
        terms='|'+'|'.join(name.casefold() for name in CATALOG if re.search(skill_pattern(name),text))+'|'
        row=db.get(JobSkillIndex,job.id)
        if row:row.content_hash=job.content_hash;row.terms=terms
        else:db.add(JobSkillIndex(job_id=job.id,content_hash=job.content_hash,terms=terms))
    if rebuild:db.merge(AnalysisRevision(key='skill_index',version=SKILL_INDEX_VERSION))
    if stale or rebuild:db.commit()


SECTION = re.compile(r"(?i)(자격\s*요건|필수\s*(?:요건|사항|조건)|지원\s*자격|requirements|minimum qualifications|우대\s*(?:사항|조건|요건)|preferred qualifications|nice to have|주요\s*업무|담당\s*업무|responsibilities|복리후생|혜택|채용\s*절차|팀\s*소개|기술\s*스택)")

def requirement_mentions(job, skills):
    """Conservative section-based evidence; a mention is not eligibility proof."""
    from .posting_sections import extract_sections
    evidence = []
    for section, rows in extract_sections(job).items():
        for row in rows:
            category=section if section in ('required','preferred') else 'mentioned'
            if row.get('non_required') and category=='required':category='mentioned'
            line=row['text']
            for skill in skills:
                match=re.search(skill_pattern(skill),line)
                if not match:continue
                snippet=line[max(0,match.start()-70):match.end()+150]
                item={'skill':skill,'category':category,'evidence':snippet,
                      'alternative':row['alternative'],'non_required':row.get('non_required',False)}
                if item not in evidence:evidence.append(item)
    for skill in skills:
        if re.search(skill_pattern(skill), job.title or '') and not any(e['skill']==skill for e in evidence):
            evidence.append({'skill':skill, 'category':'mentioned', 'evidence':job.title})
    return {'required': sorted({e['skill'] for e in evidence if e['category']=='required'}),
            'preferred': sorted({e['skill'] for e in evidence if e['category']=='preferred'}),
            'mentioned': sorted({e['skill'] for e in evidence if e['category']=='mentioned'}),
            'evidence': evidence[:100], 'method':'section_and_explicit_wording'}
