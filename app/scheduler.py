"""Opt-in, restart-safe ATS polling scheduler with durable per-board leases.

CLI 'plan' NEVER makes network requests. 'run --live' only visits registered
public-ATS endpoints via existing robots-aware adapters. Unverified company
homepage URLs are *not* scraped. SQLite is a local single-machine planner;
for distributed deployments migrate locking to PostgreSQL advisory locks.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from .board_catalog import load_boards
from .company_catalog import load_companies

PROVIDER_ENV = {'greenhouse':'PUBLIC_GREENHOUSE_BOARDS','lever':'PUBLIC_LEVER_SITES',
                'ashby':'PUBLIC_ASHBY_BOARDS','smartrecruiters':'PUBLIC_SMARTRECRUITERS_COMPANIES'}
# Separate jobs per provider but only one concurrent request stream per provider host.
INTERVALS_SECONDS = {1:2*3600,2:3*3600,3:6*3600,4:8*3600,5:24*3600}
DEFAULT_DB='rr-polling.sqlite3'


def active_boards() -> list[dict]:
    """Only registered public ATS boards; ignore all link-only career URLs."""
    preferred = {}
    for c in load_companies():
        if 'ats_provider' in c:
            key=(c['ats_provider'],c['ats_board'])
            preferred[key]=min(c['priority_stage'],preferred.get(key,5))
    result=[]
    for b in load_boards():
        if not b.get('enabled',True):
            continue
        tier=preferred.get((b['provider'],b['id']),4 if b.get('focus')=='kr_it' else 5)
        result.append({'key':f"{b['provider']}:{b['id']}", 'provider':b['provider'],
                       'board':b['id'],'company':b.get('company',b['id']),
                       'stage':tier,'interval':INTERVALS_SECONDS[tier]})
    return sorted(result,key=lambda x:(x['stage'],x['key']))


def _jitter(key: str, interval: int) -> int:
    """Stable 0-10% dispersion without random process-specific scheduling."""
    return int(int(hashlib.sha256(key.encode()).hexdigest()[:8],16)/0xffffffff * interval *.10)


class PollPlanner:
    def __init__(self,path: str | Path=DEFAULT_DB):
        self.path = str(path)
        with self._conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS poll_state (
                source_key TEXT PRIMARY KEY, last_success REAL,
                next_due REAL NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT 'never',
                unchanged_streak INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, last_message TEXT
            );
            CREATE TABLE IF NOT EXISTS origin_locks (
                provider TEXT PRIMARY KEY, lease_until REAL NOT NULL DEFAULT 0,
                source_key TEXT NOT NULL
            );
            ''')
            # Safe migration for pre-v1.9 local polling DBs.
            names={row[1] for row in c.execute('PRAGMA table_info(poll_state)')}
            if 'unchanged_streak' not in names:
                c.execute('ALTER TABLE poll_state ADD COLUMN unchanged_streak INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def _conn(self):
        c=sqlite3.connect(self.path,timeout=10, isolation_level=None)
        try:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA busy_timeout=10000')
            yield c
        finally:
            c.close()

    def plan(self, *, now: float | None=None) -> list[dict]:
        now=time.time() if now is None else now
        boards=active_boards()
        with self._conn() as c:
            states={r['source_key']:dict(r) for r in c.execute('SELECT * FROM poll_state')}
        result=[]
        for b in boards:
            state=states.get(b['key'],{})
            item={**b,'next_due':state.get('next_due',0),'failures':state.get('failures',0),
                  'last_status':state.get('last_status','never'),
                  'unchanged_streak':state.get('unchanged_streak',0)}
            item['due']=item['next_due']<=now and state.get('lease_until',0)<=now
            result.append(item)
        return sorted(result,key=lambda x:(not x['due'],x['stage'],x['next_due'],x['key']))

    def acquire(self, board: dict, *, now: float | None=None, lease_seconds: int=1800) -> str | None:
        now=time.time() if now is None else now
        if lease_seconds<1:
            raise ValueError('lease_seconds must be positive')
        # Atomic transaction prevents concurrent workers from leasing same
        # board OR different boards on the same ATS API domain.
        token=uuid.uuid4().hex
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            k,p=board['key'],board['provider']
            r=c.execute('SELECT next_due,lease_until FROM poll_state WHERE source_key=?',(k,)).fetchone()
            origin=c.execute('SELECT lease_until FROM origin_locks WHERE provider=?',(p,)).fetchone()
            if (r and (r['next_due']>now or r['lease_until']>now)) or (origin and origin['lease_until']>now):
                c.execute('ROLLBACK')
                return None
            c.execute('''INSERT INTO poll_state (source_key,lease_until,lease_token,last_status) VALUES (?,?,?,'running')
              ON CONFLICT(source_key) DO UPDATE SET lease_until=excluded.lease_until,
              lease_token=excluded.lease_token,last_status='running' ''',
                      (k,now+lease_seconds,token))
            c.execute('''INSERT INTO origin_locks(provider,lease_until,source_key) VALUES(?,?,?)
              ON CONFLICT(provider) DO UPDATE SET lease_until=excluded.lease_until,source_key=excluded.source_key''',
                      (p,now+lease_seconds,k))
            c.execute('COMMIT')
            return token

    def complete(self, board: dict, *, token: str, success: bool, now: float | None=None,
                 message: str='', change_count: int | None=None):
        now=time.time() if now is None else now
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT failures,unchanged_streak FROM poll_state WHERE source_key=? AND lease_token=?',
                        (board['key'],token)).fetchone()
            if not r:
                c.execute('ROLLBACK')
                raise ValueError('Task has not been leased')
            failures=0 if success else r['failures']+1
            streak=r['unchanged_streak']
            if success and change_count is not None:
                if change_count<0: raise ValueError('change_count must be nonnegative')
                streak=streak+1 if change_count==0 else 0
            # If 3/6 consecutive *complete and nonempty* snapshots have no new/changed jobs,
            # adapt poll interval to 2x/4x (max 24h); any change resets it.
            multiplier=min(4,2**(streak//3)) if success and change_count is not None else 1
            interval=min(24*3600,board['interval']*multiplier)
            delay=(interval + _jitter(board['key'],interval)) if success else min(24*3600,300*2**min(failures-1,8))
            c.execute('''UPDATE poll_state SET next_due=?,failures=?,unchanged_streak=?,lease_until=0,lease_token=NULL,
              last_success=CASE WHEN ? THEN ? ELSE last_success END,
              last_status=?,last_message=? WHERE source_key=? AND lease_token=?''',
                      (now+delay,failures,streak,int(success),now,'ok' if success else 'error',message[:300],board['key'],token))
            c.execute('DELETE FROM origin_locks WHERE provider=? AND source_key=?',
                      (board['provider'],board['key']))
            c.execute('COMMIT')

    def renew(self, board: dict, *, token: str, now: float | None = None, lease_seconds: int = 1800) -> bool:
        now = time.time() if now is None else now
        if lease_seconds < 1:
            raise ValueError('lease_seconds must be positive')
        with self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT lease_until FROM poll_state WHERE source_key=? AND lease_token=?',
                            (board['key'], token)).fetchone()
            origin = c.execute('SELECT source_key,lease_until FROM origin_locks WHERE provider=?',
                               (board['provider'],)).fetchone()
            if not row or row['lease_until'] <= now or not origin or origin['source_key'] != board['key'] or origin['lease_until'] <= now:
                c.execute('ROLLBACK')
                return False
            c.execute('UPDATE poll_state SET lease_until=? WHERE source_key=? AND lease_token=?',
                      (now + lease_seconds, board['key'], token))
            c.execute('UPDATE origin_locks SET lease_until=? WHERE provider=? AND source_key=?',
                      (now + lease_seconds, board['provider'], board['key']))
            c.execute('COMMIT')
            return True


async def collect_leased(manager, board, token):
    task = asyncio.create_task(collect_one(board))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=60)
            if not done and not manager.renew(board, token=token):
                raise RuntimeError('Collection lease lost')
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def collect_one(board: dict):
    from .crawlers.registry import CRAWLERS
    from .runner import run_crawler
    provider=board['provider']
    crawler=CRAWLERS['public_'+provider]()
    crawler.boards=[board['board']]
    return await run_crawler(crawler)


def main():
    from .console import configure_console
    configure_console()
    parser=argparse.ArgumentParser(description='Safe incremental ATS board planner')
    parser.add_argument('command',choices=['plan','run'])
    parser.add_argument('--db',default=DEFAULT_DB)
    parser.add_argument('--max-boards',type=int,default=5)
    parser.add_argument('--live',action='store_true',help='Explicitly allow public feed network GETs')
    args=parser.parse_args()
    if not 1<=args.max_boards<=1000:
        parser.error('max-boards must be 1..1000')
    manager=PollPlanner(args.db)
    plan=manager.plan()
    if args.command=='plan':
        print(json.dumps({'registered_public_boards':len(plan),'due':sum(p['due'] for p in plan),
            'next_tasks':plan[:args.max_boards],
            'link_only_company_sites_not_crawled':sum('ats_provider' not in c for c in load_companies())},
            ensure_ascii=False,indent=2))
        return
    if not args.live:
        parser.error('run requires --live; use plan for offline scheduling')
    count=0
    errors=0
    for board in plan:
        if not board['due'] or count>=args.max_boards:
            break
        token=manager.acquire(board)
        if not token:
            continue
        count+=1
        try:
            result=asyncio.run(collect_leased(manager,board,token))
            success=result.status=='completed' and result.failed_count==0
            errors += int(not success)
            manager.complete(board,token=token,success=success,message=result.error_summary or result.status,
                             change_count=(result.created_count+result.updated_count) if success and result.seen_count>0 else None)
            print(board['key'], result.status, 'seen',result.seen_count,'errors',result.failed_count)
        except Exception as exc:
            errors += 1
            manager.complete(board,token=token,success=False,message=f'{type(exc).__name__}: {str(exc)[:200]}')
            print(board['key'],'error',str(exc)[:140])
    print('attempted',count)
    if errors:
        raise SystemExit(1)

if __name__=='__main__':
    main()
