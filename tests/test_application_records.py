import csv
import io

from test_skill_matching import client


def test_records_survive_closed_posting_and_filters_match_export_and_analysis(client):
    items=client.get('/api/jobs').json()['items']
    closed=next(j for j in items if j['source_status']=='closed')
    url=f"/api/jobs/{closed['id']}/application"
    record={'status':'applied','note':'Synthetic note: awaiting a reply'}
    assert client.put(url,json=record).status_code==403
    assert client.put(url,json=record,headers={'X-Radar-Request':'1'}).status_code==200
    assert client.get(url).json()['note']==record['note']
    result=client.get('/api/jobs',params={'application_status':'applied'}).json()
    assert result['total']==1 and result['items'][0]['id']==closed['id']
    assert result['items'][0]['application']['status']=='applied'
    assert client.get('/api/jobs',params={'application_status':'none'}).json()['total']==3
    assert client.get('/api/jobs',params={'application_status':'applied','open_only':True}).json()['total']==0
    rows=list(csv.reader(io.StringIO(client.get('/api/export.csv?application_status=applied').text.lstrip('\ufeff'))))
    assert len(rows)==2 and rows[1][0]==closed['title']
    assert client.get('/api/job-analysis?application_status=applied&open_only=false').json()['total']==1
    assert client.put(url,json={'status':'invalid'},headers={'X-Radar-Request':'1'}).status_code==422
    assert client.get('/api/jobs/999999/application').status_code==404


def test_explicit_role_filters_and_unknown_employment(client):
    assert client.get('/api/jobs?role=server').json()['total']==1
    assert client.get('/api/jobs?role=backend').json()['total']==2
    assert client.get('/api/jobs?role=infra').json()['total']==0
    assert client.get('/api/jobs?employment_type=unknown').json()['total']==4
    assert client.get('/api/jobs?employment_type=full_time').json()['total']==0
    assert client.get('/api/jobs?employment_type=arbitrary').status_code==422


def test_label_revision_backfills_once_without_changing_application_identity(client):
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app import api
    from app.database import Job,JobClassification,get_session
    from app.backfill import ensure_current_labels
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        job=db.scalar(select(Job).order_by(Job.id));identifier=job.id
        job.body_text='자격요건\n3년 이상의 서버 개발 경험\n우대사항\n10년 이상의 업계 경험'
        db.commit()
        client.put(f'/api/jobs/{identifier}/application',json={'status':'applied','note':'Synthetic retained note'},headers={'X-Radar-Request':'1'})
        factory=sessionmaker(db.get_bind())
        assert ensure_current_labels(factory)==4
        assert ensure_current_labels(factory)==0
        db.expire_all()
        label=db.scalar(select(JobClassification).where(JobClassification.external_id==job.external_id))
        assert label.career=='early' and label.min_years==3
        assert client.get(f'/api/jobs/{identifier}/application').json()['note']=='Synthetic retained note'
        assert client.get('/api/jobs').json()['total']==4
    finally:session.close()
