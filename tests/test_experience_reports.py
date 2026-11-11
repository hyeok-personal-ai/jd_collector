import io
import json
import time

import numpy as np
import pytest
from test_skill_matching import client

from app.experience import ExperienceStore, extract_document, index_documents
from app.vector_index import MODEL_KEY, DIM

HEADERS = {'X-Radar-Request': '1'}
TEXT = 'Synthetic project: built a Java Spring API, measured Redis caching and tested Docker deployment.'


def test_document_lifecycle_and_input_guards(client):
    body = {'title': 'Synthetic portfolio', 'kind': 'portfolio', 'variant': 'Java', 'text': TEXT}
    assert client.post('/api/documents', json=body).status_code == 403
    r = client.post('/api/documents', json=body, headers=HEADERS)
    assert r.status_code == 200
    row = r.json()
    assert row['status'] == 'pending' and row['text'] == TEXT
    listed = client.get('/api/documents').json()['items']
    assert len(listed) == 1 and 'text' not in listed[0]
    assert client.put(f"/api/documents/{row['id']}", json={**body, 'text': TEXT+' Updated.'}, headers=HEADERS).status_code == 200
    assert client.post('/api/recommendations', json={'document_ids':[row['id']]},headers=HEADERS).json()['status']=='index_pending'
    assert client.delete(f"/api/documents/{row['id']}", headers=HEADERS).json()['deleted']
    assert client.get(f"/api/documents/{row['id']}").status_code == 404
    assert client.post('/api/documents',json={**body,'file_base64':'!!'},headers=HEADERS).status_code==422


def test_confirmed_career_facts_are_optional_private_and_guarded(client):
    assert client.get('/api/candidate-facts').json()['professional_months'] is None
    assert client.put('/api/candidate-facts',json={'professional_months':0}).status_code==403
    r = client.put('/api/candidate-facts',json={'professional_months':0},headers=HEADERS)
    assert r.json()['source']=='user_confirmed' and r.json()['professional_months']==0
    assert client.put('/api/candidate-facts',json={'professional_months':True},headers=HEADERS).status_code==422
    assert client.put('/api/candidate-facts',json={'professional_months':721},headers=HEADERS).status_code==422
    assert client.put('/api/candidate-facts',json={'professional_months':None},headers=HEADERS).json()['source']=='unknown'


class Tokenizer:
    def encode(self, text, **kwargs):
        return type('Tokens', (), {'offsets':[(i,i+1) for i in range(len(text))]})()


class Model:
    def embed(self, chunks, **kwargs):
        for text in chunks:
            vector=np.zeros(DIM,dtype='<f4');vector[0]=1
            yield vector


def test_index_edit_race_and_errors_do_not_spin(tmp_path):
    store=ExperienceStore(tmp_path/'docs.db')
    row=store.save('Synthetic','portfolio','Java',TEXT)
    class EditingModel(Model):
        def embed(self,chunks,**kwargs):
            store.save('Synthetic','portfolio','Java',TEXT+' Changed during indexing.',document_id=row['id'])
            yield from super().embed(chunks,**kwargs)
    index_documents(EditingModel(),Tokenizer(),store)
    assert store.get(row['id'])['status']=='pending'
    assert index_documents(Model(),Tokenizer(),store)==1
    assert store.get(row['id'])['status']=='ready'
    assert index_documents(Model(),Tokenizer(),store)==0
    store.save('Synthetic','portfolio','Java',TEXT+' Next edit.',document_id=row['id'])
    class BrokenModel:
        def embed(self,*args,**kwargs):raise RuntimeError('Synthetic failure')
    assert index_documents(BrokenModel(),Tokenizer(),store)==1
    assert store.get(row['id'])['status']=='error'
    assert index_documents(BrokenModel(),Tokenizer(),store)==0


