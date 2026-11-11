"""Conservative, reproducible document evidence; similarity is never eligibility.

Only selected document versions are read. Quotes address exact character ranges
in the stored text. Absence of a quote is unknown, not absence of experience.
"""
import re
from collections import Counter

from .skill_matching import CATALOG, skill_pattern

VERSION = 'qualification-evidence-v1'
LABELS = {'supported': '충족 근거 있음', 'mismatch': '불일치',
          'unknown': '미확인', 'optional': '필수 아님'}
EXTRA_TERMS = {
    'JPA': r'\bJPA\b', 'Hibernate': r'\bHibernate\b', 'Querydsl': r'\bQuerydsl\b',
    'TypeORM': r'\bTypeORM\b', 'MongoDB': r'\bMongoDB\b', 'Git': r'\bGit(?:Hub)?\b|깃허브',
    'REST API': r'REST(?:ful)?\s*API', 'HTTP': r'\bHTTPS?\b',
    'RDBMS': r'RDBMS|관계형\s*(?:데이터베이스|DB)',
    'ORM': r'\bORM\b', 'MyBatis': r'\bMyBatis\b', 'Oracle': r'\bOracle\b',
    'RabbitMQ': r'\bRabbitMQ\b', 'GraphQL': r'\bGraphQL\b',
    'Kotlin': r'\bKotlin\b|코틀린', 'Spring Boot': r'Spring\s*Boot|스프링\s*부트',
    'Spring MVC': r'Spring\s*MVC',
}
TERMS = {**{name: skill_pattern(name) for name in CATALOG}, **EXTRA_TERMS}
# Explicitly named implementations can support a generic technology family.
FAMILY = {'RDBMS': r'RDBMS|관계형|MySQL|PostgreSQL|Oracle|MariaDB|외래\s*키|foreign key',
          'ORM': r'\bORM\b|JPA|Hibernate|TypeORM|Prisma',
          'SQL': r'\bSQL\b|쿼리|query', 'Git': r'\bGit(?:Hub)?\b|\bPR\s*#?\d|깃허브'}
ACTION = re.compile(r'(?:구현|개발|설계|구축|배포|검증|테스트|수정|개선|적용|분리|구성|처리|작성|담당|활용|사용|운영)(?:했|하였|하고|하여|한\s|하는|해\s|합니다)|(?:API|서버|기능|환경|테스트|캐시|관계|아키텍처|아키텍쳐|모델링)[^\n]{0,60}(?:구현|설계|구축|작성|개선)(?:\s|$)|built|implemented|developed|designed|tested|deployed', re.I)
UNPROVEN = re.compile(r'경험(?:이|은|도)?\s*없|사용해\s*보지|구현하지\s*않|구성\s*없이|미구현|학습\s*예정|배울\s*예정|도입\s*예정|계획(?:입니다|하고)|no experience|not implemented|plan to|want to|yet to', re.I)
LIMITATION = re.compile(r'아직|한계|남아|남았|필요합니다|예비\s*측정|조건별\s*1회|각각\s*한\s*번|보장하지|포함하지|추가\s*(?:측정|검증)|로컬', re.I)
HARD = re.compile(r'제출|첨부|실무|상용|산업기능|전문연구|병역|비자|취업\s*자격|재학|졸업|학사|석사|박사|학위|전공|영어|영문|어학|TOEIC|TOEFL|출근|거주|근무\s*가능|professional|commercial|production experience|degree|bachelor|master|ph\.?d|fluent|visa|work authorization', re.I)
QUALITATIVE = re.compile(r'깊은|능숙|능통|탁월|전문적|고도화|확장성|대규모|대용량|숙련|높은\s*이해|탄탄|proficien|expert|strong|deep|large.scale|at scale', re.I)
YEAR = re.compile(r'(\d+)\s*년\s*이상|(?:(\d+)\s*\+\s*years)|(?:at least\s+(\d+)\s*years)', re.I)
TOPICS = [
    ('cache', r'캐시|cach', '캐시의 범위·키·만료·실패 응답 처리와 요청 격리', '변경 전후 호출 수와 응답 시간, 정상·실패 경로의 반복 측정'),
    ('query', r'인덱스|쿼리\s*튜닝|실행\s*계획|query.*optim|query.*tun', '느린 쿼리·실행 계획·인덱스 선택', '동일 데이터에서 EXPLAIN과 지연시간을 반복 비교한 기록'),
    ('database', r'RDBMS|ORM|JPA|TypeORM|데이터\s*모델|DB\s*구조|데이터베이스|트랜잭션|SQL', '스키마·관계·트랜잭션과 본인이 바꾼 데이터 처리', '관계도와 소유권·삭제·롤백 경계 테스트, 조회 시간을 포함한 측정 범위'),
    ('messaging', r'Kafka|메시지|메세지|메시징|분산|동시성|비동기|async|RabbitMQ', '메시지 흐름·순서·저장 완료·재시도 책임', '파티션 키와 commit 시점, 중복·유실·소비 지연 시나리오의 테스트와 남은 한계'),
    ('testing', r'테스트|test|품질|검증', '본인이 작성한 테스트와 발견·수정한 결함', '단위·통합·E2E 구분, 실패 재현 입력과 CI 실행 근거'),
    ('deployment', r'배포|인프라|클라우드|AWS|NCP|Docker|Kubernetes|CI/CD|Linux|Nginx|운영', '개발·운영·CI 환경과 본인 배포 책임', '네트워크·DB 접근 경계, 배포 실패·롤백·관측 방법과 운영 기간'),
    ('security', r'인증|인가|권한|보안|auth|security', '요청자·소유권·권한 검증 위치', '정상·다른 사용자·만료 토큰의 실패 경로와 데이터 보존 테스트'),
    ('api', r'API|HTTP|백엔드|Spring|NestJS|Node|Django|FastAPI', '본인이 구현한 요청·응답과 서비스 경계', 'API 계약, 오류 처리·동시 요청·DB 변경 경계 및 테스트 링크'),
    ('collaboration', r'협업|소통|커뮤니케이션|Git|리뷰|communication|collaborat', '팀 구성·본인 책임과 조율한 API 변경', 'PR·리뷰·합의 기록과 변경 전후 계약, 본인 기여 범위'),
]


