"""Read-only discovery API and a minimal browser UI."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select, exists, tuple_
from sqlalchemy.orm import Session
from .crawlers.registry import CRAWLERS
from .crawlers.catalog import SOURCES
from .access_policy import access_status
from .database import Job, JobClassification, CrawlRun, PostingPresence, get_session, init_db
from .schema import JobOut
from .duplicates import duplicate_candidates, exact_duplicate_groups
from .duplicate_index import indexed_similar_job_candidates
from .board_catalog import load_boards
from .company_catalog import load_companies, STAGES
from .site_family import identify, catalog_report
from .verification_store import latest_reports, verification_overview
import asyncio
import os
from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .control import ControlStore
from .skill_matching import normalize_skills, score_expression, match_details, BACKEND_TITLE, INFRA_TITLE, ensure_skill_index

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # For MVP; replace with Alembic migrations later.
    worker = None
    cloud_worker = None
    vector_worker = None
    if os.getenv('RADAR_WORKER') == '1':
        from .backfill import ensure_current_labels
        await asyncio.to_thread(ensure_current_labels)
        from .service import serve_queue
        worker = asyncio.create_task(serve_queue())
        from .cloud import sync_loop
        cloud_worker = asyncio.create_task(sync_loop())
        from .vector_index import supervise
        vector_worker = asyncio.create_task(supervise())
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if cloud_worker:
            cloud_worker.cancel()
            await asyncio.gather(cloud_worker, return_exceptions=True)
        if vector_worker:
            vector_worker.cancel()
            await asyncio.gather(vector_worker, return_exceptions=True)


app = FastAPI(title="Recruit Radar", version="2.4.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])


@app.middleware('http')
async def local_write_guard(request: Request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if request.headers.get('x-radar-request') != '1' or (origin and origin.rstrip('/') != str(request.base_url).rstrip('/')):
            return Response('Same-origin local application request required', status_code=403)
    return await call_next(request)
SessionDep = Annotated[Session, Depends(get_session)]
ApplicationStatus = Literal['none','planned','applied','interview','offer','rejected','withdrawn']
RoleFilter = Literal['','backend_infra','backend','server','infra']
EmploymentFilter = Literal['','full_time','contract','intern','part_time','unknown']


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get('/api/aggregate-status')
def aggregate_status(session: SessionDep):
    from .aggregate_store import AggregateStore
    store=AggregateStore()
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    status = case((or_(Job.source_status == 'closed', Job.deadline_on < now.date()), 'closed'),
                  else_=Job.source_status)
    counts = {}
    for source, state, count in session.execute(select(Job.source_id, status, func.count())
            .where(Job.source_id.in_(('inthiswork', 'zighang'))).group_by(Job.source_id, status)):
        counts.setdefault(source, {'open': 0, 'closed': 0, 'unknown': 0})[state] = count
    items = []
    for source in ('inthiswork', 'zighang'):
        item = store.summary(source)
        item['saved'] = counts.get(source, {'open': 0, 'closed': 0, 'unknown': 0})
        item['saved']['total'] = sum(item['saved'].values())
        # Sitemap URLs include historical postings; their count cannot be an open total.
        item['coverage_fresh'] = bool(item['last_scan'] and now.timestamp()-item['last_scan']<=93600)
        item['remaining_open'] = 0 if source == 'inthiswork' and (item['complete'] or item['current_list_complete']) and item['coverage_fresh'] else None
        latest=session.scalar(select(CrawlRun).where(CrawlRun.source_id==source).order_by(CrawlRun.id.desc()))
        checked=(latest.finished_at or latest.started_at) if latest else None
        item['latest_run']={'status':latest.status,'checked_at':checked.replace(tzinfo=timezone.utc).isoformat(),
                            'error':latest.error_summary} if latest else None
        item['development_open'] = session.scalar(select(func.count()).select_from(Job).join(
            JobClassification, (JobClassification.source_id == Job.source_id) &
            (JobClassification.external_id == Job.external_id)).where(Job.source_id == source,
            JobClassification.it_match == 'yes', Job.source_status == 'open',
            or_(Job.deadline_on.is_(None), Job.deadline_on >= now.date())))
        item['collection_mode'] = store.meta(source, 'collection_mode') or 'all'
        item['focus_inventory'] = store.focus_summary(source)
        if source=='zighang':
            from .crawlers.zighang_public import COMPANY_SOURCE
            item['company_discovery']=store.discovery_summary(COMPANY_SOURCE)
            from .crawlers.zighang_recent import recent_summary
            item['recent_discovery']=recent_summary(store)
        items.append(item)
    return {'captured_at': now.isoformat(), 'items': items}


@app.get("/api/sources")
def sources():
    return sorted(set(CRAWLERS) | {'company_public'})


@app.get("/api/source-catalog")
def source_catalog():
    return [{**source, "access_ready": access_status(source["id"])[0],
             "access_note": access_status(source["id"])[1]}
            for source in SOURCES]


@app.get("/api/board-catalog")
def board_catalog():
    """Provider/board candidates with provenance; never implies a live check."""
    return load_boards()


@app.get("/api/company-catalog")
def company_catalog(stage: int | None = Query(None, ge=1, le=5),
                    collectable_only: bool = False):
    """Verified-link discovery registry; not a claim of scrape permission."""
    rows=load_companies()
    if stage is not None:
        rows=[r for r in rows if r['priority_stage']==stage]
    if collectable_only:
        rows=[r for r in rows if 'ats_provider' in r]
    enriched=[]
    for r in rows:
        guess=identify(r['url'])
        enriched.append({**r,'site_family':guess.family,
                         'site_family_evidence':guess.evidence,
                         'auto_collection_enabled':bool('ats_provider' in r and guess.auto_enabled)})
    return {'total':len(rows),'stages':STAGES,'companies':enriched}


@app.get("/api/site-family-summary")
def site_family_summary():
    """Offline URL classification only, not live crawl verification."""
    return catalog_report(load_companies())


@app.get("/api/status-summary")
def status_summary(db: SessionDep):
    return {
        'jobs': db.scalar(select(func.count(Job.id))) or 0,
        'tagged': db.scalar(select(func.count(JobClassification.id))) or 0,
        'closed_by_absence': db.scalar(select(func.count(PostingPresence.id)).where(PostingPresence.closed_by_absence.is_(True))) or 0,
        'runs_failed': db.scalar(select(func.count(CrawlRun.id)).where(CrawlRun.status != 'completed')) or 0,
    }


def job_conditions(*, keyword=None, source_id=None, recruitment_type=None, region=None,
                   open_only=False, it_only=False, entry_only=False, include_unknown_entry=False,
                   bookmarked_only=False, korea_only=False, skills=None, skill_mode="any", role="",
                   employment_type='', application_status=''):
    conditions = []
    if application_status:
        records = ControlStore().applications()
        if application_status == 'none':
            conditions.append(Job.id.not_in([k for k,v in records.items() if v['status'] != 'none']))
        else:
            conditions.append(Job.id.in_([k for k,v in records.items() if v['status'] == application_status]))
    if employment_type == 'unknown':
        conditions.append(or_(Job.employment_type.is_(None), Job.employment_type == '',
                              Job.employment_type.in_(['unknown','미확인','미정'])))
    elif employment_type:
        patterns={'full_time':r'(?i)정규|full.?time|permanent', 'contract':r'(?i)계약|contract|freelance|프리랜서',
                  'intern':r'(?i)인턴|intern', 'part_time':r'(?i)파트|시간제|아르바이트|part.?time'}
        conditions.append(Job.employment_type.regexp_match(patterns[employment_type]))
    if bookmarked_only:
        conditions.append(Job.id.in_(ControlStore().bookmarks()))
    if korea_only:
        conditions.append(or_(*(Job.region.ilike('%'+term+'%') for term in
            ('한국','대한민국','서울','경기','부산','대전','인천','대구','광주','세종','성남','판교',
             'Seoul','Korea','Pangyo','Seongnam','Daejeon','Busan','Gyeonggi'))))
    if keyword:
        term = f"%{keyword}%"
        conditions.append(or_(Job.title.ilike(term), Job.organization.ilike(term),
                              Job.body_text.ilike(term)))
    if source_id:
        conditions.append(Job.source_id == source_id)
    if recruitment_type:
        conditions.append(Job.recruitment_type == recruitment_type)
    if region:
        conditions.append(Job.region.ilike(f"%{region}%"))
    if open_only:
        # Unknown deadline stays visible unless the source explicitly marks closed.
        conditions += [Job.source_status != "closed",
                       or_(Job.deadline_on.is_(None), Job.deadline_on >= datetime.now(ZoneInfo("Asia/Seoul")).date())]
    if it_only:
        conditions.append(exists(select(JobClassification.id).where(
            JobClassification.source_id == Job.source_id,
            JobClassification.external_id == Job.external_id,
            JobClassification.it_match == 'yes')))
    if entry_only:
        levels = ['intern','entry','early'] + (['unknown'] if include_unknown_entry else [])
        conditions.append(exists(select(JobClassification.id).where(
            JobClassification.source_id == Job.source_id,
            JobClassification.external_id == Job.external_id,
            JobClassification.career.in_(levels))))
    if skills:
        score=score_expression(skills)
        conditions.append(score >= (len(skills) if skill_mode=="all" else 1))
    if role=="backend_infra":
        conditions.append(Job.title.regexp_match(BACKEND_TITLE))
    elif role:
        patterns={'backend':r'(?i)백엔드|back.?end', 'server':r'(?i)서버|server',
                  'infra':INFRA_TITLE}
        conditions.append(Job.title.regexp_match(patterns[role]))
    return conditions


@app.get("/api/jobs", response_model=dict)
def search_jobs(
    db: SessionDep,
    keyword: str | None = Query(None, max_length=150),
    source_id: str | None = None,
    recruitment_type: str | None = None,
    employment_type: EmploymentFilter = '',
    application_status: ApplicationStatus | Literal[''] = '',
    region: str | None = None,
    open_only: bool = False,
    it_only: bool = False,
    entry_only: bool = False,
    include_unknown_entry: bool = False,
    bookmarked_only: bool = False,
    korea_only: bool = False,
    skills: str = Query("",max_length=1200),
    skill_mode: Literal["any","all"] = "any",
    role: RoleFilter = "",
    sort: Literal["newest", "deadline", "match"] = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:selected_skills=normalize_skills(skills)
    except ValueError as exc:raise HTTPException(422,str(exc))
    if selected_skills:ensure_skill_index(db)
    conditions = job_conditions(keyword=keyword, source_id=source_id, recruitment_type=recruitment_type,
        region=region, open_only=open_only, it_only=it_only, entry_only=entry_only,
        include_unknown_entry=include_unknown_entry, bookmarked_only=bookmarked_only, korea_only=korea_only,
        skills=selected_skills,skill_mode=skill_mode,role=role,
        employment_type=employment_type,application_status=application_status)
    statement = select(Job).where(*conditions)
    total = db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    if sort == "match" and selected_skills:
        statement = statement.order_by(score_expression(selected_skills).desc(),Job.posted_on.desc(),Job.id.desc())
    elif sort == "deadline":
        statement = statement.order_by(Job.deadline_on.is_(None), Job.deadline_on.asc(), Job.id.desc())
    else:
        statement = statement.order_by(Job.posted_on.desc(), Job.id.desc())
    jobs = db.scalars(statement.offset((page - 1) * page_size).limit(page_size)).all()
    ids = [(j.source_id, j.external_id) for j in jobs]
    labels = db.scalars(select(JobClassification).where(
        tuple_(JobClassification.source_id, JobClassification.external_id).in_(ids))).all() if ids else []
    by_key = {(v.source_id,v.external_id):v for v in labels}
    items = []
    saved_ids = set(ControlStore().bookmarks())
    applications = ControlStore().applications()
    for job in jobs:
        d = JobOut.model_validate(job).model_dump(mode='json')
        d['bookmarked'] = job.id in saved_ids
        d['application'] = applications.get(job.id, {'status':'none','note':''})
        d['skill_match'] = match_details(job,selected_skills) if selected_skills else None
        label = by_key.get((job.source_id,job.external_id))
        d['classification'] = ({'it_match':label.it_match, 'career':label.career,
                                'min_years':label.min_years,'max_years':label.max_years,
                                'reasons':label.reasons} if label else None)
        items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: SessionDep):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Posting not found")
    return job


@app.get("/api/duplicate-candidates")
def cross_site_duplicates(db: SessionDep):
    """High-confidence matching URL candidates; does not mutate or merge jobs.

    MVP scans existing rows; optimize with a normalized URL index at scale.
    """
    jobs = db.scalars(select(Job).where(Job.apply_url.is_not(None))).all()
    return duplicate_candidates(jobs)


@app.get("/api/duplicate-groups")
def grouped_duplicates(db: SessionDep, limit: int = Query(5000, ge=1, le=20000)):
    jobs = db.scalars(select(Job).where(Job.apply_url.is_not(None))
                      .order_by(Job.id.desc()).limit(limit)).all()
    return {'scanned':len(jobs), 'groups':exact_duplicate_groups(jobs)}


@app.get("/api/similar-candidates")
def similar_candidates(db: SessionDep, limit: int = Query(1000, ge=1, le=5000)):
    """Conservative title/company/deadline/region candidates. Never modifies jobs."""
    jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()
    return {"scanned": len(jobs), "candidates": indexed_similar_job_candidates(jobs)}


@app.get("/api/crawl-runs")
def crawl_runs(db: SessionDep, limit: int = Query(20, ge=1, le=100)):
    runs = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(limit)).all()
    return [{"id": r.id, "source_id": r.source_id, "status": r.status,
             "seen": r.seen_count, "created": r.created_count, "updated": r.updated_count,
             "failed": r.failed_count, "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in runs]


@app.get("/api/verification/summary")
def verification_summary(db: SessionDep):
    """Counts of stored QA snapshots; never claims full live coverage."""
    return verification_overview(db)


@app.get("/api/verification/recent")
def verification_recent(db: SessionDep, limit: int = Query(50, ge=1, le=500)):
    """Latest sample verification per source, no secrets or raw HTML."""
    return {'items': latest_reports(db, limit)}


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    auto_collect: bool | None = None
    interval_hours: int | None = Field(None, ge=1, le=168)
    max_items: int | None = Field(None, ge=1, le=1000)
    render_js: bool | None = None


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_keys: list[str] | None = Field(None, max_length=250)
    failed_only: bool = False


class ToggleRequest(BaseModel):
    enabled: bool


class DevelopmentFocusRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['development', 'all'] = 'development'
    urls: list[str] = Field(default_factory=list, max_length=200)


@app.post('/api/zighang-focus')
def zighang_focus(body: DevelopmentFocusRequest):
    from .aggregate_store import AggregateStore
    from .development_focus import candidate_links
    try:
        rows = candidate_links(body.urls)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store = AggregateStore()
    store.add_focus('zighang', rows, provenance='user_selected_public_link')
    store.meta('zighang', 'collection_mode', body.mode)
    queued = ControlStore().enqueue(['zighang:all'])
    return {'mode': body.mode, 'selected': len(rows), 'queued': queued,
            'inventory': store.focus_summary('zighang'), 'exhaustive': False}


@app.get('/api/control')
def control_status():
    return ControlStore().snapshot()


@app.get('/api/cloud-sync')
def cloud_sync_status():
    from .cloud import sync_status
    return sync_status()


@app.post('/api/cloud-sync')
async def cloud_sync_now():
    from .cloud import sync_once
    return await sync_once()


@app.get('/api/targets')
def collection_targets():
    return {'items': ControlStore().targets()}


@app.patch('/api/settings')
def settings_update(body: SettingsUpdate):
    return ControlStore().save_settings(body.model_dump(exclude_none=True))


@app.post('/api/collect', status_code=202)
def collect(body: CollectRequest):
    store = ControlStore()
    known = {t['key'] for t in store.targets()}
    if body.target_keys is not None and not set(body.target_keys).issubset(known):
        raise HTTPException(422, 'Unknown collection target')
    return {'queued': store.enqueue(body.target_keys, failed_only=body.failed_only)}


@app.post('/api/collect/stop')
def stop_collection():
    store = ControlStore()
    store.save_settings({'auto_collect': False})
    return {'cancelled': store.cancel_pending(), 'note': '현재 처리 중인 출처가 끝나면 멈춥니다.'}


@app.patch('/api/targets/{key}')
def target_enabled(key: str, body: ToggleRequest):
    try:
        ControlStore().enable(key, body.enabled)
    except KeyError:
        raise HTTPException(404, 'Unknown target')
    return {'key': key, 'enabled': body.enabled}


@app.put('/api/bookmarks/{job_id}')
def bookmark(job_id: int, body: ToggleRequest, db: SessionDep):
    if db.get(Job, job_id) is None:
        raise HTTPException(404, 'Posting not found')
    ControlStore().bookmark(job_id, body.enabled)
    return {'job_id': job_id, 'bookmarked': body.enabled}


@app.get('/api/export.csv')
def export_jobs(db: SessionDep, bookmarked_only: bool = False,
                keyword: str | None = Query(None,max_length=150), source_id: str | None = None,
                recruitment_type: str | None = None, region: str | None = None,
                employment_type: EmploymentFilter = '', application_status: ApplicationStatus | Literal[''] = '',
                open_only: bool = False, it_only: bool = False, entry_only: bool = False,
                include_unknown_entry: bool = False, korea_only: bool = False,
                sort: Literal['newest','deadline','match'] = 'newest',
                skills: str = Query('',max_length=1200), skill_mode: Literal['any','all']='any',
                role: RoleFilter=''):
    import csv
    import io
    try:selected_skills=normalize_skills(skills)
    except ValueError as exc:raise HTTPException(422,str(exc))
    if selected_skills:ensure_skill_index(db)
    conditions=job_conditions(keyword=keyword,source_id=source_id,recruitment_type=recruitment_type,
        region=region,open_only=open_only,it_only=it_only,entry_only=entry_only,
        include_unknown_entry=include_unknown_entry,bookmarked_only=bookmarked_only,korea_only=korea_only,
        skills=selected_skills,skill_mode=skill_mode,role=role,
        employment_type=employment_type,application_status=application_status)
    statement=select(Job).where(*conditions)
    statement=statement.order_by(Job.deadline_on.is_(None),Job.deadline_on.asc(),Job.id.desc()) if sort=='deadline' else statement.order_by(Job.posted_on.desc(),Job.id.desc())
    if sort=='match' and selected_skills:statement=statement.order_by(None).order_by(score_expression(selected_skills).desc(),Job.posted_on.desc(),Job.id.desc())
    statement=statement.limit(10000)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['제목', '회사', '지역', '마감일', '상태', '원문', '출처'])
    for job in db.scalars(statement):
        state='closed' if job.source_status=='closed' or (job.deadline_on and job.deadline_on<datetime.now(ZoneInfo('Asia/Seoul')).date()) else job.source_status
        values = [job.title, job.organization, job.region, job.deadline_on, state, job.original_url, job.source_id]
        writer.writerow([("'" + str(v)) if str(v or '').lstrip().startswith(('=', '+', '-', '@')) else str(v or '') for v in values])
    return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="recruit-radar.csv"'})


@app.get('/api/metrics')
def metrics(db: SessionDep):
    from .database import HttpMetric
    from collections import defaultdict
    rows = db.scalars(select(HttpMetric).order_by(HttpMetric.id.desc()).limit(10000)).all()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.host].append(row)
    items = []
    for host, values in sorted(grouped.items()):
        times = sorted(v.elapsed_ms for v in values)
        items.append({'host': host, 'requests': len(values),
                      'not_modified': sum(v.status == 304 for v in values),
                      'rate_limited': sum(v.status == 429 for v in values),
                      'errors': sum(v.status == 0 or v.status >= 400 for v in values),
                      'p95_ms': round(times[min(len(times)-1, int(len(times)*.95))], 1)})
    return {'window': 'latest_10000_requests', 'items': items}


@app.post('/api/backup')
def backup():
    from .maintenance import create_backup
    return create_backup()


class SkillProfile(BaseModel):
    skills: list[str] = Field(default_factory=list,max_length=30)
    role: RoleFilter = ''


@app.get('/api/skill-profile')
def read_skill_profile():
    with ControlStore().connection() as db:
        row=db.execute("SELECT value FROM settings WHERE key='skill_profile'").fetchone()
    import json
    return json.loads(row[0]) if row else {'skills':[],'role':''}


@app.put('/api/skill-profile')
def save_skill_profile(body:SkillProfile):
    import json
    try:skills=normalize_skills(body.skills)
    except ValueError as exc:raise HTTPException(422,str(exc))
    profile={'skills':skills,'role':body.role}
    with ControlStore().connection() as db:
        db.execute("INSERT INTO settings(key,value) VALUES('skill_profile',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(profile,ensure_ascii=False),))
    return profile


@app.get('/api/skill-catalog')
def skill_catalog():
    from .skill_matching import CATALOG
    return [{'name':name,'aliases':aliases} for name,aliases in CATALOG.items()]


@app.get('/api/job-analysis')
def analyze_jobs(db: SessionDep, keyword: str | None=None, source_id: str | None=None,
                 recruitment_type: str | None=None, region: str | None=None,
                 employment_type: EmploymentFilter='', application_status: ApplicationStatus | Literal['']='',
                 open_only: bool=True, it_only: bool=False, entry_only: bool=False,
                 include_unknown_entry: bool=False, bookmarked_only: bool=False,
                 korea_only: bool=False, skills: str=Query('',max_length=1200),
                 skill_mode: Literal['any','all']='any', role: RoleFilter=''):
    from .job_analysis import summarize_jobs
    try: selected=normalize_skills(skills)
    except ValueError as exc: raise HTTPException(422,str(exc))
    if selected: ensure_skill_index(db)
    conditions=job_conditions(keyword=keyword,source_id=source_id,recruitment_type=recruitment_type,
        region=region,open_only=open_only,it_only=it_only,entry_only=entry_only,
        include_unknown_entry=include_unknown_entry,bookmarked_only=bookmarked_only,
        korea_only=korea_only,skills=selected,skill_mode=skill_mode,role=role,
        employment_type=employment_type,application_status=application_status)
    total=db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    jobs=db.scalars(select(Job).where(*conditions).order_by(Job.posted_on.desc(),Job.id.desc()).execution_options(yield_per=300))
    return summarize_jobs(jobs,selected,total)


class ApplicationUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: ApplicationStatus = 'none'
    note: str = Field('', max_length=5000)


@app.get('/api/jobs/{job_id}/application')
def read_application(job_id: int, db: SessionDep):
    if db.get(Job, job_id) is None:
        raise HTTPException(404, 'Posting not found')
    return ControlStore().applications().get(job_id, {'job_id':job_id, 'status':'none','note':''})


@app.put('/api/jobs/{job_id}/application')
def update_application(job_id: int, body: ApplicationUpdate, db: SessionDep):
    if db.get(Job, job_id) is None:
        raise HTTPException(404, 'Posting not found')
    return ControlStore().save_application(job_id, body.status, body.note)


@app.get('/api/jobs/{job_id}/analysis')
def analyze_posting(job_id: int, db: SessionDep):
    from .posting_analysis import structured_analysis
    job=db.get(Job,job_id)
    if job is None:raise HTTPException(404,'Posting not found')
    return structured_analysis(job)


@app.get('/api/compare')
def compare_jobs(db: SessionDep, ids: str=Query(...,max_length=100)):
    from .posting_analysis import compare_postings
    try: selected=list(dict.fromkeys(int(v) for v in ids.split(',')))
    except ValueError:raise HTTPException(422,'Expected comma-separated posting IDs')
    if not 2<=len(selected)<=4:raise HTTPException(422,'Select two to four postings')
    jobs=[db.get(Job,i) for i in selected]
    if any(j is None for j in jobs):raise HTTPException(404,'Posting not found')
    return compare_postings(jobs)


@app.get('/api/vector-status')
def vector_status(db: SessionDep):
    from .vector_index import index_status
    return index_status(db)


@app.get('/api/jobs/{job_id}/similar')
def similar_postings(job_id: int, db: SessionDep, open_only: bool=True,
                     entry_only: bool=False, role: RoleFilter='', region: str | None=None,
                     employment_type: EmploymentFilter='', limit: int=Query(10,ge=1,le=30)):
    from .vector_index import nearest
    job=db.get(Job,job_id)
    if job is None:raise HTTPException(404,'Posting not found')
    try:
        return nearest(db,job,job_conditions(open_only=open_only,entry_only=entry_only,
                       role=role,region=region,employment_type=employment_type),limit)
    except ImportError:raise HTTPException(503,'Local analysis dependencies are not installed')


@app.get('/api/jobs/{job_id}/duplicates')
def review_candidates(job_id: int, db: SessionDep):
    from .duplicate_review import candidates
    job=db.get(Job,job_id)
    if job is None:raise HTTPException(404,'Posting not found')
    return candidates(db,job)


class DuplicateGroupRequest(BaseModel):
    ids: list[int] = Field(min_length=2,max_length=20)


@app.get('/api/reviewed-duplicates')
def reviewed_duplicates(db: SessionDep):
    groups,_=ControlStore().duplicate_reviews()
    return {'groups':[{'id':key,'items':[JobOut.model_validate(job).model_dump(mode='json')
        for i in members if (job:=db.get(Job,i)) is not None]} for key,members in groups.items()]}


@app.post('/api/reviewed-duplicates')
def confirm_duplicate_group(body: DuplicateGroupRequest, db: SessionDep):
    ids=list(dict.fromkeys(body.ids))
    if len(ids)<2:raise HTTPException(422,'Select distinct postings')
    if any(db.get(Job,i) is None for i in ids):raise HTTPException(404,'Posting not found')
    return {'id':ControlStore().group_duplicates(ids)}


@app.delete('/api/reviewed-duplicates/{group_id}')
def split_duplicate_group(group_id: str):
    try:ControlStore().split_duplicates(group_id)
    except KeyError:raise HTTPException(404,'Group not found')
    return {'split':True}


@app.put('/api/jobs/{job_id}/not-duplicate/{other_id}')
def reject_duplicate_pair(job_id: int, other_id: int, db: SessionDep):
    if job_id==other_id:raise HTTPException(422,'Select distinct postings')
    if db.get(Job,job_id) is None or db.get(Job,other_id) is None:raise HTTPException(404,'Posting not found')
    ControlStore().reject_duplicate(job_id,other_id)
    return {'rejected':True}
