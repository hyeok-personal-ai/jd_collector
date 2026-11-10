"""Local semantic index built by a separate process, incrementally after updates."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from sqlalchemy import select, or_, func, case, exists

from .control import data_dir, ControlStore
from .database import Job, JobEmbedding, JobClassification, SessionLocal, init_db, utcnow
from .locking import FileLock
from .posting_analysis import structured_analysis, plain_body
from .posting_sections import extract_sections

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MODEL_KEY = MODEL + ':sections-token100-overlap20-v4'
REUSABLE_MODEL_KEYS = {MODEL + ':sections-token100-overlap20-v2', MODEL + ':sections-token100-overlap20-v3'}
DIM = 384


def stale_condition():
    return or_(JobEmbedding.job_id.is_(None), JobEmbedding.content_hash != Job.content_hash,
               JobEmbedding.model != MODEL_KEY)


def index_status(db):
    total=db.scalar(select(func.count()).select_from(Job)) or 0
    pending=db.scalar(select(func.count()).select_from(Job).outerjoin(JobEmbedding,JobEmbedding.job_id==Job.id)
                      .where(stale_condition())) or 0
    with ControlStore().connection() as control:
        record=control.execute("SELECT value FROM settings WHERE key='vector_status'").fetchone()
    worker=json.loads(record[0]) if record else {}
    worker['online']=time.time()-worker.get('heartbeat',0)<120
    return {'model':MODEL,'total':total,'indexed':total-pending,'pending':pending,
            'worker':worker,'privacy':'local_only','metric':'cosine_similarity_not_eligibility'}


def status(phase, **extra):
    # SQLite avoids Windows rename failures while the UI reads a status file.
    value=json.dumps({'phase':phase,'heartbeat':time.time(),'pid':os.getpid(),**extra})
    with ControlStore().connection() as control:
        control.execute("INSERT INTO settings(key,value) VALUES('vector_status',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))


def chunks_for(job, tokenizer):
    # Embedding needs source sections only, not career and technology summaries.
    sections=extract_sections(job)
    segments=[j['text'] for k in ('duties','required','preferred') for j in sections[k]]
    if not segments:segments=[plain_body(job)]
    chunks=[job.title]
    for text in segments:
        encoded=tokenizer.encode(text,add_special_tokens=False)
        offsets=encoded.offsets
        for start in range(0,len(offsets),80):
            end=min(start+100,len(offsets))
            chunk=text[offsets[start][0]:offsets[end-1][1]].strip()
            if chunk and chunk not in chunks:chunks.append(chunk)
            if end==len(offsets):break
    return chunks


def encode_job(job, model, tokenizer, chunks=None):
    import numpy as np
    chunks=chunks_for(job,tokenizer) if chunks is None else chunks
    vectors=np.asarray(list(model.embed(chunks,batch_size=16)),dtype='<f4')
    norms=np.linalg.norm(vectors,axis=1,keepdims=True)
    vectors=vectors/np.maximum(norms,1e-12)
    vector=vectors[0] if len(vectors)==1 else .25*vectors[0]+.75*vectors[1:].mean(axis=0)
    vector=vector/max(float(np.linalg.norm(vector)),1e-12)
    if not np.isfinite(vectors).all() or not np.isfinite(vector).all():
        raise ValueError('Embedding produced non-finite values')
    return JobEmbedding(job_id=job.id,content_hash=job.content_hash,model=MODEL_KEY,
                        vector=vector.astype('<f4').tobytes(),chunks=chunks,
                        chunk_vectors=vectors.astype('<f4').tobytes(),updated_at=utcnow())


def build_batch(model, tokenizer, factory=SessionLocal, limit=16):
    with factory() as db:
        development=exists(select(JobClassification.id).where(JobClassification.source_id==Job.source_id,
            JobClassification.external_id==Job.external_id,JobClassification.it_match=='yes'))
        jobs=db.scalars(select(Job).outerjoin(JobEmbedding,JobEmbedding.job_id==Job.id)
                        .where(stale_condition()).order_by(
                            case(((Job.source_status=='open') & development,0),else_=1),
                            case((Job.source_status=='closed',1),else_=0),Job.updated_at.desc()).limit(limit)).all()
        db.expunge_all()
    for job in jobs:
        status('indexing',job_id=job.id,batch_size=len(jobs))
        chunks=chunks_for(job,tokenizer)
        with factory() as db:
            previous=db.get(JobEmbedding,job.id)
            if previous:db.expunge(previous)
        # Parser-only revisions can retain the exact same model input. Reuse
        # those vectors; changed chunks still require genuine re-embedding.
        if (previous and previous.model in REUSABLE_MODEL_KEYS and
                previous.content_hash==job.content_hash and previous.chunks==chunks):
            row=previous;row.model=MODEL_KEY;row.updated_at=utcnow()
        else:row=encode_job(job,model,tokenizer,chunks)
        with factory() as db:
            current=db.get(Job,job.id)
            # A collector may have changed the body while inference was running.
            if current and current.content_hash==job.content_hash:
                db.merge(row)
                db.commit()
    return len(jobs)


def worker(once=False):
    lock=FileLock(data_dir()/'vectors.lock')
    if not lock.acquire():return
    try:
        status('loading_model')
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer
        model=TextEmbedding(MODEL,cache_dir=str(data_dir()/'models'),threads=2)
        tokenizer=Tokenizer.from_str(model.model.tokenizer.to_str())
        tokenizer.no_truncation();tokenizer.no_padding()
        init_db()
        while True:
            count=build_batch(model,tokenizer)
            if count:continue
            status('ready')
            if once:break
            time.sleep(30)
    except Exception as exc:
        status('error',error_type=type(exc).__name__,
               message='로컬 분석 엔진을 준비하지 못했습니다. 설치 상태와 모델 다운로드 연결을 확인하세요.')
        raise
    finally:lock.release()


async def supervise():
    """Own the child lifecycle; API threads never load or run the embedding model."""
    while True:
        child=None
        try:
            with (data_dir()/'vector-worker.log').open('a',encoding='utf-8') as log:
                child=subprocess.Popen([sys.executable,'-X','utf8','-m','app.vector_index'],
                    stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                while child.poll() is None:await asyncio.sleep(2)
        finally:
            if child and child.poll() is None:
                if os.name=='nt':
                    # Windows venv python.exe redirects to a child interpreter.
                    # Stop our owned process tree, not only the redirector.
                    await asyncio.to_thread(subprocess.run,
                        ['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
                else:child.terminate()
                try:await asyncio.to_thread(child.wait,timeout=10)
                except subprocess.TimeoutExpired:child.kill();await asyncio.to_thread(child.wait)
        await asyncio.sleep(60)


def nearest(db,job,conditions=(),limit=10):
    import numpy as np
    row=db.get(JobEmbedding,job.id)
    coverage=index_status(db)
    if row is None or row.content_hash!=job.content_hash or row.model!=MODEL_KEY:
        return {'status':'index_pending','coverage':coverage,'items':[]}
    target=np.frombuffer(row.vector,dtype='<f4')
    # Rank compact vectors first. Loading every full body and chunk matrix made
    # a query read hundreds of MB although only the top few need evidence.
    statement=select(Job.id,JobEmbedding.vector).join(JobEmbedding,JobEmbedding.job_id==Job.id).where(
        Job.id!=job.id,JobEmbedding.content_hash==Job.content_hash,JobEmbedding.model==MODEL_KEY,*conditions)
    import heapq
    best=[];examined=0
    for candidate_id,vector_bytes in db.execute(statement.execution_options(yield_per=256)):
        vector=np.frombuffer(vector_bytes,dtype='<f4')
        score=float(np.dot(target,vector));examined+=1
        value=(score,candidate_id)
        if len(best)<limit:heapq.heappush(best,value)
        elif score>best[0][0]:heapq.heapreplace(best,value)
    items=[]
    from .schema import JobOut
    from .skill_matching import CATALOG,requirement_mentions
    base=requirement_mentions(job,list(CATALOG))
    base_vectors=np.frombuffer(row.chunk_vectors,dtype='<f4').reshape(-1,DIM)
    for score,candidate_id in sorted(best,reverse=True):
        candidate=db.get(Job,candidate_id)
        embedding=db.get(JobEmbedding,candidate_id)
        if not candidate or not embedding or embedding.content_hash!=candidate.content_hash:continue
        other=requirement_mentions(candidate,list(CATALOG))
        shared=sorted(set(base['required']+base['preferred']+base['mentioned']) &
                      set(other['required']+other['preferred']+other['mentioned']))
        other_vectors=np.frombuffer(embedding.chunk_vectors,dtype='<f4').reshape(-1,DIM)
        similarities=base_vectors @ other_vectors.T
        pairs=[];used_a=set();used_b=set()
        for flat in np.argsort(similarities,axis=None)[::-1]:
            a,b=np.unravel_index(flat,similarities.shape)
            if a in used_a or b in used_b:continue
            pairs.append({'base':row.chunks[a],'candidate':embedding.chunks[b],
                          'similarity':round(float(similarities[a,b]),4)})
            used_a.add(a);used_b.add(b)
            if len(pairs)==2:break
        detail=structured_analysis(candidate)
        items.append({'job':JobOut.model_validate(candidate).model_dump(mode='json'),
                      'similarity':round(score,4),'shared_technologies':shared,'evidence_pairs':pairs,
                      'career':detail['career'], 'conditions':detail['conditions']})
    return {'status':'ready','coverage':coverage,'compared':examined,'items':items,
            'note':'문장 의미의 코사인 유사도입니다. 지원 적합률·합격 가능성·동일 공고 확정값이 아닙니다.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true')
    worker(parser.parse_args().once)