def present_terms(text):
    terms = [name for name, pattern in TERMS.items() if re.search(pattern, text, re.I)]
    if 'Spring Boot' in terms or 'Spring MVC' in terms:
        terms = [t for t in terms if t != 'Spring']
    return terms


def unrecognized_terms(text):
    residual = text
    for pattern in TERMS.values():
        residual = re.sub(pattern, ' ', residual, flags=re.I)
    generic = {'API','REST','RESTful','Database','Framework','Backend','Back-end','Front-end',
               'Experience','Qualifications','Required','Preferred','Software','Engineer','Development','Web','IT','CS'}
    return sorted({word for word in re.findall(r'\b[A-Z][A-Za-z0-9+#./-]*\b', residual) if word not in generic})


class EvidencePassages(list):
    def __init__(self):
        super().__init__()
        self.cache = {}


def document_passages(documents):
    """At most three neighbouring lines, within one PDF page, with exact offsets."""
    passages = EvidencePassages()
    for doc in documents:
        text = doc['text']
        page = None
        lines = list(re.finditer(r'[^\r\n]+', text))
        for i, line in enumerate(lines):
            marker = re.fullmatch(r'\[페이지 (\d+)\]', line.group().strip())
            if marker:
                page = int(marker[1]); continue
            if len(line.group().strip()) < 8:
                continue
            for width in (1, 2, 3):
                selected = lines[i:i+width]
                if len(selected) != width or any('[페이지 ' in x.group() for x in selected):
                    break
                start, end = line.start(), selected[-1].end()
                quote = text[start:end]
                if len(quote) > 420:
                    break
                passages.append({'document_id': doc['id'], 'document_title': doc['title'],
                    'kind': doc['kind'], 'variant': doc['variant'], 'content_hash': doc['content_hash'],
                    'page': page, 'start': start, 'end': end, 'text': quote})
    return passages


