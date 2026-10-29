"""v2.1 sample verification CLI. Default commands issue NO network requests.

Examples:
  python -m app.verification_cli catalog
  python -m app.verification_cli offline --manifest fixtures/v21_snapshots.json --save
  python -m app.verification_cli public --live --max-boards 3 --samples 2 --save
  python -m app.verification_cli domestic --live --samples 2 --save

DOMESTIC_ATS_FILE and ALLOW_HTML_DOMESTIC_ATS=1 required for domestic.
Never visits a site without explicit --live and relevant access flags.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .board_catalog import load_boards
from .company_catalog import load_companies
from .crawlers.domestic_ats import (
    _job_parts, diagnose_listing, extract_domestic_links, load_domestic_config, parse_domestic_detail)
from .crawlers.public_feeds import (
    GreenhousePublicCrawler, LeverPublicCrawler, AshbyPublicCrawler, SmartRecruitersPublicCrawler)
from .database import SessionLocal, init_db
from .fetcher import PoliteFetcher
from .site_family import identify, catalog_report
from .verification import inspect_draft, make_report, report_summary
from .verification_store import store_report
from .console import print_json_payload

PUBLIC_URLS = {
    'greenhouse': lambda b: f'https://boards-api.greenhouse.io/v1/boards/{b}/jobs',
    'lever': lambda b: f'https://api.lever.co/v0/postings/{b}?mode=json',
    'ashby': lambda b: f'https://api.ashbyhq.com/posting-api/job-board/{b}',
    'smartrecruiters': lambda b: f'https://api.smartrecruiters.com/v1/companies/{b}/postings',
}

PROVIDERS = {'greenhouse': GreenhousePublicCrawler, 'lever': LeverPublicCrawler,
             'ashby': AshbyPublicCrawler, 'smartrecruiters': SmartRecruitersPublicCrawler}


def _local_file(base: Path, relative: str) -> Path:
    """Manifest files must be inside the manifest folder; block arbitrary traversal."""
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file() or path.stat().st_size > 5_000_000:
        raise ValueError('Unsafe, missing or oversized snapshot file')
    return path


def offline_snapshots(manifest: str | Path, *, sample_limit: int = 5) -> list[dict]:
    path = Path(manifest).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    rows = data.get('boards', [])
    if not isinstance(rows, list) or len(rows) > 1000 or not 1 <= sample_limit <= 10:
        raise ValueError('Invalid snapshot manifest')
    reports = []
    for row in rows:
        board = {k: row[k] for k in ('family','company','url')}
        if board['family'] not in ('greeting','ninehire'):
            raise ValueError('Offline snapshot requires greeting or ninehire')
        if identify(board['url']).family != board['family']:
            raise ValueError('Snapshot family does not match URL hostname')
        source = 'domestic:' + urlsplit(board['url']).hostname + ':' + hashlib.sha256(board['url'].encode()).hexdigest()[:12]
        listing = _local_file(path.parent,row['list_file']).read_text(encoding='utf-8')
        refs = extract_domestic_links(listing, board)
        details = {item['url']:item for item in row.get('details', [])}
        checks, errors, attempted = [], [], 0
        for jid, url, title in refs:
            if attempted >= sample_limit:
                break
            if url not in details:
                continue  # No corresponding reference capture; not a parse failure.
            attempted += 1
            try:
                item = details[url]
                snapshot = _local_file(path.parent,item['file']).read_text(encoding='utf-8')
                draft = parse_domestic_detail(snapshot,url,board,listing_title=title)
                if _job_parts(url,board['url'],board['family'])[0] != jid:
                    raise ValueError('Unstable posting ID')
                checks.append(inspect_draft(draft,item.get('reference')))
            except Exception as exc:
                errors.append(f'{jid}: {type(exc).__name__}: {str(exc)[:160]}')
        outcome = make_report(source_key=source,url=board['url'],family=board['family'],
            mode='offline_fixture',discovered=len(refs),attempted=attempted,
            checks=checks,errors=errors,extraction_method='local_html_snapshot',
            meta={'fixture_origin':row.get('fixture_origin','synthetic'),
                  'listing_diagnosis':diagnose_listing(listing,board)})
        reports.append(outcome)
    return reports


async def live_public(*, provider: str | None = None,
                      max_boards: int = 3, samples: int = 2) -> list[dict]:
    candidates = [x for x in load_boards() if x.get('enabled',True)
                  and (provider is None or x['provider'] == provider)][:max_boards]
    reports = []
    for board in candidates:
        crawler = PROVIDERS[board['provider']](max_pages=1)
        crawler.boards = [board['id']]
        source_key = f"{board['provider']}:{board['id']}"
        checks, errors, discovered, attempted = [], [], 0, 0
        reached = False
        try:
            async with PoliteFetcher(crawler.allowed_hosts) as fetcher:
                async for ref in crawler.discover(fetcher):
                    reached = True
                    discovered += 1
                    if attempted >= samples:
                        # We intentionally finish list iteration for the true discover count.
                        continue
                    attempted += 1
                    try:
                        draft = await crawler.parse(fetcher, ref)
                        if not draft.title or not draft.original_url:
                            raise ValueError('No title or original URL')
                        checks.append(inspect_draft(draft))
                    except Exception as exc:
                        errors.append(f'{ref.external_id}: {type(exc).__name__}: {str(exc)[:160]}')
                errors.extend(getattr(crawler,'scope_errors',[]))
                # Empty valid list: HTTP might have succeeded; without fetcher
                # instrumentation don't assert site reachability.
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {str(exc)[:160]}')
        reports.append(make_report(source_key=source_key,
            url=PUBLIC_URLS[board['provider']](board['id']),
            family=board['provider'],mode='live_public',discovered=discovered,
            attempted=attempted,checks=checks,errors=errors,
            reachable=True if reached else None,
            extraction_method='documented_public_json',
            meta={'company':board.get('company'),'board_id':board['id'],
                  'reachability_note':'true means postings were returned, not access/reuse permission'}))
    return reports


async def live_domestic(*, samples: int = 2, max_boards: int = 3,
                        render_js: bool = False) -> list[dict]:
    from .access_policy import require_access
    require_access('domestic_ats')
    config = load_domestic_config()
    reports = []
    for board in config[:max_boards]:
        host = urlsplit(board['url']).hostname
        checks, errors, attempted, count = [], [], 0, 0
        reached = False
        family = board['family']
        marker = 'domestic:' + host + ':' + hashlib.sha256(board['url'].encode()).hexdigest()[:12]
        try:
            async with PoliteFetcher(frozenset({host})) as fetcher:
                html = await fetcher.get_text(board['url'])
                reached = True
                links = extract_domestic_links(html,board)
                if not links and render_js:
                    html = await fetcher.render_html(board['url'])
                    links = extract_domestic_links(html,board)
                count = len(links)
                if not links:
                    errors.append('No visible matching job links; requires layout or rendering review')
                for jid, url, title in links[:samples]:
                    attempted += 1
                    try:
                        draft = parse_domestic_detail(await fetcher.get_text(url),url,board,listing_title=title)
                        checks.append(inspect_draft(draft))
                    except Exception as exc:
                        errors.append(f'{jid}: {type(exc).__name__}: {str(exc)[:160]}')
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {str(exc)[:160]}')
        reports.append(make_report(source_key=marker,url=board['url'],family=family,
            mode='live_domestic',discovered=count,attempted=attempted,checks=checks,
            errors=errors,reachable=reached,extraction_method='visible_html_links',
            meta={'company':board['company'],'render_js_enabled':render_js}))
    return reports


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Independent v2.1 QA; reports never change job lifecycle')
    sub = parser.add_subparsers(dest='command', required=True)
    cat = sub.add_parser('catalog',help='Offline 145-company URL audit, NO site requests')
    off = sub.add_parser('offline',help='Analyze local saved HTML; does not contact sites')
    off.add_argument('--manifest',required=True)
    pub = sub.add_parser('public',help='Documented no-key ATS endpoints, gated by --live')
    pub.add_argument('--provider',choices=sorted(PROVIDERS))
    dom = sub.add_parser('domestic',help='Operator-authorized domestic ATS HTML, gated by --live')
    dom.add_argument('--render-js',action='store_true',help='Only on authorized/robots-permitted page')
    for subparser in (off,pub,dom):
        subparser.add_argument('--samples',type=int,default=2)
        subparser.add_argument('--save',action='store_true',help='Save metadata/QA report to local database')
    for subparser in (pub,dom):
        subparser.add_argument('--live',action='store_true',help='Explicitly permit actual site requests')
        subparser.add_argument('--max-boards',type=int,default=3)
    for subparser in (cat,off,pub,dom):
        subparser.add_argument('--output',default='',help='Local JSON report file')
    args = parser.parse_args()
    if args.command in ('public','domestic') and not args.live:
        parser.error('Live site requests require explicit --live (no implicit network)')
    if args.command != 'catalog' and not 1 <= args.samples <= 10:
        parser.error('--samples must be between 1 and 10')
    if args.command in ('public','domestic') and not 1 <= args.max_boards <= 100:
        parser.error('--max-boards must be between 1 and 100')
    if args.command == 'catalog':
        result = catalog_report(load_companies())
        result['mode']='catalog_only'
        result['targets']=[{'id':row['id'],'name':row['name'],
                           'priority_stage':row['priority_stage'],
                           'url':row['url'],
                           'family':identify(row['url']).family,
                           'status':'not_checked',
                           'collection_status':row['collection_status']}
                          for row in load_companies()]
        result['note'] += ' 0 live sites verified by this operation.'
    else:
        if args.command == 'offline':
            reports=offline_snapshots(args.manifest,sample_limit=args.samples)
        elif args.command == 'public':
            reports=asyncio.run(live_public(provider=args.provider,
                max_boards=args.max_boards,samples=args.samples))
        else:
            reports=asyncio.run(live_domestic(max_boards=args.max_boards,
                samples=args.samples,render_js=args.render_js))
        if args.save:
            init_db()
            with SessionLocal() as db:
                for report in reports:
                    store_report(db,report)
                db.commit()
        result={'summary':report_summary(reports),'reports':reports}
    output = json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).write_text(output+'\n',encoding='utf-8')
    print_json_payload(output)

if __name__ == '__main__':
    main()
