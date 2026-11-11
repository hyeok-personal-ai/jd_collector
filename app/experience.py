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


def variant_selection(variants):
    """Keep a reference variant for details without inventing a clear winner."""
    name, _, winner = variants[0]
    counts = winner['assessment']['required_counts']
    if not counts['supported']:
        return 'insufficient_evidence', '선택 근거 부족 (상세 기준: ' + name + ')'
    if len(variants) > 1:
        other = variants[1][2]
        a, b = winner['assessment'], other['assessment']
        if (a['priority'] == b['priority'] and a['required_counts'] == b['required_counts']
                and a['resume_supported'] == b['resume_supported']
                and abs(winner['similarity'] - other['similarity']) < 0.02):
            return 'tie', '뚜렷한 우위 없음 (상세 기준: ' + name + ')'
    return 'supported', name


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
            CREATE TABLE IF NOT EXISTS candidate_facts (
              id INTEGER PRIMARY KEY CHECK(id=1), professional_months INTEGER,
              updated_at REAL NOT NULL);
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
        result['content_hash'] = row['content_hash']
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

    def candidate_facts(self):
        with self.connection() as db:
            row = db.execute('SELECT professional_months,updated_at FROM candidate_facts WHERE id=1').fetchone()
        return {'professional_months': row['professional_months'] if row else None,
                'updated_at': row['updated_at'] if row else None,
                'source': 'user_confirmed' if row and row['professional_months'] is not None else 'unknown',
                'scope': '회사 소속 백엔드 개발 실무 경력. 교육·사이드 프로젝트 제외.'}

    def save_candidate_facts(self, professional_months):
        if professional_months is not None and (type(professional_months) is not int or not 0 <= professional_months <= 720):
            raise ValueError('실무 경력은 0~720개월 또는 미확인으로 입력하세요.')
        with self.connection() as db:
            db.execute('INSERT INTO candidate_facts(id,professional_months,updated_at) VALUES(1,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET professional_months=excluded.professional_months,updated_at=excluded.updated_at',
                       (professional_months,time.time()))
        return self.candidate_facts()


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


def recommend(db, document_ids, conditions=(), limit=20, store=None, *, analysis_cache=None, include_body=True, candidate_facts=None):
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
    candidate_facts = store.candidate_facts() if candidate_facts is None else candidate_facts
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
    from .qualification import assess_analysis, document_passages
    passages = document_passages(docs)
    # A bounded, actual vector search over already-filtered posting vectors.
    eligible = db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    query = select(Job.id, JobEmbedding.vector).join(JobEmbedding, JobEmbedding.job_id == Job.id).where(
        *conditions, JobEmbedding.model == MODEL_KEY, JobEmbedding.content_hash == Job.content_hash)
    best = []
    examined = 0
    for job_id, raw in db.execute(query.execution_options(yield_per=256)):
        # Balance document kinds so a long portfolio cannot drown out a resume.
        by_kind = {}
        for doc, matrix in zip(docs, matrices):
            scores = matrix @ np.frombuffer(raw, dtype='<f4')
            by_kind.setdefault(doc['kind'], []).append(float(np.mean(np.sort(scores)[-min(3, len(scores)):])))
        weights = {'resume': .6, 'portfolio': .4, 'experience': .4}
        score = sum(np.mean(values)*weights[kind] for kind, values in by_kind.items()) / sum(weights[k] for k in by_kind)
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
        cache_key = (job.id, job.content_hash)
        if analysis_cache is not None and cache_key in analysis_cache:
            analysis = analysis_cache[cache_key]
        else:
            analysis = structured_analysis(job)
            if analysis_cache is not None:
                analysis_cache[cache_key] = analysis
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
        assessment = assess_analysis(analysis, docs, passages, candidate_facts=candidate_facts)
        job_out = JobOut.model_validate(job).model_dump(mode='json')
        if not include_body:
            job_out.pop('body_text', None)
        items.append({'job': job_out, 'similarity': round(score, 4),
                      'evidence_pairs': selected, 'analysis': analysis, 'guidance': assessment['guidance'],
                      'assessment': assessment})
    return {'status': 'ready', 'items': items, 'eligible': eligible, 'compared': examined,
            'unindexed': eligible-examined, 'documents': [ExperienceStore.public(d) for d in docs],
            'candidate_facts': candidate_facts,
            'note': '선택 조건을 통과한 공고와 문서의 의미 유사도입니다. 지원 적합률이나 합격 확률이 아닙니다. 원문과 실제 경험을 함께 확인하세요.'}