def find_evidence(pattern, passages, *, action=True, context=None):
    key = (pattern, action, context)
    if hasattr(passages, 'cache') and key in passages.cache:
        return passages.cache[key]
    hits = []
    for p in passages:
        text = p['text']
        # Stack lists and project headings are mentions, even if a neighbouring
        # line happens to contain an action about an unrelated implementation.
        substantive = '\n'.join(line for line in text.splitlines() if not re.search(
            r'^\s*(?:활용\s*기술|기술\s*[·:：]|Back-End:|Database:|DevOps:)|^[^\n]*\s[·/]\s[^\n]*\s[·/]\s', line, re.I)) if action else text
        if not re.search(pattern, substantive, re.I) or UNPROVEN.search(text):
            continue
        if action and not ACTION.search(substantive):
            continue
        hits.append(p)
    # A short direct passage is preferable to a coincidental long window.
    ranked = sorted(hits, key=lambda p: (bool(context) and not bool(re.search(context, p['text'], re.I)),
                                        not bool(re.search(r'[.!?。]\s*$', p['text'])), len(p['text']), p['document_id']))
    result = []
    for hit in ranked:
        if not any(e['kind'] == hit['kind'] for e in result):
            result.append(hit)
        if len(result) == 2:
            break
    if hasattr(passages, 'cache'):
        passages.cache[key] = result
    return result


def logic_for(text, terms):
    positions = [re.search(TERMS[t], text, re.I) for t in terms]
    if len(positions) < 2:
        return 'single'
    region = text[min(m.start() for m in positions):max(m.end() for m in positions)]
    alternative = re.search(r'또는|혹은|\bor\b', region, re.I) or re.search(r'중\s*(?:하나|한\s*개)|하나\s*이상|at least one|any of', text, re.I)
    if alternative:
        return 'mixed' if re.search(r'및|\band\b', region, re.I) else 'any'
    if re.search(r'등(?:의)?(?:\s|[)）])', text):
        families = [({'MySQL', 'PostgreSQL', 'Oracle', 'RDBMS'}, r'RDBMS|관계형'),
                    ({'JPA', 'Hibernate', 'Querydsl', 'TypeORM', 'ORM'}, r'ORM'),
                    ({'Java', 'Python', 'Kotlin', 'Go', 'JavaScript', 'TypeScript', 'C++', 'C#'}, r'언어|language')]
        if any(set(terms) <= family and re.search(pattern, text, re.I) for family, pattern in families):
            return 'examples'
        # "etc." in a heterogeneous stack is not permission to satisfy just
        # one item. The full sentence remains unknown if any item lacks proof.
    return 'all'


