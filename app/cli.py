"""Examples: python -m app.cli sources | crawl demo | crawl demo --max-items 2"""
import argparse
import asyncio
import logging
import os
from .crawlers.registry import CRAWLERS
from .crawlers.catalog import SOURCES
from .runner import run_crawler
from .access_policy import access_status, ACCESS_RULES, require_access


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="Recruit Radar crawler management")
    subs = parser.add_subparsers(dest="cmd", required=True)
    subs.add_parser("sources", help="Show sources and current collection permission status")
    subs.add_parser("access", help="Audit collection access settings (no network)")
    boards_command = subs.add_parser('boards', help='List curated candidate boards with provenance (offline)')
    boards_command.add_argument('--catalog',default='',help='Custom board catalog JSON')
    subs.add_parser('relabel', help='Rebuild job classification for existing DB rows (offline)')
    reports = subs.add_parser('reports', help='Show recent crawl runs and errors (offline)')
    reports.add_argument('--limit', type=int, default=20)
    doctor = subs.add_parser("doctor", help="Read-only, robots-aware smoke check (explicit --live)")
    doctor.add_argument('site_id', choices=sorted(CRAWLERS))
    doctor.add_argument('--live', action='store_true', help='Make real HTTP requests')
    doctor.add_argument('--samples', type=int, default=1, help='Detail pages to verify (1..10)')
    crawl = subs.add_parser("crawl", help="Collect one source")
    crawl.add_argument("site_id", choices=sorted(CRAWLERS))
    crawl.add_argument("--max-items", type=int, default=None)
    crawl.add_argument("--max-pages", type=int, default=None)
    public = subs.add_parser("crawl-public", help="Collect documented public postings without signup or API keys")
    public.add_argument("--max-items", type=int, default=None, help="Optional sampling cap; disables missing-post closure")
    public.add_argument("--max-pages", type=int, default=None, help="Optional page limit for paginated providers")
    public.add_argument('--catalog', default='', help='Use a custom public_boards.json instead of built-in catalog')
    public.add_argument('--batch-size', type=int, default=0, help='Companies per provider per batch, 0 means all')
    public.add_argument('--batch-index', type=int, default=0, help='Zero-based batch index')
    public.add_argument('--focus', choices=['all','kr_it'], default='all', help='Prefer curated Korean IT boards')
    args = parser.parse_args()
    if args.cmd == 'boards':
        if args.catalog:
            os.environ['PUBLIC_BOARD_CATALOG'] = args.catalog
        from .board_catalog import load_boards
        for row in load_boards():
            print(f"{row['provider']:<16} {row['id']:<24} {row.get('company','')} [{row.get('confidence','candidate')}]")
            print('  ' + row.get('evidence',''))
        return
    if args.cmd == 'relabel':
        from .backfill import relabel_all
        print(f'Relabeled {relabel_all()} existing jobs without recrawling')
        return
    if args.cmd == 'reports':
        from .database import SessionLocal, CrawlRun, init_db
        from sqlalchemy import select
        init_db()
        with SessionLocal() as session:
            for r in session.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(args.limit)):
                print(f'{r.id}: {r.source_id}: {r.status} seen={r.seen_count} created={r.created_count} failed={r.failed_count}')
                if r.error_summary:
                    print('  ' + r.error_summary[:400])
        return
    if args.cmd in {"sources", "access"}:
        for rule in ACCESS_RULES.values():
            permitted, explanation = access_status(rule.site_id)
            print(f"{rule.site_id:<12} {'READY' if permitted else 'BLOCKED':<7} "
                  f"{rule.mode:<26} {explanation}")
            if args.cmd == "sources" and rule.site_id != "demo":
                url = next((s["url"] for s in SOURCES if s["id"] == rule.site_id), "")
                print(f"  {url}")
        return
    if args.cmd == 'crawl-public':
        if args.max_items is not None and args.max_items < 1:
            parser.error('--max-items must be positive')
        if args.max_pages is not None and not 1 <= args.max_pages <= 100:
            parser.error('--max-pages must be 1..100')
        if args.batch_size < 0 or args.batch_index < 0 or (args.batch_index and not args.batch_size):
            parser.error('--batch-size must be >=0; batch-index requires batch-size')
        if args.catalog:
            os.environ['PUBLIC_BOARD_CATALOG'] = args.catalog
        from .crawlers.public_feeds import _boards
        from .board_catalog import default_ids, load_boards
        focus_by_provider = None
        if args.focus == 'kr_it':
            focus_by_provider = {provider: [r['id'] for r in load_boards()
                if r['provider'] == provider and r.get('focus') == 'kr_it' and r.get('enabled',True)]
                for provider in ('greenhouse','lever','ashby','smartrecruiters')}
        provider_specs = (
            ('public_greenhouse','PUBLIC_GREENHOUSE_BOARDS',default_ids('greenhouse')),
            ('public_lever','PUBLIC_LEVER_SITES',default_ids('lever')),
            ('public_ashby','PUBLIC_ASHBY_BOARDS',default_ids('ashby')),
            ('public_smartrecruiters','PUBLIC_SMARTRECRUITERS_COMPANIES',default_ids('smartrecruiters')),
        )
        errors = 0
        for site_id, env_key, default_boards in provider_specs:
            boards = _boards(env_key, default_boards)
            if focus_by_provider is not None:
                focus_ids = set(focus_by_provider[site_id.removeprefix('public_')])
                boards = [board for board in boards if board in focus_ids]
            if args.batch_size:
                start = args.batch_index * args.batch_size
                boards = boards[start:start + args.batch_size]
            if not boards:
                print(f'SKIP {site_id}: no company boards configured (set {env_key})')
                continue
            kwargs = {}
            if args.max_pages is not None and site_id in ('public_lever','public_smartrecruiters'):
                kwargs['max_pages'] = args.max_pages
            crawler = CRAWLERS[site_id](**kwargs)
            crawler.boards = boards
            try:
                result = asyncio.run(run_crawler(crawler, max_items=args.max_items))
                print(f'{site_id}: {result.status} seen={result.seen_count} new={result.created_count} '
                      f'changed={result.updated_count} errors={result.failed_count}')
                if result.status != 'completed':
                    errors += 1
                    if result.error_summary:
                        print(f'  {result.error_summary}')
            except Exception as exc:
                errors += 1
                print(f'{site_id}: ERROR {type(exc).__name__}: {exc}')
        if errors:
            raise SystemExit(1)
        return
    if args.cmd == 'doctor':
        if not 1 <= args.samples <= 10:
            parser.error('--samples must be 1..10')
        if not args.live:
            print('Network check disabled. Add --live after reviewing site terms and access permission.')
            return
        from .database import init_db, SessionLocal
        from .quotas import ApiQuota
        from .fetcher import PoliteFetcher
        init_db()
        async def probe():
            crawler = CRAWLERS[args.site_id]() if args.site_id == 'demo' else CRAWLERS[args.site_id](max_pages=1)
            crawler.api_quota = ApiQuota(SessionLocal)
            count = 0
            async with PoliteFetcher(crawler.allowed_hosts) as fetcher:
                async for ref in crawler.discover(fetcher):
                    draft = await crawler.parse(fetcher, ref)
                    print(f'PASS {crawler.site_id}: id={ref.external_id} title={draft.title[:60]} '
                          f'org={draft.organization or "?"} body_chars={len(draft.body_text or "")} '
                          f'closed={draft.source_status} deadline={draft.deadline_on or draft.deadline_text or "?"}')
                    count += 1
                    if count >= args.samples:
                        return
                if count == 0:
                    raise ValueError('No recognizable postings in first listing page')
        try:
            require_access(args.site_id)
            asyncio.run(probe())
        except Exception as exc:
            print(f'UNVERIFIED {args.site_id}: {type(exc).__name__}: {exc}')
            raise SystemExit(2)
        return
    if args.max_items is not None and args.max_items < 1:
        parser.error("--max-items must be positive")
    if args.max_pages is not None and not 1 <= args.max_pages <= 100:
        parser.error("--max-pages must be 1..100")
    if args.site_id == "demo":
        if args.max_pages is not None:
            parser.error("--max-pages does not apply to demo")
        crawler = CRAWLERS[args.site_id]()
    else:
        crawler = CRAWLERS[args.site_id](max_pages=args.max_pages)
    logging.basicConfig(level=logging.INFO)
    try:
        result = asyncio.run(run_crawler(crawler, max_items=args.max_items))
    except PermissionError as exc:
        print(f"BLOCKED: {exc}")
        raise SystemExit(2) from None
    print(f"source={result.source_id} status={result.status} seen={result.seen_count} "
          f"created={result.created_count} updated={result.updated_count} failed={result.failed_count}")
    if result.error_summary:
        print(f"error={result.error_summary}")
    if result.status != 'completed' or result.failed_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
