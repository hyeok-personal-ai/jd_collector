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
            return {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}

    def save_settings(self, changes):
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
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY CASE WHEN target_key='inthiswork:all' THEN 0 WHEN target_key='zighang:all' THEN 1 ELSE 2 END,id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',started_at=?,finished_at=NULL,error=NULL,run_id=NULL,attempts=attempts+1 WHERE id=?", (time.time(), row['id']))
            target = db.execute('SELECT * FROM targets WHERE key=?', (row['target_key'],)).fetchone()
            claimed=db.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
            return {**dict(claimed), 'spec': json.loads(target['spec'])}

    def finish(self, task, *, status, seen=0, created=0, updated=0, error='', run_id=None):
        now = time.time()
        interval = self.settings()['interval_hours'] * 3600
        success = status in ('completed', 'limited')
        with self.connection() as db:
            target = db.execute('SELECT failures FROM targets WHERE key=?', (task['target_key'],)).fetchone()
            failures = 0 if success else target['failures'] + 1
            # Blocked/layout errors should not hammer the same endpoint every few minutes.
            delay = interval if success else min(72 * 3600, 3600 * 2 ** min(failures, 7))
            if not success and 'robots.txt check returned' in error.lower():
                # No content requests until the next successful robots check.
                # Revisit a temporarily unavailable rules document with a bounded cooldown.
                delay=min(interval,max(1800,300*2**min(failures,7)))
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

    def bookmark(self, job_id, saved):
        with self.connection() as db:
            if saved:
                db.execute('INSERT OR IGNORE INTO bookmarks VALUES (?,?)', (job_id, time.time()))
            else:
                db.execute('DELETE FROM bookmarks WHERE job_id=?', (job_id,))
