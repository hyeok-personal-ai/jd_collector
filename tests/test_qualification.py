from app.qualification import assess_requirement, assess_analysis, document_passages
from app.posting_analysis import structured_analysis
from test_posting_analysis import sample


def documents(text):
    return [{'id': 1, 'title': 'Synthetic resume', 'kind': 'resume', 'variant': 'Java',
             'content_hash': 'synthetic-hash', 'text': text}]


def assess(requirement, text):
    return assess_requirement({'text': requirement, 'start': 0, 'end': len(requirement),
        'source_url': 'https://example.org/job'}, 'required', document_passages(documents(text)))


def test_absence_is_unknown_and_skill_mention_is_not_experience():
    assert assess('Java 개발 경험', 'Python API를 개발했습니다.')['state'] == 'unknown'
    assert assess('Java 개발 경험', '기술 목록: Java, Spring, Python')['state'] == 'unknown'
    result = assess('Java 개발 경험', '[페이지 1]\nJava API를 구현하고 오류 경로를 테스트했습니다.')
    assert result['state'] == 'supported'
    assert result['evidence'][0]['page'] == 1
    assert assess('Java 개발 경험', 'Java 사용 경험은 없습니다. 앞으로 학습 예정입니다.')['state'] == 'unknown'
    assert assess('Java 개발 경험', '기술 · Java · Spring · Docker\n요청 오류 경로를 테스트했습니다.')['state'] == 'unknown'
    assert assess('Spring 개발 경험', '스프링 프로젝트 구성 없이도 API 연동 환경을 구축했습니다.')['state'] == 'unknown'
    assert assess('Java 개발 경험', 'Java·Spring으로 채팅의 전송·저장·조회\n흐름을 구현했습니다.')['state'] == 'supported'


def test_or_between_tools_is_not_or_between_actions():
    assert assess('Python 또는 Java 개발 경험', 'Java API를 구현했습니다.')['state'] == 'supported'
    result = assess('Docker, Kubernetes 구축 또는 운영 경험', 'Docker 배포 환경을 구축했습니다.')
    assert result['logic'] == 'all' and result['state'] == 'unknown'
    result = assess('Java 또는 Python 및 AWS 개발 경험', 'Java API를 구현했습니다.')
    assert result['logic'] == 'mixed' and result['state'] == 'unknown'
    result = assess('Spring, Docker, Kubernetes 등 기술 활용 경험', 'Spring API를 구현했습니다.')
    assert result['state'] == 'unknown'
    assert assess('JPA, Hibernate, Querydsl 등 ORM 경험', 'JPA를 사용하여 API를 구현했습니다.')['state'] == 'supported'
    assert assess('Spring Boot 또는 Spring MVC 개발 경험', 'Spring API를 구현했습니다.')['state'] == 'unknown'
    result = assess('Spring, MySQL, Redis, Airflow, IaC, Grafana 등 기술 활용',
                    'Spring API를 구현하고 MySQL과 Redis를 사용했습니다.')
    assert result['state']=='unknown' and 'Airflow' in result['unrecognized_terms']


def test_professional_years_never_inferred_from_projects():
    assert assess('백엔드 실무 경력 3년 이상', '프로젝트 기간 2020~2025. Java API 개발.')['state'] == 'unknown'
    result = assess('백엔드 실무 경력 3년 이상', '실무 경력: 0년. Java API 프로젝트를 개발했습니다.')
    assert result['state'] == 'mismatch' and result['evidence']
    result = assess('백엔드 실무 경력 3년 이상', '실무 경력: 0년\n실무 경력: 4년')
    assert result['state'] == 'unknown'
    result = assess('백엔드 실무 경력 3년 이상', '상품 추천 기능 및 알림 API 구현')
    assert result['evidence'] == []
    p = document_passages(documents('프로젝트에서 Java API를 구현했습니다.'))
    facts = {'professional_months':0, 'source':'user_confirmed', 'updated_at':1}
    r = assess_requirement({'text':'백엔드 실무 경력 1년 이상'},'required',p,facts)
    assert r['state']=='mismatch' and r['candidate_fact']==facts
    assert assess_requirement({'text':'Java 개발 경험 1년 이상'},'required',p,facts)['state']=='unknown'