def test_recommendation_prefilters_closed_and_stale_embeddings(client):
    from app import api
    from app.database import Job,JobEmbedding,get_session
    from sqlalchemy import select
    from app.experience import recommend
    store=ExperienceStore();doc=store.save('Synthetic','resume','General',TEXT)
    index_documents(Model(),Tokenizer(),store)
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        jobs=db.scalars(select(Job)).all()
        vector=np.zeros(DIM,dtype='<f4');vector[0]=1
        for job in jobs:
            db.add(JobEmbedding(job_id=job.id,content_hash=job.content_hash,model=MODEL_KEY,
                vector=vector.tobytes(),chunks=[job.title,job.body_text],chunk_vectors=np.stack([vector,vector]).tobytes()))
        db.commit()
        conditions=api.job_conditions(open_only=True,role='backend_infra')
        result=recommend(db,[doc['id']],conditions)
        assert result['eligible']==result['compared']==2
        assert all(item['job']['source_status']=='open' for item in result['items'])
        assert all(p['text'] in TEXT for item in result['items'] for p in item['evidence_pairs'])
        job=jobs[0];job.content_hash='changed';db.commit()
        result=recommend(db,[doc['id']],conditions)
        assert result['unindexed']==1
        assert job.id not in [item['job']['id'] for item in result['items']]
    finally:session.close()


def test_report_escapes_untrusted_source_and_pdf_korean():
    from app.reports import html_report,markdown,pdf_report
    blocks=[('title','한글 공고 분석'),('quote','<script>alert(1)</script> Java & Spring'),('link','javascript:alert(1)')]
    html=html_report(blocks)
    assert '<script>' not in html and 'href="javascript:' not in html
    assert '&lt;script&gt;' in markdown(blocks)
    pytest.importorskip('reportlab')
    pypdf=pytest.importorskip('pypdf')
    pdf=pdf_report(blocks)
    reader=pypdf.PdfReader(io.BytesIO(pdf))
    assert len(reader.pages)>=1
    assert 'Java' in reader.pages[0].extract_text()


def test_variant_comparison_same_pool_separate_evidence_and_report(client):
    from app import api
    from app.database import Job, JobEmbedding, get_session
    from sqlalchemy import select
    store = ExperienceStore()
    java = store.save('Synthetic Java', 'resume', 'Java', 'Java와 Spring API를 구현하고 Redis 오류 경로를 테스트했습니다.')
    node = store.save('Synthetic Node', 'resume', 'Node', 'Node.js API를 구현하고 요청자 소유권과 삭제 경계를 테스트했습니다.')
    index_documents(Model(), Tokenizer(), store)
    session = api.app.dependency_overrides[get_session](); db = next(session)
    try:
        vector = np.zeros(DIM, dtype='<f4'); vector[0] = 1
        for job in db.scalars(select(Job)).all():
            job.body_text = '자격요건\nJava 개발 경험\n우대사항\nRedis 사용 경험'
            db.add(JobEmbedding(job_id=job.id, content_hash=job.content_hash, model=MODEL_KEY,
                vector=vector.tobytes(), chunks=[job.title, job.body_text], chunk_vectors=np.stack([vector, vector]).tobytes()))
        db.commit()
        payload = {'document_ids':[java['id'],node['id']], 'filters':{'open_only':True,'it_only':False,'role':'backend_infra'}}
        assert client.post('/api/recommendations/compare', json=payload).status_code == 403
        result = client.post('/api/recommendations/compare', json=payload, headers=HEADERS).json()
        assert result['status'] == 'ready' and result['compared'] == 2
        assert result['eligible'] == 2 and result['unindexed'] == 0
        both = client.post('/api/recommendations/compare', json={**payload,'filters':{'it_only':False,'role':'backend_server'}}, headers=HEADERS).json()
        assert both['compared']==2
        for item in result['items']:
            assert item['best_variant'] == 'Java'
            assert len(item['variants']) == 2
            for variant in item['variants']:
                for row in variant['assessment']['requirements']:
                    assert all(e['variant'] == variant['variant'] for e in row['evidence'])
        for fmt in ('html', 'md'):
            r = client.post('/api/reports', json={**payload, 'kind':'variants','format':fmt}, headers=HEADERS)
            assert r.status_code == 200 and 'SHA-256' in r.text
            assert 'Java' in r.text and 'Node' in r.text
        store.save('Synthetic Node', 'resume', 'Node', TEXT+' Edited.', document_id=node['id'])
        assert client.post('/api/recommendations/compare', json=payload, headers=HEADERS).json()['status'] == 'index_pending'
        assert client.post('/api/reports', json={**payload,'kind':'variants'}, headers=HEADERS).status_code == 409
        assert client.post('/api/recommendations/compare', json={'document_ids':[java['id']]}, headers=HEADERS).status_code == 422
    finally:
        session.close()


