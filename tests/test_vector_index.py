import struct
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from app.database import Job,JobEmbedding,get_session
from app import api
from app.vector_index import MODEL_KEY,DIM,nearest,index_status,chunks_for
from test_skill_matching import client


def embedding(job,first=1.,second=0.):
    vector=struct.pack('<'+'f'*DIM,first,second,*([0.]*(DIM-2)))
    return JobEmbedding(job_id=job.id,content_hash=job.content_hash,model=MODEL_KEY,
                        vector=vector,chunks=[job.title],chunk_vectors=vector)


def test_vectors_rank_and_exclude_stale_or_filtered_rows(client):
    pytest.importorskip('numpy')
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        jobs=db.scalars(select(Job).order_by(Job.id)).all()
        for i,j in enumerate(jobs):db.add(embedding(j,1 if i<2 else 0,0 if i<2 else 1))
        db.commit()
        result=nearest(db,jobs[0],limit=3)
        assert result['items'][0]['job']['id']==jobs[1].id
        assert result['items'][0]['similarity']==1
        assert all(r['job']['id']!=jobs[0].id for r in result['items'])
        jobs[1].content_hash='new-content';db.commit()
        result=nearest(db,jobs[0],[Job.source_status=='open'],limit=3)
        assert [r['job']['id'] for r in result['items']]==[jobs[2].id]
        assert index_status(db)['pending']==1
        assert nearest(db,jobs[1])['status']=='index_pending'
    finally:session.close()


def test_chunking_covers_long_body_and_does_not_truncate_tail():
    class Tokenizer:
        def encode(self,text,add_special_tokens):
            return SimpleNamespace(offsets=[(i,i+1) for i in range(len(text))])
    from test_posting_analysis import sample
    body='시스템 '+('가나다라마바사 '*70)+'마지막 Redis Kafka 운영'
    chunks=chunks_for(sample(body_text=body),Tokenizer())
    assert 'Kafka' in chunks[-1]
    assert len(chunks)>2
    assert all(len(chunk)<=100 for chunk in chunks[1:])


def test_parser_revision_reuses_only_identical_model_input(client):
    pytest.importorskip('numpy')
    from sqlalchemy.orm import sessionmaker
    from app.vector_index import build_batch,REUSABLE_MODEL_KEYS
    class Tokenizer:
        def encode(self,text,add_special_tokens):
            return SimpleNamespace(offsets=[(i,i+1) for i in range(len(text))])
    class Model:
        calls=0
        def embed(self,chunks,batch_size):
            self.calls+=1
            return [[1.]+[0.]*(DIM-1) for _ in chunks]
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        factory=sessionmaker(db.get_bind(),expire_on_commit=False)
        model=Model()
        assert build_batch(model,Tokenizer(),factory)==4
        for row in db.scalars(select(JobEmbedding)):
            row.model=next(iter(REUSABLE_MODEL_KEYS))
        changed=db.scalar(select(JobEmbedding).order_by(JobEmbedding.job_id))
        changed.chunks=['Obsolete parser input']
        db.commit();model.calls=0
        assert build_batch(model,Tokenizer(),factory)==4
        assert model.calls==1
        db.expire_all()
        assert index_status(db)['pending']==0
        assert build_batch(model,Tokenizer(),factory)==0
    finally:session.close()


def test_similar_api_preserves_domestic_filter(client):
    pytest.importorskip('numpy')
    from app.classification_store import update_classification
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        jobs=db.scalars(select(Job).order_by(Job.id)).all()
        for i,job in enumerate(jobs):
            job.region='London, UK' if i==1 else 'Seoul, South Korea'
            update_classification(db,job)
            db.add(embedding(job))
        db.commit()
        unfiltered=client.get(f'/api/jobs/{jobs[0].id}/similar').json()
        assert {r['job']['id'] for r in unfiltered['items']}=={jobs[1].id,jobs[2].id}
        filtered=client.get(f'/api/jobs/{jobs[0].id}/similar?korea_only=true&it_only=true').json()
        assert [r['job']['id'] for r in filtered['items']]==[jobs[2].id]
    finally:session.close()


def test_batch_commit_does_not_attach_vector_to_changed_source(client):
    from sqlalchemy.orm import sessionmaker
    from app.vector_index import build_batch
    class Tokenizer:
        def encode(self,text,add_special_tokens):
            return SimpleNamespace(offsets=[(i,i+1) for i in range(len(text))])
    session=api.app.dependency_overrides[get_session]();db=next(session)
    try:
        factory=sessionmaker(db.get_bind(),expire_on_commit=False)
        target=db.scalar(select(Job).order_by(Job.id)).id
        class ChangingModel:
            changed=False
            def embed(self,chunks,batch_size):
                if not self.changed:
                    with factory() as other:
                        other.get(Job,target).content_hash='new-revision-during-inference';other.commit()
                    self.changed=True
                return [[1.]+[0.]*(DIM-1) for _ in chunks]
        assert build_batch(ChangingModel(),Tokenizer(),factory)==4
        db.expire_all()
        assert db.get(JobEmbedding,target) is None
        assert index_status(db)['pending']==1
    finally:session.close()
