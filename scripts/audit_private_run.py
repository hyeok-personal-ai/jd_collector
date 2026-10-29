"""Actions-only audit. Standard library; no posting bodies in reports or logs."""
import base64
import gzip
import io
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def api(path, payload=None, missing_ok=False):
    args=['gh','api',path]
    if payload is not None:args+=['--method','PUT','--input','-']
    result=subprocess.run(args,input=json.dumps(payload) if payload is not None else None,
                          capture_output=True,text=True,encoding='utf-8',timeout=90)
    if result.returncode:
        if missing_ok and 'HTTP 404' in result.stderr:return None
        raise RuntimeError('GitHub audit API request failed')
    return json.loads(result.stdout)


def assessment(source):
    status=source.get('status','missing')
    failure=source.get('failure') or {}
    responses=[]
    for row in (source.get('responses') or [])[-3:]:
        path=row.get('path','')
        if not re.fullmatch(r'/[a-zA-Z0-9_./-]{0,200}',path):path='redacted'
        responses.append({'path':path,'status':row.get('status'),
                          'pagination':{k:v for k,v in (row.get('pagination') or {}).items() if k in ('page','per_page') and type(v) is int},
                          'browser_challenge':row.get('browser_challenge') is True})
    if status=='completed':reason='completed'
    elif status=='limited':reason='budget_resume'  # Expected continuation, not a failed run.
    elif failure.get('kind')=='browser_challenge' or any(r['browser_challenge'] for r in responses):reason='site_browser_challenge'
    elif failure.get('kind')=='robots_disallow':reason='robots_disallow'
    elif failure.get('http_status')==429:reason='rate_limited'
    elif failure.get('http_status') in (401,403):reason='access_denied_rule_unknown'
    else:reason='insufficient_evidence'
    return {'status':status,'reason':reason,'http_status':failure.get('http_status'),
            'stage':failure.get('stage'),'responses':responses}


def report_for(run,doc,collect_result):
    generated=datetime.fromisoformat(doc['generated_at'].replace('Z','+00:00')) if doc else None
    started=datetime.fromisoformat(run['created_at'].replace('Z','+00:00'))
    fresh=bool(generated and generated.tzinfo and generated>=started)
    sources={s:assessment((doc.get('source_status') or {}).get(s,{})) for s in ('inthiswork','zighang')} if fresh else {}
    healthy=collect_result=='success' and fresh and all(s['status'] in ('completed','limited') for s in sources.values())
    return {'version':1,'run_id':str(run['id']),'checked_at':datetime.now(timezone.utc).isoformat(),
            'collect_result':collect_result,'snapshot_fresh':fresh,'healthy':healthy,
            'reason':'source_results' if fresh else 'no_fresh_snapshot_or_collection_failed',
            'sources':sources}


def put_report(repo,path,report):
    old=api(f'repos/{repo}/contents/{path}?ref=state',missing_ok=True)
    payload={'message':'Record private collection audit','branch':'state',
             'content':base64.b64encode(json.dumps(report,ensure_ascii=False,indent=2).encode()).decode()}
    if old:payload['sha']=old['sha']
    api(f'repos/{repo}/contents/{path}',payload)


def main():
    repo=os.environ['GITHUB_REPOSITORY'];run_id=os.environ['GITHUB_RUN_ID']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo) or not run_id.isdecimal():raise ValueError('Invalid audit identity')
    if api(f'repos/{repo}').get('private') is not True:raise PermissionError('Private repository required')
    run=api(f'repos/{repo}/actions/runs/{run_id}')
    doc=None
    try:
        meta=api(f'repos/{repo}/contents/snapshot.json.gz?ref=state')
        if meta.get('size',0)>25_000_000:raise ValueError('Snapshot too large')
        if meta.get('encoding')!='base64':meta=api(f'repos/{repo}/git/blobs/{meta["sha"]}')
        blob=base64.b64decode(meta['content'])
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:raw=stream.read(100_000_001)
        if len(raw)>100_000_000:raise ValueError('Snapshot too large')
        doc=json.loads(raw)
    except Exception:
        pass  # Audit still records workflow failure when state cannot be read.
    report=report_for(run,doc,os.environ.get('COLLECT_RESULT','unknown'))
    jobs=api(f'repos/{repo}/actions/runs/{run_id}/jobs?per_page=100')
    report['failed_steps']=[{'job':j['name'][:100],'step':s['name'][:100],'conclusion':s['conclusion']}
        for j in jobs.get('jobs',[]) for s in j.get('steps',[]) if s.get('conclusion') in ('failure','cancelled','timed_out')]
    put_report(repo,f'audits/{run_id}.json',report)
    put_report(repo,'latest-audit.json',report)
    summary='## Private collection audit\n\n'+('Healthy; unfinished inventory resumes next run.' if report['healthy'] else 'Collection needs attention. See private audit record.')+'\n\n'
    summary+='\n'.join(f'- {source}: {v["status"]} / {v["reason"]}' for source,v in report['sources'].items())
    summary+=f'\n\nPrivate state record: `audits/{run_id}.json`\n'
    if os.getenv('GITHUB_STEP_SUMMARY'):Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(summary,encoding='utf-8')
    print(json.dumps({'audit_saved':True,'healthy':report['healthy'],'sources':report['sources']}))
    return 0 if report['healthy'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'audit_saved':False,'error_type':type(exc).__name__}))
        raise SystemExit(1)
