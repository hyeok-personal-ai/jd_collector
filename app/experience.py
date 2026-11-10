"""Private document storage and evidence-based matching; never part of cloud state."""
import hashlib
import io
import json
import sqlite3
import time
from contextlib import contextmanager

from .control import data_dir

MAX_BYTES = 12_000_000
MAX_TEXT = 120_000


def document_model_key():
    from .vector_index import MODEL_KEY
    return MODEL_KEY + ':experience-v2'


def substantive_chunk(text):
    import re
    cleaned = re.sub(r'\[페이지 \d+\]', '', text)
    cleaned = re.sub(r'[^\n]{0,35}(?:이력서|포트폴리오)\s*\d+\s*/\s*\d+[^\n]*', '', cleaned)
    return len(re.findall(r'[가-힣A-Za-z]', cleaned)) >= 20


def extract_document(filename, content):
    if not content or len(content) > MAX_BYTES:
        raise ValueError('파일은 12MB 이하로 선택하세요.')
    extension = filename.lower().rsplit('.', 1)[-1]
    if extension == 'pdf':
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or len(reader.pages) > 80:
                raise ValueError('암호 없는 80페이지 이하 PDF가 필요합니다.')
            text = '\n\n'.join(f'[페이지 {i+1}]\n{page.extract_text() or ""}'
                                for i, page in enumerate(reader.pages))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('PDF를 읽지 못했습니다. 텍스트 문서로 다시 저장해 주세요.') from exc
        # Page labels alone must not make an image-only PDF look successfully read.
        import re
        if len(re.sub(r'\[페이지 \d+\]|\s', '', text)) < 30:
            raise ValueError('텍스트가 없는 스캔 PDF입니다. OCR 처리하거나 내용을 붙여넣어 주세요.')
    elif extension in ('txt', 'md'):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeError as exc:
            raise ValueError('UTF-8 텍스트 파일로 저장해 주세요.') from exc
    else:
        raise ValueError('PDF, Markdown, TXT 파일을 지원합니다.')
    if len(text) > MAX_TEXT:
        raise ValueError('추출한 문서가 너무 깁니다. 12만 자 이하로 나누어 주세요.')
    return text.strip()


