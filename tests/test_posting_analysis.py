from types import SimpleNamespace
from app.posting_analysis import structured_analysis,compare_postings
from app.posting_sections import plain_body
from test_skill_matching import client


def sample(**changes):
    values=dict(id=1,title='백엔드 개발자',organization='Example',original_url='https://example.org/1',
                body_text='주요 업무\nAPI 설계\nRequired qualifications\nPython 또는 Java 개발 경력 2년 이상\n우대사항\nRedis 운영\n복리후생\nDocker 교육 지원',
                recruitment_type=None,employment_type=None,region=None,deadline_on=None,deadline_text=None,source_status='open')
    return SimpleNamespace(**{**values,**changes})


def test_analysis_keeps_exact_source_spans_and_alternatives():
    job=sample();result=structured_analysis(job);raw=plain_body(job)
    assert result['technologies']['required']==['Java','Python']
    assert result['technologies']['preferred']==['Redis']
    assert result['technologies']['mentioned']==['Docker']
    assert result['sections']['required'][0]['alternative']
    for rows in result['sections'].values():
        for row in rows:assert raw[row['start']:row['end']]==row['text']
    assert result['career']['min_years']==2
    assert any('2년' in line for line in result['career']['evidence'])


def test_missing_sections_are_unknown_not_empty_requirements():
    result=structured_analysis(sample(body_text='Python 서비스를 만듭니다.'))
    assert 'required' in result['missing_sections']
    assert result['technologies']['required']==[]
    assert result['technologies']['mentioned']==['Python']


def test_flattened_body_splits_explicit_headings_without_promoting_prose():
    text=('회사를 소개합니다. 서비스 운영에 필수적인 Python 도구를 만듭니다. '
          '이런 일을 해요 Redis 서버 운영 이런 분을 찾고 있어요 Java 개발 경험 '
          '이런 분이라면 더 좋아요! AWS 운영 경험 참고해 주세요 채용 전형 우대 안내')
    result=structured_analysis(sample(body_text=text))
    assert result['technologies']['required']==['Java']
    assert result['technologies']['preferred']==['AWS']
    assert result['technologies']['mentioned']==['Python','Redis']
    for rows in result['sections'].values():
        for row in rows:assert text[row['start']:row['end']]==row['text']
    ordinary=structured_analysis(sample(body_text='Python 서비스를 만듭니다. '+('제품 설명입니다. '*30)+'채용 전형에서 우대합니다.'))
    assert ordinary['technologies']['preferred']==[]
    assert ordinary['technologies']['mentioned']==['Python']


def test_short_headings_and_english_plus_boundary():
    result=structured_analysis(sample(body_text='Key Responsibilities\nAPI 개발\n필수\nPython 개발\n우대\nRedis 운영\nPlus if you have\nKafka 경험\n혜택 및 복지\nDocker 교육'))
    assert result['technologies']['required']==['Python']
    assert result['technologies']['preferred']==['Kafka','Redis']
    assert result['technologies']['mentioned']==['Docker']
    assert result['sections']['duties'][0]['text']=='API 개발'


def test_unrestricted_languages_and_negations_are_not_mandatory_technologies():
    result=structured_analysis(sample(body_text='자격요건\n언어 및 프레임워크 제약 없음(Spring, FastAPI, Node.js 등)\n최소 하나의 언어 이해(Python, Java 등 언어 무관)\n우대사항\nRedis 경험'))
    assert result['technologies']['required']==[]
    assert result['technologies']['mentioned']==['FastAPI','Java','Node.js','Python','Spring']
    assert result['sections']['required'][1]['alternative']
    result=structured_analysis(sample(body_text='필수 아님: Python\nRedis is not required\nAWS experience preferred'))
    assert result['technologies']['required']==[]
    assert result['technologies']['preferred']==['AWS']


def test_required_years_recognize_korean_experience_and_exclude_preferences():
    from app.job_labels import label_job
    job=sample(body_text='자격요건\n3년 이상의 서버 개발 및 운영 경험\n우대사항\n10년 이상의 업계 경험\n복리후생\n5년 이상 근속 시 휴가')
    assert label_job(job).min_years==3
    assert label_job(job).career=='early'
    assert structured_analysis(job)['career']['evidence']==['3년 이상의 서버 개발 및 운영 경험']
    job=sample(body_text='자격요건\nPython 개발 경험\n우대사항\n경력 5년 이상')
    assert label_job(job).min_years is None
    assert label_job(job).career=='unknown'


def test_comparison_uses_same_requirement_category():
    result=compare_postings([sample(),sample(id=2,body_text='자격요건\nJava 경험\n우대사항\nPython 경험')])
    assert result['common_technologies']['required']==['Java']
    assert 'Python' not in result['common_technologies']['required']


def test_semantic_duplicate_precheck_rejects_intern_role_and_experience_conflicts():
    from app.duplicate_review import compatible_pair
    base=sample(title='Backend Engineer',body_text='경력 3~5년')
    assert not compatible_pair(base,sample(title='Backend Engineer Intern',body_text=''))
    assert not compatible_pair(base,sample(title='Data Engineer',body_text='경력 3~5년'))
    assert not compatible_pair(base,sample(title='Backend Engineer',body_text='경력 8년 이상'))
    assert compatible_pair(base,sample(title='백엔드 개발자',body_text='경력 3~5년'))


