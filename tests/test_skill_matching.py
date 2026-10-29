import re
import csv
import io
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import api
from app.database import Base,get_session
from app.repository import upsert_job
from app.schema import JobDraft
from app.skill_matching import normalize_skills,skill_pattern


def test_aliases_and_identifier_boundaries():
    assert normalize_skills('python, PYTHON, nest.js, docker-compose')==['Python','NestJS','Docker Compose']
    assert re.search(skill_pattern('Java'),'Java 17')
    assert not re.search(skill_pattern('Java'),'JavaScript 자바스크립트')
    assert re.search(skill_pattern('Python'),'Python3.12')
    assert not re.search(skill_pattern('Go'),'Django Google')
    assert re.search(skill_pattern('C++'),'C++ 개발')
    assert not re.search(skill_pattern('.*'),'any text')
    with pytest.raises(ValueError):normalize_skills(['Python']*31)


@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setenv('RADAR_DATA_DIR',str(tmp_path))
    monkeypatch.delenv('RADAR_WORKER',raising=False)
    monkeypatch.setattr(api,'init_db',lambda:None)
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine)
    with factory() as db:
        for identifier,title,body,status in [
            ('a','백엔드 엔지니어','Java, Spring Boot, Redis','open'),
            ('b','서버 개발자','Java 개발','open'),
            ('c','프론트엔드 개발자','JavaScript React','open'),
            ('d','백엔드 개발자','Java Spring Redis','closed')]:
            upsert_job(db,JobDraft(source_id='demo',external_id=identifier,title=title,
                body_text=body,original_url='https://example.org/'+identifier,source_status=status))
        db.commit()
    def session():
        with factory() as db:yield db
    api.app.dependency_overrides[get_session]=session
    try:
        with TestClient(api.app) as c:yield c
    finally:
        api.app.dependency_overrides.clear()
        engine.dispose()


def test_filter_rank_count_pagination_and_csv_use_same_predicate(client):
    query={'skills':'Java, Spring, Redis','open_only':True,'sort':'match','role':'backend_infra','page_size':1}
    result=client.get('/api/jobs',params=query).json()
    assert result['total']==2
    assert result['items'][0]['external_id']=='a'
    assert result['items'][0]['skill_match']['matched']==['Java','Spring','Redis']
    result=client.get('/api/jobs',params={**query,'page':2}).json()
    assert result['items'][0]['external_id']=='b'
    rows=list(csv.reader(io.StringIO(client.get('/api/export.csv',params=query).text.lstrip('\ufeff'))))
    assert len(rows)==3 and rows[1][0]=='백엔드 엔지니어'
    assert client.get('/api/jobs',params={**query,'skill_mode':'all'}).json()['total']==1
    assert client.get('/api/jobs',params={'skills':'Java','open_only':True}).json()['total']==2


def test_local_profile_persists_and_write_guard_applies(client):
    assert client.get('/api/skill-profile').json()['skills']==[]
    data={'skills':['java','nest.js'],'role':'backend_infra'}
    assert client.put('/api/skill-profile',json=data).status_code==403
    result=client.put('/api/skill-profile',json=data,headers={'X-Radar-Request':'1'})
    assert result.status_code==200
    assert client.get('/api/skill-profile').json()=={'skills':['Java','NestJS'],'role':'backend_infra'}
    assert client.get('/api/jobs',params={'skills':','.join(['Java']*31)}).status_code==422


def test_skill_index_refreshes_after_collected_content_changes(client):
    assert client.get('/api/jobs', params={'skills': 'Kafka'}).json()['total'] == 0
    session = api.app.dependency_overrides[get_session]()
    db = next(session)
    try:
        upsert_job(db, JobDraft(source_id='demo', external_id='b', title='서버 개발자',
            body_text='Kafka 운영', original_url='https://example.org/b', source_status='open'))
        db.commit()
    finally:
        session.close()
    result = client.get('/api/jobs', params={'skills': 'Kafka', 'open_only': True}).json()
    assert result['total'] == 1
    assert result['items'][0]['skill_match']['matched'] == ['Kafka']
    assert client.get('/api/jobs', params={'skills': 'Java', 'open_only': True}).json()['total'] == 1


def test_requirement_sections_and_evidence():
    from types import SimpleNamespace
    from app.skill_matching import requirement_mentions
    job = SimpleNamespace(title='Java 서버 개발자', body_text='<h2>자격요건</h2><p>Java 또는 Python 경험</p><h2>우대사항</h2><p>AWS, Docker 경험</p><h2>주요업무</h2><p>Redis 운영</p>')
    result = requirement_mentions(job, ['Java','Python','AWS','Docker','Redis'])
    assert result['required'] == ['Java','Python']
    assert result['preferred'] == ['AWS','Docker']
    assert result['mentioned'] == ['Redis']
    assert any('또는' in e['evidence'] for e in result['evidence'])
    result = requirement_mentions(SimpleNamespace(title='',body_text='Python 개발\nRedis 경험 우대'), ['Python','Redis'])
    assert result['required'] == []
    assert result['mentioned'] == ['Python'] and result['preferred'] == ['Redis']