class ExperienceStore:
    def __init__(self, path=None):
        self.path = path or data_dir() / 'experience.sqlite3'
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
              variant TEXT NOT NULL, text TEXT NOT NULL, content_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              model TEXT, indexed_hash TEXT, chunks TEXT, vectors BLOB, error TEXT);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, title, kind, variant, text, enabled=True, document_id=None):
        if kind not in ('resume', 'portfolio', 'experience'):
            raise ValueError('문서 종류를 선택하세요.')
        text = text.strip()
        if not title.strip() or len(title) > 160 or len(variant) > 80 or not 30 <= len(text) <= MAX_TEXT:
            raise ValueError('제목과 30~120,000자의 문서 내용이 필요합니다.')
        digest = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            if document_id is None:
                if db.execute('SELECT count(*) FROM documents').fetchone()[0] >= 100:
                    raise ValueError('문서는 최대 100개까지 보관할 수 있습니다.')
                document_id = db.execute('INSERT INTO documents(title,kind,variant,text,content_hash,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    (title.strip(), kind, variant.strip(), text, digest, int(enabled), now, now)).lastrowid
            else:
                result = db.execute('UPDATE documents SET title=?,kind=?,variant=?,text=?,content_hash=?,enabled=?,updated_at=?,error=NULL WHERE id=?',
                    (title.strip(), kind, variant.strip(), text, digest, int(enabled), now, document_id))
                if not result.rowcount:
                    raise KeyError(document_id)
        return self.get(document_id)

    @staticmethod
    def public(row, include_text=False):
        if row is None:
            raise KeyError('Document not found')
        result = {k: row[k] for k in ('id', 'title', 'kind', 'variant', 'enabled', 'created_at', 'updated_at', 'error')}
        result['characters'] = len(row['text'])
        result['status'] = 'ready' if row['indexed_hash'] == row['content_hash'] and row['model'] == document_model_key() else ('error' if row['error'] else 'pending')
        if include_text:
            result['text'] = row['text']
        return result

    def get(self, document_id):
        with self.connection() as db:
            return self.public(db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone(), True)

    def list(self):
        with self.connection() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM documents ORDER BY id')]

    def delete(self, document_id):
        with self.connection() as db:
            if not db.execute('DELETE FROM documents WHERE id=?', (document_id,)).rowcount:
                raise KeyError(document_id)


def index_documents(model, tokenizer, store=None, limit=2):
    """Called only in the model worker, with compare-and-swap on edited content."""
    import numpy as np
    model_key = document_model_key()
    store = store or ExperienceStore()
    with store.connection() as db:
        rows = db.execute('SELECT * FROM documents WHERE enabled=1 AND error IS NULL AND (indexed_hash IS NULL OR indexed_hash!=content_hash OR model!=?) ORDER BY updated_at LIMIT ?', (model_key, limit)).fetchall()
    for row in rows:
        try:
            offsets = tokenizer.encode(row['text'], add_special_tokens=False).offsets
            chunks = [row['text'][offsets[start][0]:offsets[min(start+100, len(offsets))-1][1]]
                      for start in range(0, len(offsets), 80)]
            chunks = [chunk for chunk in chunks if substantive_chunk(chunk)]
            if not chunks:
                raise ValueError('No substantive document chunks')
            vectors = np.asarray(list(model.embed(chunks, batch_size=16)), dtype='<f4')
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            if not np.isfinite(vectors).all():
                raise ValueError('Invalid vector')
            with store.connection() as db:
                db.execute('UPDATE documents SET model=?,indexed_hash=?,chunks=?,vectors=?,error=NULL WHERE id=? AND content_hash=?',
                    (model_key, row['content_hash'], json.dumps(chunks, ensure_ascii=False), vectors.tobytes(), row['id'], row['content_hash']))
        except Exception:
            with store.connection() as db:
                db.execute('UPDATE documents SET error=? WHERE id=? AND content_hash=?', ('문서 색인 실패. 분석 엔진 상태를 확인해 주세요.', row['id'], row['content_hash']))
    return len(rows)


def recommend(db, document_ids, conditions=(), limit=20, store=None):
    import heapq
    import numpy as np
    from sqlalchemy import select, func
    from .database import Job, JobEmbedding
    from .vector_index import MODEL_KEY, DIM
    from .posting_analysis import structured_analysis
    from .schema import JobOut
    if not document_ids or len(document_ids) > 20:
        raise ValueError('비교할 문서를 1~20개 선택하세요.')
    store = store or ExperienceStore()
    with store.connection() as conn:
        docs = conn.execute('SELECT * FROM documents WHERE id IN (' + ','.join('?' * len(document_ids)) + ')', document_ids).fetchall()
    if len(docs) != len(set(document_ids)):
        raise ValueError('선택한 문서를 찾을 수 없습니다.')
    if any(not d['enabled'] or d['model'] != document_model_key() or d['content_hash'] != d['indexed_hash'] for d in docs):
        return {'status': 'index_pending', 'items': [], 'note': '선택 문서를 수정한 뒤 의미 색인을 갱신하고 있습니다.'}
    evidence = []
    matrices = []
    for doc in docs:
        chunks = json.loads(doc['chunks'])
        matrices.append(np.frombuffer(doc['vectors'], dtype='<f4').reshape(-1, DIM))
        evidence += [{'document_id': doc['id'], 'document_title': doc['title'], 'variant': doc['variant'], 'text': chunk} for chunk in chunks]
    vectors = np.concatenate(matrices)
    # A bounded, actual vector search over already-filtered posting vectors.
    eligible = db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    query = select(Job.id, JobEmbedding.vector).join(JobEmbedding, JobEmbedding.job_id == Job.id).where(
        *conditions, JobEmbedding.model == MODEL_KEY, JobEmbedding.content_hash == Job.content_hash)
    best = []
    examined = 0
    for job_id, raw in db.execute(query.execution_options(yield_per=256)):
        scores = vectors @ np.frombuffer(raw, dtype='<f4')
        score = float(np.mean(np.sort(scores)[-min(3, len(scores)):]))
        examined += 1
        if len(best) < limit:
            heapq.heappush(best, (score, job_id))
        elif score > best[0][0]:
            heapq.heapreplace(best, (score, job_id))
    items = []
    for score, job_id in sorted(best, reverse=True):
        job = db.get(Job, job_id)
        embedding = db.get(JobEmbedding, job_id)
        if not job or not embedding or embedding.content_hash != job.content_hash:
            continue
        analysis = structured_analysis(job)
        job_vectors = np.frombuffer(embedding.chunk_vectors, dtype='<f4').reshape(-1, DIM)
        scores = vectors @ job_vectors.T
        pairs = []
        used = set()
        # Skip the title when full requirement/duty chunks are available.
        for col in range(1 if len(embedding.chunks) > 1 else 0, len(embedding.chunks)):
            row = int(np.argmax(scores[:, col]))
            pairs.append({**evidence[row], 'posting_text': embedding.chunks[col], 'similarity': round(float(scores[row, col]), 4)})
        pairs.sort(key=lambda p: p['similarity'], reverse=True)
        selected = []
        for pair in pairs:
            key = (pair['document_id'], pair['text'])
            if key not in used:
                selected.append(pair); used.add(key)
            if len(selected) == 3:
                break
        # Explain what evidence to strengthen, never invent accomplishments.
        guidance = []
        for category in ('required', 'preferred'):
            for entry in analysis['sections'][category][:4]:
                guidance.append({'category': category, 'requirement': entry['text'],
                    'action': '이 조건과 연결되는 본인 역할·설계 선택·검증 결과를 실제 경험에서 확인하세요. 문서에 없다는 이유로 경험이 없다고 단정하지 않습니다.'})
        items.append({'job': JobOut.model_validate(job).model_dump(mode='json'), 'similarity': round(score, 4),
                      'evidence_pairs': selected, 'analysis': analysis, 'guidance': guidance})
    return {'status': 'ready', 'items': items, 'eligible': eligible, 'compared': examined,
            'unindexed': eligible-examined, 'documents': [ExperienceStore.public(d) for d in docs],
            'note': '선택 조건을 통과한 공고와 문서의 의미 유사도입니다. 지원 적합률이나 합격 확률이 아닙니다. 원문과 실제 경험을 함께 확인하세요.'}