def test_comparison_api_validates_selection_and_returns_evidence(client):
    ids=[j['id'] for j in client.get('/api/jobs').json()['items']]
    assert client.get('/api/compare',params={'ids':','.join(map(str,ids[:2]))}).status_code==200
    assert client.get('/api/compare?ids=1,1').status_code==422
    assert client.get('/api/compare?ids=abc,2').status_code==422
    assert client.get('/api/compare?ids=1,9999').status_code==404
    assert client.get(f'/api/jobs/{ids[0]}/analysis').json()['source_url']


def test_duplicate_grouping_and_split_preserve_jobs_and_records(client):
    jobs=client.get('/api/jobs').json()['items'];ids=[j['id'] for j in jobs[:2]]
    headers={'X-Radar-Request':'1'}
    client.put(f'/api/jobs/{ids[0]}/application',json={'status':'planned','note':'Synthetic note'},headers=headers)
    group=client.post('/api/reviewed-duplicates',json={'ids':ids},headers=headers).json()['id']
    assert len(client.get('/api/reviewed-duplicates').json()['groups'][0]['items'])==2
    assert client.delete('/api/reviewed-duplicates/'+group,headers=headers).status_code==200
    assert client.get('/api/reviewed-duplicates').json()['groups']==[]
    assert client.get('/api/jobs').json()['total']==4
    assert client.get(f'/api/jobs/{ids[0]}/application').json()['note']=='Synthetic note'
    from app.control import ControlStore
    assert tuple(sorted(ids)) in ControlStore().duplicate_reviews()[1]
    client.post('/api/reviewed-duplicates',json={'ids':ids},headers=headers)
    assert tuple(sorted(ids)) not in ControlStore().duplicate_reviews()[1]


def test_conversational_english_headings_separate_requirements_preferences_and_culture():
    body=("Roles and Responsibilities\nOperate APIs\nWhat We're Looking For\n2+ years of Python experience\n"
          "Preferred Skills\nRedis operations\nSupporting Your Best Work and Your Best Life:\nDocker training\n"
          "Example Values:\nGo Further Together\n")
    result=structured_analysis(sample(body_text=body))
    assert result['technologies']['required']==['Python']
    assert result['technologies']['preferred']==['Redis']
    assert result['technologies']['mentioned']==['Docker']
    assert result['career']['min_years']==2
    assert result['sections']['duties'][0]['text']=='Operate APIs'
    assert result['sections']['other'][-1]['text']=='Go Further Together'
    for rows in result['sections'].values():
        for row in rows:assert body[row['start']:row['end']]==row['text']


def test_key_qualifications_and_ideal_years_are_separate():
    result=structured_analysis(sample(body_text='Responsibilities and Opportunities\nSDK development\nKey Qualifications\nPython and C++\nIdeal Qualifications\n5+ years of industry experience\nDocker operations'))
    assert result['technologies']['required']==['C++','Python']
    assert result['technologies']['preferred']==['Docker']
    assert result['career']['min_years'] is None
    assert [r['text'] for r in result['sections']['duties']]==['SDK development']


def test_open_ended_experience_range_and_spelled_out_minimum():
    from app.job_labels import label_job
    result=label_job(sample(body_text='Your Skills and Qualifications\n0-2+ years of data engineering experience.\nYOUR GROWTH\n10 years of company history'))
    assert result.min_years==0 and result.max_years is None
    result=label_job(sample(body_text='How Do I Know if the Role is Right For Me?\nAt least one year of software-testing experience.'))
    assert result.min_years==1
    result=label_job(sample(body_text='YOUR QUALIFICATIONS AND SKILLS\nAt least five years of experience.\nPreferred Qualifications\nPython'))
    assert result.min_years==5 and result.career=='experienced'


def test_explicit_optional_sentence_inside_requirements():
    result=structured_analysis(sample(body_text='Your Qualifications and Skills\nPython development experience\nSQL experience is a plus.\nExposure to AWS will be beneficial.'))
    assert result['technologies']['required']==['Python']
    assert result['technologies']['preferred']==['AWS','SQL']


def test_english_language_alternative_and_hardware_role():
    from app.job_labels import label_job
    result=structured_analysis(sample(body_text='Requirements\nProficiency in at least one language (Python, Java, Go).'))
    assert result['sections']['required'][0]['alternative']
    assert label_job(sample(title='Hardware QA Engineer',body_text='Python test automation')).it_match=='unknown'
    assert label_job(sample(title='Hardware Software Engineer',body_text='Python device software')).it_match=='yes'


def test_api_plain_body_preserves_stored_source_and_evidence_offsets(client):
    from sqlalchemy import select
    from app import api
    from app.database import Job,get_session
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        job=db.scalar(select(Job).order_by(Job.id))
        raw='<div><h2>Requirements</h2><p>Python &amp; Java</p><script>alert(1)</script></div>'
        job.body_text=raw;db.commit()
        response=client.get(f'/api/jobs/{job.id}').json()
        assert response['body_text']=='Requirements\nPython & Java'
        analysis=client.get(f'/api/jobs/{job.id}/analysis').json()
        row=analysis['sections']['required'][0]
        assert response['body_text'][row['start']:row['end']]==row['text']
        db.expire_all()
        assert db.get(Job,job.id).body_text==raw
    finally:session.close()
