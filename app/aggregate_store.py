"""Resumable public-source inventory, separate from searchable full postings."""
import json
import sqlite3
import time
from contextlib import contextmanager
from .control import data_dir
from .deadline_policy import today_kst


class AggregateStore:
    def __init__(self, path=None):
        self.path = path or data_dir() / 'inventory.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS aggregate_pages(source TEXT,url TEXT,state TEXT DEFAULT 'pending',
                count INTEGER DEFAULT 0,error TEXT,checked REAL,PRIMARY KEY(source,url));
            CREATE TABLE IF NOT EXISTS aggregate_refs(source TEXT,id TEXT,url TEXT,revision TEXT,
                payload TEXT,state TEXT DEFAULT 'pending',error TEXT,checked REAL,
                PRIMARY KEY(source,id));
            CREATE INDEX IF NOT EXISTS aggregate_pending ON aggregate_refs(source,state,checked);
            CREATE TABLE IF NOT EXISTS aggregate_meta(source TEXT,key TEXT,value TEXT,PRIMARY KEY(source,key));
            CREATE TABLE IF NOT EXISTS aggregate_focus(source TEXT,id TEXT,focus TEXT,title TEXT,provenance TEXT,
                PRIMARY KEY(source,id,focus));
            ''')
            if 'seen' not in {r[1] for r in db.execute('PRAGMA table_info(aggregate_refs)')}:
                db.execute('ALTER TABLE aggregate_refs ADD COLUMN seen REAL DEFAULT 0')
            if 'active' not in {r[1] for r in db.execute('PRAGMA table_info(aggregate_refs)')}:
                db.execute('ALTER TABLE aggregate_refs ADD COLUMN active INTEGER DEFAULT 1')
            columns={r[1] for r in db.execute('PRAGMA table_info(aggregate_refs)')}
            if 'recheck_mode' not in columns:
                db.execute("ALTER TABLE aggregate_refs ADD COLUMN recheck_mode TEXT NOT NULL DEFAULT 'unknown'")
            if 'deadline_on' not in columns:
                db.execute('ALTER TABLE aggregate_refs ADD COLUMN deadline_on TEXT')

    @contextmanager
    def db(self):
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

    def meta(self, source, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT INTO aggregate_meta VALUES(?,?,?) ON CONFLICT(source,key) DO UPDATE SET value=excluded.value',
                           (source,key,json.dumps(value,ensure_ascii=False)))
                return value
            row = db.execute('SELECT value FROM aggregate_meta WHERE source=? AND key=?',(source,key)).fetchone()
            return json.loads(row[0]) if row else None

    def pages(self, source, urls):
        with self.db() as db:
            db.executemany('INSERT OR IGNORE INTO aggregate_pages(source,url) VALUES(?,?)',[(source,u) for u in urls])

    def active_pages(self,source,urls):
        self.pages(source,urls)
        if not urls:return
        placeholders=','.join('?' for _ in urls)
        with self.db() as db:
            db.execute(f"UPDATE aggregate_pages SET state='retired' WHERE source=? AND url NOT IN ({placeholders})",(source,*urls))
            db.execute(f"UPDATE aggregate_pages SET state='pending',checked=NULL WHERE source=? AND state='retired' AND url IN ({placeholders})",(source,*urls))

    def pending_pages(self, source, limit=10):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT url FROM aggregate_pages WHERE source=? AND state='pending' ORDER BY rowid LIMIT ?",(source,limit))]

    def stale_pages(self,source,limit=5):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT url FROM aggregate_pages WHERE source=? AND state='done' AND checked<? ORDER BY rowid LIMIT ?",(source,time.time()-21600,limit))]

    def due_discovery_pages(self,source,limit=5):
        with self.db() as db:
            return [r[0] for r in db.execute("""SELECT url FROM aggregate_pages WHERE source=?
                AND (state='pending' OR (state IN ('done','failed') AND checked<?))
                ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END,checked,rowid LIMIT ?""",
                (source,time.time()-21600,limit))]

    def mark_discovery_page(self,source,url,*,count=0,error='',state='done'):
        with self.db() as db:
            db.execute('UPDATE aggregate_pages SET state=?,count=?,error=?,checked=? WHERE source=? AND url=?',
                       (state,count,error[:1000],time.time(),source,url))

    def discovery_summary(self,source):
        with self.db() as db:
            return {r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM aggregate_pages WHERE source=? GROUP BY state',(source,))}

    def checked_refs(self,source,identifiers):
        identifiers=list(identifiers)
        if not identifiers:return set()
        placeholders=','.join('?' for _ in identifiers)
        with self.db() as db:
            return {r[0] for r in db.execute(f'SELECT id FROM aggregate_refs WHERE source=? AND id IN ({placeholders}) AND checked IS NOT NULL',
                                           (source,*identifiers))}

    def save_page(self, source, url, rows):
        with self.db() as db:
            for row in rows:
                db.execute('''INSERT INTO aggregate_refs(source,id,url,revision,payload,seen) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(source,id) DO UPDATE SET url=excluded.url,revision=excluded.revision,
                    seen=excluded.seen,
                    payload=coalesce(excluded.payload,aggregate_refs.payload),
                    state=CASE WHEN aggregate_refs.revision IS NOT excluded.revision OR aggregate_refs.url IS NOT excluded.url THEN 'pending' ELSE aggregate_refs.state END''',
                    (source,row['id'],row['url'],row.get('revision'),json.dumps(row['payload'],ensure_ascii=False) if row.get('payload') else None,time.time()))
            db.execute("UPDATE aggregate_pages SET state='done',count=?,checked=?,error=NULL WHERE source=? AND url=?",(len(rows),time.time(),source,url))

    def due(self, source, limit=1000, *, focus=None):
        with self.db() as db:
            db.execute("UPDATE aggregate_refs SET active=0,recheck_mode='closed' WHERE source=? AND state='done' AND active=1 AND deadline_on<?",(source,today_kst().isoformat()))
            # Known postings are rechecked before new backlog. Failure retries have a cooldown.
            if focus:
                # Keep the small selected set as the outer loop, then use the
                # (source,id) primary key. Scanning the historical backlog here
                # stalls the async collection worker even when no detail is due.
                rows=db.execute('''SELECT r.* FROM aggregate_focus f CROSS JOIN aggregate_refs r
                    WHERE f.source=? AND f.focus=? AND r.source=f.source AND r.id=f.id AND
                    (r.state='pending' OR (r.state='done' AND r.active=1 AND r.recheck_mode NOT IN ('fixed','closed') AND r.checked<?)
                     OR (r.state IN ('failed','unavailable') AND r.recheck_mode NOT IN ('fixed','closed') AND r.checked<?))
                    ORDER BY CASE WHEN r.state='done' THEN 0 ELSE 1 END,r.rowid LIMIT ?''',
                    (source,focus,time.time()-21600,time.time()-86400,limit)).fetchall()
            else:
                rows=db.execute('''SELECT * FROM aggregate_refs WHERE source=? AND
                    (state='pending' OR (state='done' AND active=1 AND recheck_mode NOT IN ('fixed','closed') AND checked<?)
                     OR (state IN ('failed','unavailable') AND recheck_mode NOT IN ('fixed','closed') AND checked<?))
                    ORDER BY CASE WHEN state='done' THEN 0 ELSE 1 END,rowid LIMIT ?''',
                    (source,time.time()-21600,time.time()-86400,limit)).fetchall()
            return [{**dict(r),'payload':json.loads(r['payload']) if r['payload'] else None} for r in rows]

    def add_focus(self, source, rows, *, focus='development', provenance='public_search'):
        """Add selected public candidates without resetting saved status or source revisions."""
        with self.db() as db:
            for row in rows:
                if row.get('payload') and row.get('revision'):
                    db.execute('''INSERT INTO aggregate_refs(source,id,url,seen,payload,revision) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,
                        seen=excluded.seen,state=CASE WHEN aggregate_refs.revision IS NOT excluded.revision
                        THEN 'pending' ELSE aggregate_refs.state END''',
                        (source,row['id'],row['url'],time.time(),json.dumps(row['payload'],ensure_ascii=False),row['revision']))
                else:
                    db.execute('INSERT OR IGNORE INTO aggregate_refs(source,id,url,seen) VALUES(?,?,?,?)',
                               (source,row['id'],row['url'],time.time()))
                db.execute('''INSERT INTO aggregate_focus VALUES(?,?,?,?,?) ON CONFLICT(source,id,focus)
                    DO UPDATE SET title=excluded.title,provenance=excluded.provenance''',
                    (source,row['id'],focus,row.get('title','')[:500],provenance))

    def focus_summary(self, source, focus='development'):
        with self.db() as db:
            return {r[0]:r[1] for r in db.execute('''SELECT r.state,count(*) FROM aggregate_refs r
                JOIN aggregate_focus f ON f.source=r.source AND f.id=r.id
                WHERE r.source=? AND f.focus=? GROUP BY r.state''',(source,focus))}

    def mark(self, source, identifier, state, error='',active=None,recheck_mode=None,deadline_on=None):
        with self.db() as db:
            db.execute('UPDATE aggregate_refs SET state=?,error=?,checked=? WHERE source=? AND id=?',
                       (state,error[:1000],time.time(),source,identifier))
            if active is not None:db.execute('UPDATE aggregate_refs SET active=? WHERE source=? AND id=?',(int(active),source,identifier))
            if recheck_mode is not None:
                db.execute('UPDATE aggregate_refs SET recheck_mode=?,deadline_on=? WHERE source=? AND id=?',
                           (recheck_mode,deadline_on,source,identifier))

    def reset_pages(self, source):
        with self.db() as db:
            db.execute("UPDATE aggregate_pages SET state='pending' WHERE source=?",(source,))

    def absent(self,source,since):
        with self.db() as db:
            return [{**dict(r),'payload':json.loads(r['payload']) if r['payload'] else None} for r in
                    db.execute("SELECT * FROM aggregate_refs WHERE source=? AND seen<? AND state='done' AND recheck_mode NOT IN ('fixed','closed')",(source,since))]

    def scan_count(self,source,since):
        with self.db() as db:return db.execute('SELECT count(*) FROM aggregate_refs WHERE source=? AND seen>=?',(source,since)).fetchone()[0]

    def summary(self, source):
        with self.db() as db:
            refs={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM aggregate_refs WHERE source=? GROUP BY state',(source,))}
            pages={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM aggregate_pages WHERE source=? GROUP BY state',(source,))}
            review={r[0]:r[1] for r in db.execute("SELECT recheck_mode,count(*) FROM aggregate_refs WHERE source=? AND state='done' GROUP BY recheck_mode",(source,))}
        expected=self.meta(source,'expected')
        scanned=self.meta(source,'scanned_unique_count')
        started=self.meta(source,'scan_started')
        current={}
        if source=='inthiswork' and started:
            with self.db() as db:
                current={r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM aggregate_refs WHERE source=? AND seen>=? GROUP BY state',(source,started))}
        count_matches=expected is None or (scanned if scanned is not None else sum(refs.values()))==expected
        return {'source':source,'inventory':refs,'pages':pages,'expected':expected,'count_matches':count_matches,
                'review_policy':review,'current_inventory':current,
                'current_list_complete':bool(current and count_matches and sum(current.values())==expected and not pages.get('pending') and not any(current.get(state) for state in ('pending','failed','unavailable'))),
                'scope':self.meta(source,'scope'),'last_scan':self.meta(source,'last_scan'),
                'complete':bool(count_matches and pages.get('done') and not pages.get('pending') and not refs.get('pending') and not refs.get('failed'))}