def test_database_usage_does_not_prove_schema_design_or_scalability():
    r = assess('RDBMS 구조를 설계하고 확장성 있는 DB 구조를 고민해 보신 분',
               'MySQL을 사용한 API를 구현했습니다.')
    assert r['state'] == 'unknown'


def test_degree_and_proficiency_require_more_than_tool_matching():
    assert assess('4년제 대학 졸업자 이상', '2025.02 졸업 · 소프트웨어전공 공학사')['state'] == 'supported'
    assert assess('석사 이상 졸업', '2025.02 졸업 · 소프트웨어전공 공학사')['state'] == 'unknown'
    assert assess('Java에 대한 깊은 이해', 'Java API를 구현했습니다.')['state'] == 'unknown'
    assert assess('Java 상용 서비스 실무 경험', 'Java API를 구현했습니다.')['state'] == 'unknown'
    assert assess('컴퓨터 관련 학과의 학사 학위 보유', '한국역사학과 문학사 졸업')['state'] == 'unknown'
    assert assess('컴퓨터 관련 학과의 학사 학위 보유', '소프트웨어전공 공학사(컴퓨터공학심화)')['state'] == 'supported'
    assert assess('4년제 대학 졸업자', '한국역사학과 학사 (2019~2025)\n한국역사학과 졸업')['state'] == 'supported'
    assert assess('컴퓨터 관련 학과의 학사 학위 보유', '한국역사학과 학사\n한국역사학과 졸업\n소프트웨어전공 학사 (2022~2025)\n소프트웨어전공 복수 학위자로 졸업')['state'] == 'supported'
    assert assess('4년제 대학 졸업자', '소프트웨어전공 학사 과정 재학 · 2027년 졸업 예정')['state'] == 'unknown'


def test_exact_quotes_source_offsets_and_version_isolation():
    text = '[페이지 1]\nJava와 Spring API를 구현했습니다.\n[페이지 2]\nKafka 사용은 학습 예정입니다.'
    docs = documents(text)
    passages = document_passages(docs)
    for passage in passages:
        assert text[passage['start']:passage['end']] == passage['text']
        assert '[페이지' not in passage['text']
        assert passage['content_hash'] == 'synthetic-hash'
    assert assess('Kafka 사용 경험', text)['state'] == 'unknown'
    other = documents('Node.js API를 개발했습니다.')
    assert assess_analysis(structured_analysis(sample(body_text='자격요건\nJava 개발 경험')), other)['required_counts']['supported'] == 0


def test_detailed_zighang_requirements_override_summary_without_losing_offsets():
    text = '자격요건\nJava 개발 경험\n소개\n회사 설명\n자격요건\nJava 개발 실무 경력 3년 이상\n4년제 졸업자\n우대사항\nRedis 운영 경험'
    result = structured_analysis(sample(source_id='zighang', body_text=text))
    assert len(result['sections']['required']) == 2
    assert result['career']['min_years'] == 3
    for row in result['sections']['required']:
        assert text[row['start']:row['end']] == row['text']
    assessment = assess_analysis(result, documents('Java API 개발 프로젝트 5년. 대학 졸업 여부 미기재.'))
    assert assessment['priority'] == '조건 확인 필요'
    assert assessment['required_counts']['mismatch'] == 0


def test_missing_required_section_cannot_receive_apply_first():
    result = assess_analysis(structured_analysis(sample(body_text='Python 서비스를 만듭니다.')),
                             documents('Python API 개발과 테스트를 맡았습니다.'))
    assert result['priority'] == '조건 확인 필요'
    assert result['missing_required_section']


