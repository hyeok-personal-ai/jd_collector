"""Durable local control plane: targets, queue, preferences and bookmarks."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .board_catalog import load_boards
from .company_catalog import load_companies


def data_dir():
    path = Path(os.getenv('RADAR_DATA_DIR', 'data')).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


class ControlStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'control.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Control database requires a newer app version')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS targets (
                key TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'not_checked', checked_at REAL, next_due REAL NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, target_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                queued_at REAL NOT NULL, started_at REAL, finished_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, run_id INTEGER);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks(target_key) WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bookmarks (job_id INTEGER PRIMARY KEY, saved_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS applications (job_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'none', note TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS duplicate_members (job_id INTEGER PRIMARY KEY, group_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS duplicate_rejections (a INTEGER NOT NULL,b INTEGER NOT NULL,PRIMARY KEY(a,b));
            CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL);
            PRAGMA user_version=1;
            ''')
            for key, value in {'auto_collect': True, 'interval_hours': 24, 'max_items': 100,
                               'render_js': False}.items():
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        with self.connection() as db:
            empty = db.execute('SELECT count(*) FROM targets').fetchone()[0] == 0
        if empty:
            self.sync_catalog()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def sync_catalog(self):
        specs = []
        routes_path=Path(__file__).parent/'assets/career_routes.json'
        routes=json.loads(routes_path.read_text(encoding='utf-8')) if routes_path.exists() else {}
        for source,name,url in [('inthiswork','인디스워크','https://inthiswork.com'),('zighang','직행','https://www.zighang.com/recruitment')]:
            specs.append((source+':all',{'kind':'aggregate','source':source,'name':name,'url':url,'stage':0,'company_ids':[]},True))
        for row in load_boards():
            specs.append((f"{row['provider']}:{row['id']}",
                          {'kind': 'public', 'provider': row['provider'], 'board': row['id'],
                           'name': row.get('company', row['id']), 'url': row.get('evidence', ''),
                           'stage': 4, 'company_ids': []},
                          bool(row.get('enabled', True) and row.get('focus') == 'kr_it')))
        mapped = {key: spec for key, spec, _ in specs}
        portals = {}
        for company in load_companies():
            if company.get('ats_provider'):
                key = company['ats_provider'] + ':' + company['ats_board']
                if key in mapped:
                    mapped[key]['company_ids'].append(company['id'])
                    mapped[key]['stage'] = min(mapped[key]['stage'], company['priority_stage'])
            else:
                url = company['url']
                resolved_url=routes.get(company['id'],{}).get('url',url)
                key = 'company:' + company['id']
                if url in portals or resolved_url in portals:
                    parent_key, spec = portals.get(url) or portals[resolved_url]
                    spec['company_ids'].append(company['id'])
                    spec['company']['shared_portal'] = True
                    specs.append((key, {'kind': 'alias', 'alias_of': parent_key}, False))
                else:
                    spec = {'kind': 'company', 'name': company['name'], 'url': resolved_url,
                            'stage': company['priority_stage'], 'company_ids': [company['id']],
                            'company': dict(company)}
                    portals[url] = (key, spec)
                    portals[resolved_url] = (key,spec)
                    specs.append((key, spec, False))
        with self.connection() as db:
            for key, spec, enabled in specs:
                db.execute('''INSERT INTO targets(key,spec,enabled) VALUES (?,?,?)
                    ON CONFLICT(key) DO UPDATE SET spec=excluded.spec''',
                           (key, json.dumps(spec, ensure_ascii=False), int(enabled)))
                if spec['kind'] == 'alias':
                    db.execute('UPDATE targets SET enabled=0 WHERE key=?', (key,))
                    db.execute("UPDATE tasks SET status='cancelled',finished_at=? WHERE target_key=? AND status='queued'", (time.time(), key))

    def settings(self):
        with self.connection() as db:
            settings = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}
            qa = settings.get('qa_schedule') or {}
            if qa.get('expires_at', 0) and qa['expires_at'] <= time.time():
                self._end_qa(db, settings)
            return settings

    @staticmethod
    def _end_qa(db, settings):
        qa = settings.get('qa_schedule') or {}
        for key in qa.get('keys', []):
            db.execute("UPDATE targets SET next_due=MAX(next_due,COALESCE(checked_at,?)+86400) WHERE key=? AND status IN ('completed','limited')", (time.time(),key))
        settings.update(auto_collect=True, interval_hours=24, qa_schedule={})
        for key in ('auto_collect','interval_hours','qa_schedule'):
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(settings[key])))

    def begin_qa(self, keys, interval_seconds=120, duration_seconds=900):
        if not keys or not 60 <= interval_seconds <= 600 or not 120 <= duration_seconds <= 1800:
            raise ValueError('QA는 1~10분 간격, 2~30분 동안 진행할 수 있습니다.')
        now=time.time()
        with self.connection() as db:
            for key in keys:
                row=db.execute('SELECT spec,status FROM targets WHERE key=?',(key,)).fetchone()
                if not row or json.loads(row['spec'])['kind'] != 'public' or row['status'] != 'completed':
                    raise ValueError('주기 단축은 마지막 수집이 성공한 공개 ATS 대상에만 적용합니다. 제한 출처의 휴식은 유지됩니다.')
            qa={'keys':list(dict.fromkeys(keys)),'interval_seconds':interval_seconds,'expires_at':now+duration_seconds,'started_at':now}
            for key,value in {'qa_schedule':qa,'auto_collect':True,'interval_hours':24}.items():
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
            for key in keys:
                db.execute('UPDATE targets SET next_due=? WHERE key=?',(now,key))
        return qa

    def end_qa(self):
        settings=self.settings()
        with self.connection() as db:
            self._end_qa(db,settings)
        return settings

    def save_settings(self, changes):
        # An explicit pause/settings change cancels temporary QA. Its expiry
        # must never silently turn collection back on after the user paused it.
        if ('interval_hours' in changes or changes.get('auto_collect') is False) and self.settings().get('qa_schedule'):
            self.end_qa()
        with self.connection() as db:
            for key, value in changes.items():
                db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))
        return self.settings()

    def targets(self):
        with self.connection() as db:
            return [{**dict(r), 'spec': json.loads(r['spec']), 'enabled': bool(r['enabled'])}
                    for r in db.execute('SELECT * FROM targets ORDER BY key') if json.loads(r['spec']).get('kind') != 'alias']

    def enable(self, key, enabled):
        with self.connection() as db:
            if not db.execute('UPDATE targets SET enabled=? WHERE key=?', (int(enabled), key)).rowcount:
                raise KeyError(key)
            if not enabled:
                db.execute("UPDATE tasks SET status='cancelled',finished_at=? WHERE target_key=? AND status='queued'", (time.time(), key))

    def enqueue(self, keys=None, *, due_only=False, failed_only=False):
        now = time.time()
        count = 0
        with self.connection() as db:
            for row in db.execute('SELECT * FROM targets').fetchall():
                if json.loads(row['spec']).get('kind') == 'alias':
                    continue
                if keys is not None and row['key'] not in keys:
                    continue
                if keys is None and not row['enabled']:
                    continue
                if due_only and row['next_due'] > now:
                    continue
                if failed_only and row['status'] not in ('failed', 'partial', 'blocked', 'needs_review', 'interrupted'):
                    continue
                count += db.execute('INSERT OR IGNORE INTO tasks(target_key,queued_at) VALUES (?,?)',
                                    (row['key'], now)).rowcount
        return count

    def claim(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # Aggregators lead fresh work, but their endless follow-up batches
            # must not starve an older company request. After two 120s batch
            # budgets, waiting tasks receive FIFO priority at the next claim.
            row = db.execute("""SELECT * FROM tasks WHERE status='queued'
                ORDER BY CASE WHEN queued_at<=? THEN 0
                    WHEN target_key='inthiswork:all' THEN 1
                    WHEN target_key='zighang:all' THEN 2 ELSE 3 END,id LIMIT 1""",
                (time.time()-240,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',started_at=?,finished_at=NULL,error=NULL,run_id=NULL,attempts=attempts+1 WHERE id=?", (time.time(), row['id']))
            target = db.execute('SELECT * FROM targets WHERE key=?', (row['target_key'],)).fetchone()
            claimed=db.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
            return {**dict(claimed), 'spec': json.loads(target['spec'])}

    def finish(self, task, *, status, seen=0, created=0, updated=0, error='', run_id=None):
        now = time.time()
        settings = self.settings()
        interval = settings['interval_hours'] * 3600
        qa = settings.get('qa_schedule') or {}
        if task['target_key'] in qa.get('keys', []) and qa.get('expires_at',0)>now:
            interval = qa['interval_seconds']
        success = status in ('completed', 'limited')
        with self.connection() as db:
            target = db.execute('SELECT failures FROM targets WHERE key=?', (task['target_key'],)).fetchone()
            failures = 0 if success else target['failures'] + 1
            # Blocked/layout errors should not hammer the same endpoint every few minutes.
            delay = interval if success else min(72 * 3600, 3600 * 2 ** min(failures, 7))
            if not success and 'robots.txt check returned' in error.lower():
                # No content requests until the next successful robots check.
                # Revisit a temporarily unavailable rules document with a bounded cooldown.
                delay=min(settings['interval_hours']*3600,max(1800,300*2**min(failures,7)))
            if not success and 'browser security challenge' in error.lower():
                delay=min(86400,max(21600,interval))
            db.execute('''UPDATE targets SET status=?,checked_at=?,next_due=?,failures=?,seen=?,created=?,updated=?,error=? WHERE key=?''',
                       (status, now, now + delay, failures, seen, created, updated, error[:1000], task['target_key']))
            db.execute('UPDATE tasks SET status=?,finished_at=?,error=?,run_id=? WHERE id=?',
                       (status, now, error[:1000], run_id, task['id']))

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE tasks SET status=CASE WHEN attempts<3 THEN 'queued' ELSE 'interrupted' END,error='Previous worker stopped',finished_at=? WHERE status='running'", (time.time(),))

    def heartbeat(self):
        with self.connection() as db:
            db.execute('INSERT INTO worker VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat', (time.time(),))

    def snapshot(self):
        with self.connection() as db:
            worker = db.execute('SELECT heartbeat FROM worker WHERE id=1').fetchone()
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM tasks GROUP BY status')}
            recent = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 30')]
        return {'worker_online': bool(worker and time.time() - worker['heartbeat'] < 30),
                'settings': self.settings(), 'counts': counts, 'recent': recent}

    def cancel_pending(self):
        with self.connection() as db:
            return db.execute("UPDATE tasks SET status='cancelled',finished_at=? WHERE status='queued'", (time.time(),)).rowcount

    def bookmarks(self):
        with self.connection() as db:
            return [r['job_id'] for r in db.execute('SELECT job_id FROM bookmarks ORDER BY saved_at DESC')]

    def applications(self, status=None):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM applications' + (' WHERE status=?' if status else ''),
                              (status,) if status else ()).fetchall()
            return {r['job_id']:dict(r) for r in rows}

    def save_application(self, job_id, status, note):
        with self.connection() as db:
            db.execute('''INSERT INTO applications VALUES (?,?,?,?) ON CONFLICT(job_id)
                DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at''',
                       (job_id,status,note,time.time()))
        return self.applications()[job_id]

    def duplicate_reviews(self):
        with self.connection() as db:
            groups={}
            for row in db.execute('SELECT * FROM duplicate_members ORDER BY group_id,job_id'):
                groups.setdefault(row['group_id'],[]).append(row['job_id'])
            rejected={tuple(r) for r in db.execute('SELECT a,b FROM duplicate_rejections')}
        return groups,rejected

    def group_duplicates(self, ids):
        import uuid
        from itertools import combinations
        group_id=uuid.uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            placeholders=','.join('?' for _ in ids)
            previous=[r[0] for r in db.execute(f'SELECT DISTINCT group_id FROM duplicate_members WHERE job_id IN ({placeholders})',ids)]
            members=set(ids)
            for group in previous:
                members.update(r[0] for r in db.execute('SELECT job_id FROM duplicate_members WHERE group_id=?',(group,)))
            db.executemany('INSERT OR REPLACE INTO duplicate_members VALUES (?,?)',[(i,group_id) for i in members])
            db.executemany('DELETE FROM duplicate_rejections WHERE a=? AND b=?',list(combinations(sorted(members),2)))
        return group_id

    def split_duplicates(self, group_id):
        from itertools import combinations
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            ids=[r[0] for r in db.execute('SELECT job_id FROM duplicate_members WHERE group_id=? ORDER BY job_id',(group_id,))]
            if not ids:raise KeyError(group_id)
            db.executemany('INSERT OR IGNORE INTO duplicate_rejections VALUES (?,?)',list(combinations(ids,2)))
            db.execute('DELETE FROM duplicate_members WHERE group_id=?',(group_id,))

    def reject_duplicate(self,a,b):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO duplicate_rejections VALUES (?,?)',tuple(sorted((a,b))))

    def bookmark(self, job_id, saved):
        with self.connection() as db:
            if saved:
                db.execute('INSERT OR IGNORE INTO bookmarks VALUES (?,?)', (job_id, time.time()))
            else:
                db.execute('DELETE FROM bookmarks WHERE job_id=?', (job_id,))
