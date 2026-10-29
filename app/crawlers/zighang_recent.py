"""Bounded discovery outside company first pages, through official recent sitemaps."""
import re
import time
from datetime import timedelta

from ..deadline_policy import today_kst
from ..development_focus import PUBLIC_JOB
from .zighang_public import COMPANY_SOURCE, public_rows, company_candidates, company_urls

RECENT_SOURCE = 'zighang_recent'
INDEX = 'https://zighang.com/seo/sitemap/sitemap-index.xml'


def pending_recent(store, limit=10):
    cutoff = (today_kst() - timedelta(days=7)).isoformat()
    with store.db() as db:
        return [dict(r) for r in db.execute("""SELECT * FROM aggregate_refs
            WHERE source=? AND revision>=? AND (state='pending' OR
            (state='failed' AND checked<?)) ORDER BY revision DESC,rowid LIMIT ?""",
            (RECENT_SOURCE, cutoff, time.time()-86400, limit))]


async def discover_recent(fetcher, store, limit=10):
    from .aggregators import sitemap_urls
    if time.time() - (store.meta(RECENT_SOURCE, 'last_scan') or 0) >= 21600:
        index = sitemap_urls(await fetcher.get_text(INDEX))
        cutoff = (today_kst() - timedelta(days=7)).isoformat()
        pages = sorted((r for r in index if re.fullmatch(
            r'https://zighang.com/seo/sitemap/sitemap-recruitment-\d+\.xml', r[0]) and r[1]>=cutoff),
            key=lambda r: (r[1], -int(re.search(r'-(\d+)\.xml', r[0])[1])), reverse=True)[:3]
        if not pages:
            raise ValueError('No recent official recruitment sitemap')
        store.pages(RECENT_SOURCE, [u for u, _ in pages])
        for url, _ in pages:
            rows = []
            for target, revision in sitemap_urls(await fetcher.get_text(url)):
                match = PUBLIC_JOB.fullmatch(target)
                if match and revision[:10]>=cutoff:
                    rows.append({'id':match[1].lower(), 'url':target, 'revision':revision[:10]})
            store.save_page(RECENT_SOURCE, url, rows)
        store.meta(RECENT_SOURCE, 'last_scan', time.time())
    for row in pending_recent(store, limit):
        try:
            rows = public_rows(await fetcher.get_text(row['url']))
            if row['id'] not in rows:
                raise ValueError('Public detail has no recognized target posting')
            # Non-development text is inspected transiently, never persisted here.
            candidates = company_candidates(rows, row['url'], store.checked_refs('zighang', rows))
            store.add_focus('zighang', candidates, provenance='official_recent_sitemap')
            store.pages(COMPANY_SOURCE, company_urls(rows))
            store.mark(RECENT_SOURCE, row['id'], 'done', active=False)
        except PermissionError:
            store.mark(RECENT_SOURCE, row['id'], 'failed', 'Access policy; no bypass')
            raise
        except Exception as exc:
            store.mark(RECENT_SOURCE, row['id'], 'unavailable' if any(
                code in str(exc) for code in ('HTTP 404','HTTP 410')) else 'failed', str(exc))
            if any(code in str(exc) for code in ('HTTP 401','HTTP 403','HTTP 429')):
                raise


def recent_summary(store):
    with store.db() as db:
        counts = {r[0]:r[1] for r in db.execute('SELECT state,count(*) FROM aggregate_refs WHERE source=? GROUP BY state', (RECENT_SOURCE,))}
    return {'inventory':counts, 'eligible_pending':len(pending_recent(store, 100000)),
            'last_scan':store.meta(RECENT_SOURCE, 'last_scan'), 'window_days':7,
            'max_sitemap_pages':3, 'remaining_development':None}
