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