def assess_requirement(row, category, passages, candidate_facts=None):
    text = row['text']
    terms = present_terms(text)
    unknown_terms = unrecognized_terms(text)
    mode = logic_for(text, terms)
    evidence, matched, mentions = [], [], []
    context = r'스키마|관계|외래\s*키|ERD|모델링|cascade' if re.search(r'RDBMS.*(?:구조|설계)|스키마|데이터\s*모델|ORM', text, re.I) else None
    for term in terms:
        pattern = FAMILY.get(term, TERMS[term])
        hits = find_evidence(pattern, passages, context=context)
        if hits:
            matched.append(term); evidence.extend(hits)
        elif find_evidence(pattern, passages, action=False):
            mentions.append(term)
    evidence = list({(e['document_id'], e['start'], e['end']): e for e in evidence}.values())
    state = 'unknown'
    reason = '문서에서 이 조건 전체를 입증할 근거를 확인하지 못했습니다. 경험이 없다는 뜻은 아닙니다.'
    hard = bool(HARD.search(text) or YEAR.search(text))
    candidate_fact = None
    if row.get('non_required'):
        state, reason = 'optional', '원문이 필수가 아님을 명시합니다.'
    elif YEAR.search(text):
        # Only an explicit total of PROFESSIONAL years may contradict a minimum.
        minimum = int(next(v for v in YEAR.search(text).groups() if v is not None))
        facts = []
        for p in passages:
            for m in re.finditer(r'(?:총\s*)?실무\s*경력\s*[:：]\s*(\d+)\s*년', p['text']):
                facts.append((int(m[1]), p))
        values = {v for v, _ in facts}
        confirmed_months = (candidate_facts or {}).get('professional_months')
        professional = bool(re.search(r'경력|실무|professional|industry|commercial|work experience', text, re.I)) and not bool(re.search(r'프로젝트\s*포함|project.*count', text, re.I))
        if professional and confirmed_months is not None and confirmed_months < minimum*12:
            state, reason = 'mismatch', f'사용자가 확인한 백엔드 실무 경력 {confirmed_months}개월이 최소 {minimum}년에 못 미칩니다.'
            candidate_fact = candidate_facts
            evidence = []
        elif professional and len(values) == 1 and next(iter(values)) < minimum and confirmed_months is None:
            state, reason = 'mismatch', f'문서에 명시한 실무 경력이 공고의 최소 {minimum}년에 못 미칩니다.'
            evidence = [facts[0][1]]
        else:
            reason = f'최소 {minimum}년 경력 조건입니다. 프로젝트·교육 기간은 실무 경력으로 합산하지 않았습니다.'
    elif re.search(r'4년제|학사|bachelor', text, re.I) and not re.search(r'석사|박사|전공|재학|졸업\s*예정자만', text):
        degree = r'공학사|문학사|이학사|학사\s*(?:취득|졸업)|bachelor|(?=[\s\S]*학사)(?=[\s\S]*졸업)'
        pattern = degree
        if re.search(r'학과|관련\s*학위|computer science', text, re.I):
            if re.search(r'컴퓨터|소프트웨어|computer science', text, re.I):
                pattern = r'(?=[\s\S]*(?:컴퓨터|소프트웨어|computer science))(?=[\s\S]*(?:' + degree + r'))'
            else:
                pattern = r'(?!)'
        hits = [e for e in find_evidence(pattern, passages, action=False)
                if not re.search(r'졸업\s*예정|재학|진학\s*예정', e['text'])]
        if hits:
            state, reason, evidence = 'supported', '선택 문서에 학사 학위 근거가 명시되어 있습니다.', hits[:1]
    elif not hard and not QUALITATIVE.search(text) and terms and mode != 'mixed' and not unknown_terms:
        enough = bool(matched) if mode in ('any', 'examples') else len(matched) == len(terms)
        # Understanding/aptitude is not demonstrated merely by using a tool.
        if enough and not re.search(r'이해|기본기|역량|능력|관심|선호|숙지|구조|설계|성능|최적화|knowledge|understand|design|performan|optimi', text, re.I):
            state = 'supported'
            reason = '요구 기술을 사용한 구체적 행동이 선택 문서에 있습니다. 기술 숙련도나 채용 판단까지 보장하지 않습니다.'
        elif matched:
            reason = '관련 구현 근거는 있으나 요구하는 이해 수준·복합 조건 전체의 충족 여부는 추가 확인이 필요합니다.'
    if mode == 'mixed' and state == 'unknown':
        reason = '및/또는이 섞인 복합 조건입니다. 일부 기술의 일치로 전체 충족을 판정하지 않았습니다.'
    elif unknown_terms and state == 'unknown' and not hard:
        reason = '자동 분석이 해석하지 못한 기술·표현이 포함되어 있습니다. 알려진 기술 일부만으로 전체 충족을 판정하지 않았습니다.'
    if not evidence and not hard and not terms:
        for topic, pattern, _, _ in TOPICS:
            if topic != 'api' and re.search(pattern, text, re.I):
                evidence = find_evidence(pattern, passages)[:1]
                if evidence:
                    break
    return {'requirement': text, 'category': category, 'state': state, 'label': LABELS[state],
            'reason': reason, 'hard_condition': hard, 'logic': mode,
            'terms': terms, 'matched_terms': matched, 'mention_only': mentions,
            'unrecognized_terms': unknown_terms,
            'unconfirmed_terms': [t for t in terms if t not in matched],
            'source': {k: row.get(k) for k in ('text', 'start', 'end', 'source_url', 'provenance')},
            'candidate_fact': candidate_fact,
            'evidence': evidence[:4]}