def test_conflicting_deadline_and_qualification_like_duties_are_visible():
    from datetime import date
    text = '자격요건\nJava 개발 경험\n담당 업무\nSpring 개발 경험자\n채용 절차\n접수기간: 2026년 10월 26일 ~ 2026년 11월 13일'
    a = structured_analysis(sample(body_text=text, deadline_on=date(2026,11,17)))
    assert {w['code'] for w in a['quality_warnings']} == {'deadline_conflict','qualification_in_duties'}
    for w in a['quality_warnings']:
        assert text[w['start']:w['end']] == w['evidence']
    result = assess_analysis(a, documents('Java와 Spring API를 구현하고 오류 경로를 테스트했습니다.'))
    assert result['priority'] == '조건 확인 필요'


def test_preferred_experience_heading_does_not_create_required_years():
    a = structured_analysis(sample(body_text='Requirements\nJava development experience\nPreferred Experience\n5+ years of industry experience'))
    assert a['career']['min_years'] is None
    assert a['sections']['preferred'][0]['text'].startswith('5+')
    a = structured_analysis(sample(body_text='자격요건\n【 이런 분을 찾고 있습니다 】\nJava 개발 경험'))
    assert [r['text'] for r in a['sections']['required']] == ['Java 개발 경험']
    a = structured_analysis(sample(body_text='Qualifications\nPython development experience\nHelpful experience (not required)\nKubernetes operations\nLife & Benefit\n연간 도서비 지원\nJoining Process\n경력직 면접 3단계'))
    assert len(a['sections']['required'])==1
    assert a['technologies']['preferred']==['Kubernetes']
    assert len(a['sections']['conditions'])==1


def test_guidance_is_specific_and_retains_measured_scope():
    a = structured_analysis(sample(body_text='자격요건\nRedis 캐시를 활용한 성능 개선 경험'))
    text = 'Redis 캐시를 구현했습니다. 로컬 예비 측정이며 정상 응답의 추가 측정이 필요합니다.'
    result = assess_analysis(a, documents(text))
    guide = result['guidance'][0]
    assert '키·만료' in guide['resume_action']
    assert '정상·실패' in guide['portfolio_action']
    assert guide['limitations']


def test_guidance_uses_evidence_for_its_own_topic():
    from app.qualification import improvement
    unrelated = document_passages(documents('[페이지 1] Java Spring API를 구현했습니다.'))
    row = assess('HTTP, SQL, Git 이해', 'Git PR 리뷰를 진행하고 API를 구현했습니다.')
    advice = improvement(row, unrelated)
    assert not advice['evidence']
    assert '근거를 찾지 못했습니다' in advice['resume_action']
    relevant = document_passages(documents('[페이지 2]\nPostgreSQL 데이터베이스의 관계와 삭제 경계를 설계했습니다.'))
    advice = improvement(row, relevant)
    assert advice['evidence'] and advice['evidence'][0]['page'] == 2
    assert '2쪽' in advice['resume_action']


def test_collaboration_stack_does_not_borrow_framework_action():
    row = assess('Git 활용 경험', 'CollaborationTool: GitHub, Figma, Notion\nJava Spring과 Django 프레임워크를 활용했습니다.')
    assert row['state']=='unknown' and not row['evidence']


def test_decorated_preferred_heading_is_not_a_requirement():
    a = structured_analysis(sample(body_text='자격요건\nJava 개발 경험\n【 이런 경험이 있으면 더 좋습니다 】\nAWS 배포 경험'))
    assert [r['text'] for r in a['sections']['preferred']] == ['AWS 배포 경험']


def test_repository_url_does_not_prove_git_collaboration():
    row = assess('Git 활용 경험', 'AWS 서버 배포와 테스트 환경을 구축했습니다.\n링크: https://github.com/example/project')
    assert row['state']=='unknown' and not row['evidence']
    assert assess('Git 활용 경험', 'Git 브랜치와 PR 리뷰로 API 변경을 관리했습니다.')['state']=='supported'