def test_report_endpoints_and_text_input_limits(client):
    ids=[x['id'] for x in client.get('/api/jobs').json()['items'][:2]]
    for fmt in ('html','md'):
        response=client.post('/api/reports',json={'ids':ids,'format':fmt},headers=HEADERS)
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert client.post('/api/reports',json={'ids':[]},headers=HEADERS).status_code==422
    assert client.post('/api/reports',json={'ids':[99999]},headers=HEADERS).status_code==404
    assert extract_document('sample.md',TEXT.encode())==TEXT
    with pytest.raises(ValueError):extract_document('sample.exe',b'contents')
    with pytest.raises(ValueError):extract_document('sample.txt',b'X'*120001)


def test_qa_expiry_restores_daily_and_preserves_failure_backoff(tmp_path,monkeypatch):
    from app.control import ControlStore
    store=ControlStore(tmp_path/'control.db')
    with store.connection() as db:
        db.execute('INSERT INTO targets(key,spec,enabled,status) VALUES(?,?,1,?)',('qa-public',json.dumps({'kind':'public'}),'completed'))
    qa=store.begin_qa(['qa-public'],60,120)
    store.enqueue(['qa-public']);task=store.claim()
    store.finish(task,status='failed',error='robots.txt check returned 502')
    with store.connection() as db:
        row=db.execute('SELECT * FROM targets WHERE key=?',('qa-public',)).fetchone()
    assert row['next_due']-row['checked_at']>=1800
    monkeypatch.setattr('app.control.time.time',lambda:qa['expires_at']+1)
    settings=store.settings()
    assert settings['auto_collect'] is True and settings['interval_hours']==24 and settings['qa_schedule']=={}
    with pytest.raises(ValueError):store.begin_qa(['qa-public'])


def test_document_page_footer_is_not_experience():
    from app.experience import substantive_chunk
    assert not substantive_chunk('이력서 2 / 2')
    assert not substantive_chunk('[페이지 3] 포트폴리오 이름 3 / 10')
    assert substantive_chunk('Redis 캐시 도입 전후의 응답 시간을 측정하고 데이터 갱신 시 캐시 무효화를 검증했습니다.')


def test_explicit_pause_during_qa_does_not_resume_at_expiry(tmp_path,monkeypatch):
    from app.control import ControlStore
    store=ControlStore(tmp_path/'control.db')
    with store.connection() as db:
        db.execute('INSERT INTO targets(key,spec,enabled,status) VALUES(?,?,1,?)',('qa-public',json.dumps({'kind':'public'}),'completed'))
    qa=store.begin_qa(['qa-public'],60,120)
    store.save_settings({'auto_collect':False})
    monkeypatch.setattr('app.control.time.time',lambda:qa['expires_at']+1)
    assert store.settings()['auto_collect'] is False
    assert store.settings()['qa_schedule']=={}


def test_variant_selection_does_not_overstate_similarity():
    from app.experience import variant_selection
    def item(supported, score):
        return {'assessment': {'required_counts': {'supported': supported, 'unknown': 2, 'mismatch': 0},
                'priority': '보완 후 지원', 'resume_supported': supported}, 'similarity': score}
    assert variant_selection([('Node', 1, item(0, .9)), ('Java', 1, item(0, .5))])[0] == 'insufficient_evidence'
    assert variant_selection([('Node', 1, item(2, .59)), ('Java', 1, item(2, .58))])[0] == 'tie'
    assert variant_selection([('Java', 1, item(3, .59)), ('Node', 1, item(2, .58))]) == ('supported', 'Java')