def improvement(assessment, passages=None):
    text = assessment['requirement']
    topic = next((t for t in TOPICS if re.search(t[1], text, re.I)), None)
    evidence = assessment['evidence']
    if topic:
        # A multi-technology requirement can have evidence about another topic.
        # Locate a passage about this advice instead of pointing to that quote.
        evidence = [e for e in evidence if re.search(topic[1], e['text'], re.I)]
        if not evidence and passages is not None:
            evidence = find_evidence(topic[1], passages)
    location = (f"{evidence[0]['document_title']}" + (f" {evidence[0]['page']}쪽" if evidence[0]['page'] else '')) if evidence else '관련 프로젝트 항목'
    if assessment['hard_condition']:
        resume = '경력·학력·근무 가능 여부를 사실대로 명시하고 공고의 인정 범위를 확인하세요. 프로젝트 기간을 재직 기간으로 바꾸지 마세요.'
        portfolio = '작품 설명만으로 경력·학위·자격 제한을 해소할 수 없습니다. 해당 자격의 별도 증빙이 있는지 확인하세요.'
    elif topic:
        resume = (f'{location}의 경험에서 {topic[2]}를 본인 역할 중심으로 1~2문장에 연결하세요.' if evidence else
                  f'선택 문서에서 {topic[2]}의 구체적 근거를 찾지 못했습니다. 실제 경험이 있다면 해당 프로젝트와 본인 역할을 명시하세요.')
        portfolio = f'{topic[3]}을 제시하세요. 이미 있는 자료는 해당 위치를 연결하고, 미측정 값은 만들지 마세요.'
    else:
        resume = f'“{text[:100]}”에 대응하는 본인 행동과 결과를 {location}에 연결하세요. 사실로 확인되지 않은 능력은 추가하지 마세요.'
        portfolio = '이 조건을 보여주는 실제 작업·의사결정·검증 자료가 있는지 확인하고 없으면 보완 과제로 남기세요.'
    limitations = [e for e in evidence if LIMITATION.search(e['text'])]
    return {'category': assessment['category'], 'requirement': text, 'state': assessment['state'],
            'resume_action': resume, 'portfolio_action': portfolio,
            'action': resume + ' ' + portfolio, 'evidence': evidence,
            'limitations': limitations,
            'verification': '실제 경험·코드·측정 기록에 있는 내용만 사용하세요. 유사도는 사실 확인 수단이 아닙니다.'}


def assess_analysis(analysis, documents, passages=None, candidate_facts=None):
    passages = document_passages(documents) if passages is None else passages
    rows, seen = [], set()
    for category in ('required', 'preferred'):
        for row in analysis['sections'][category]:
            normalized = re.sub(r'[\W_]+', '', row['text']).casefold()
            if (category, normalized) in seen:
                continue
            seen.add((category, normalized))
            rows.append(assess_requirement(row, category, passages, candidate_facts))
    minimum = analysis['career']['min_years']
    # A title/listing may state years even when the requirements section omits it.
    if minimum and not any(YEAR.search(r['requirement']) for r in rows if r['category'] == 'required'):
        title_evidence = next((s for s in analysis['career']['evidence'] if str(minimum) in s), analysis['title'])
        row = assess_requirement({'text': f'{minimum}년 이상 경력 / {title_evidence}',
                'source_url': analysis['source_url']}, 'required', passages, candidate_facts)
        row['source']['text'] = title_evidence
        row['source']['provenance'] = 'career_extraction'
        rows.append(row)
    required = [r for r in rows if r['category'] == 'required' and r['state'] != 'optional']
    counts = dict(Counter(r['state'] for r in required))
    hard_unknown = [r for r in required if r['hard_condition'] and r['state'] == 'unknown']
    if counts.get('mismatch'):
        priority, reason = '현재 후순위', '명시된 필수 조건과 문서의 사실이 불일치합니다.'
    elif not required or hard_unknown or analysis.get('quality_warnings'):
        priority, reason = '조건 확인 필요', '자격요건 원문 또는 경력·학력 등 지원 자격을 먼저 확인해야 합니다.'
        if analysis.get('quality_warnings'):
            reason = '공고의 날짜·구역 표현에 확인이 필요한 문제가 있습니다. 원문에서 해소한 뒤 지원 순서를 결정하세요.'
    elif counts.get('supported', 0) >= max(1, len(required) * .6):
        priority, reason = '먼저 지원', '필수 조건의 다수에 문서 근거가 있고 확인된 불일치가 없습니다. 미확인 조건은 지원 전 점검하세요.'
    else:
        priority, reason = '보완 후 지원', '필수 조건과 연결되는 구체적 설명·검증 근거를 먼저 보완하세요.'
    ordered = sorted(rows, key=lambda r: (r['category'] != 'required', r['state'] == 'supported'))
    resume_supported = sum(r['state'] == 'supported' and any(e['kind'] == 'resume' for e in r['evidence']) for r in required)
    return {'method': VERSION, 'priority': priority, 'priority_reason': reason,
            'resume_supported': resume_supported,
            'quality_warnings': analysis.get('quality_warnings', []),
            'required_counts': {s: counts.get(s, 0) for s in ('supported', 'mismatch', 'unknown')},
            'requirements': rows, 'guidance': [improvement(r, passages) for r in ordered],
            'missing_required_section': not bool(analysis['sections']['required']),
            'note': '선택 문서에 적힌 근거를 판정했습니다. 문서 미기재는 자격 불일치가 아니며, 채용 적합성·합격 확률을 계산하지 않습니다.'}
