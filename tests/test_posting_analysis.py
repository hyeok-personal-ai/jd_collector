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