def compare_variants(db, document_ids, conditions=(), limit=20, store=None):
    """Assess every indexed candidate against each variant, never blend versions."""
    from sqlalchemy import select, func
    from .database import Job
    store = store or ExperienceStore()
    selected = set(document_ids)
    docs = [d for d in store.list() if d['id'] in selected]
    if len(docs) != len(selected):
        raise ValueError('선택한 문서를 찾을 수 없습니다.')
    groups = {}
    for doc in docs:
        if not doc['variant'].strip():
            raise ValueError('비교할 문서에 일반형·Node형·Java형 등 유형을 지정하세요.')
        groups.setdefault(doc['variant'], []).append(doc['id'])
    if not 2 <= len(groups) <= 5:
        raise ValueError('서로 다른 문서 유형을 2~5개 선택하세요.')
    eligible = db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    analysis_cache = {}
    candidate_facts = store.candidate_facts()
    results = {name: recommend(db, ids, conditions, max(1, eligible), store,
                analysis_cache=analysis_cache, include_body=False, candidate_facts=candidate_facts) for name, ids in groups.items()}
    if any(r['status'] != 'ready' for r in results.values()):
        return {'status': 'index_pending', 'items': [], 'note': '모든 비교 문서의 색인이 완료되어야 같은 공고로 비교할 수 있습니다.'}
    # Surface conflicting metadata from same-company, same-title postings.
    # These remain separate records, never automatically merged as duplicates.
    import re
    related = {}
    for analysis in analysis_cache.values():
        org = analysis.get('organization') or ''
        if not org:
            continue
        title = analysis['title'].replace(org, '')
        key = (re.sub(r'\W+', '', org).casefold(), re.sub(r'\W+', '', title).casefold())
        related.setdefault(key, []).append(analysis)
    for group in related.values():
        conflicts = [(a, w) for a in group for w in a.get('quality_warnings', []) if w['code']=='deadline_conflict']
        for analysis in group:
            for other, warning in conflicts:
                if other['job_id'] == analysis['job_id']:
                    continue
                analysis['quality_warnings'].append({**warning, 'code':'related_source_conflict',
                    'message':f"같은 기업·제목의 다른 공고({other['job_id']})에 마감일 충돌이 있습니다. 동일 채용인지와 최종 기한을 확인하세요. 자동 병합하지 않았습니다."})
    for result in results.values():
        for item in result['items']:
            a = item['assessment']
            if item['analysis'].get('quality_warnings') and not a['required_counts']['mismatch']:
                a['priority'] = '조건 확인 필요'
                a['priority_reason'] = '공고 날짜·구역 또는 같은 제목의 다른 출처에 확인할 문제가 있습니다.'
                a['quality_warnings'] = item['analysis']['quality_warnings']
    priorities = {'먼저 지원': 0, '보완 후 지원': 1, '조건 확인 필요': 2, '현재 후순위': 3}
    def order(item):
        a = item['assessment']; c = a['required_counts']
        total = sum(c.values()) or 1
        return (priorities[a['priority']], -c['supported']/total, -a['resume_supported']/total, -item['similarity'], item['job']['id'])
    indexed = {}
    summaries = []
    # A concurrent collection update can invalidate an embedding during a run.
    # Intersect observed IDs so all displayed candidates have the same basis.
    stable = set.intersection(*({(i['job']['id'], i['analysis']['provenance']['content_hash']) for i in r['items']} for r in results.values()))
    common = {job_id for job_id, _ in stable}
    for name, result in results.items():
        ranked = sorted((i for i in result['items'] if i['job']['id'] in common), key=order)
        counts = {}
        for rank, item in enumerate(ranked, 1):
            a = item['assessment']; counts[a['priority']] = counts.get(a['priority'], 0)+1
            indexed.setdefault(item['job']['id'], []).append((name, rank, item))
        summaries.append({'variant': name, 'document_ids': groups[name], 'counts': counts,
                          'top_job_ids': [i['job']['id'] for i in ranked[:limit]]})
    combined = []
    for job_id, variants in indexed.items():
        variants.sort(key=lambda v: (*order(v[2])[:-1], v[0]))
        name, rank, winner = variants[0]
        selection_status, selection_label = variant_selection(variants)
        counts = winner['assessment']['required_counts']
        reasons = (f"{name}: 필수 근거 {counts['supported']}개, 미확인 {counts['unknown']}개, "
                   f"불일치 {counts['mismatch']}개. 이력서 자체에 근거가 있는 조건 {winner['assessment']['resume_supported']}개. "
                   '필수 판정 → 근거 비율 → 이력서 근거 → 문서별 의미 유사도 순으로 비교했습니다.')
        variant_rows = []
        for n, r, i in variants:
            own = {a['requirement'] for a in i['assessment']['requirements'] if a['category'] == 'required' and a['state'] == 'supported'}
            others = {a['requirement'] for other, _, result in variants if other != n
                      for a in result['assessment']['requirements'] if a['category'] == 'required' and a['state'] == 'supported'}
            variant_rows.append({'variant': n, 'rank': r, 'similarity': i['similarity'],
                'assessment': i['assessment'], 'evidence_pairs': i['evidence_pairs'],
                'distinct_evidence': sorted(own-others), 'evidence_in_other_versions': sorted(others-own)})
        combined.append({'job': winner['job'], 'analysis': winner['analysis'],
            'best_variant': name, 'selection_status': selection_status,
            'selection_label': selection_label, 'priority': winner['assessment']['priority'],
            'best_variant_reason': reasons, 'variants': variant_rows,
            '_order': order(winner)})
    combined.sort(key=lambda i: i['_order'])
    for item in combined:
        item.pop('_order')
    return {'status': 'ready', 'items': combined[:limit], 'eligible': eligible, 'compared': len(common),
            'unindexed': eligible-len(common), 'variants': summaries,
            'documents': [d for r in results.values() for d in r['documents']],
            'candidate_facts': candidate_facts,
            'note': '모든 문서 유형을 같은 후보 공고로 비교했습니다. 버전별 경험을 합치지 않았습니다. 지원 우선순위는 문서 근거에 따른 점검 순서이며 합격 확률이 아닙니다.'}
