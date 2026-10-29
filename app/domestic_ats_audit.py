"""Offline inspection commands for explicitly saved public-HTML snapshots.

Never makes network requests. Use the collection system only after checking
site policies and obtaining any required permission.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from .crawlers.domestic_ats import diagnose_listing, load_domestic_config

CANDIDATES = Path(__file__).resolve().parent.parent / 'sources/domestic_ats_candidates.json'


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Domestic ATS offline audit (no HTTP requests)')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('candidates', help='List candidate company boards, no authorization assumed')
    inspect = subs.add_parser('inspect', help='Inspect an operator-supplied saved HTML file')
    inspect.add_argument('--file', required=True)
    inspect.add_argument('--family', choices=['greeting','ninehire'], required=True)
    inspect.add_argument('--url', required=True)
    inspect.add_argument('--company', required=True)
    subs.add_parser('configured', help='Validate opt-in configuration without fetching')
    export = subs.add_parser('export-catalog', help='Export 145-catalog Greeting/Ninehire candidate boards, disabled by default')
    export.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'candidates':
        print(json.dumps(json.loads(CANDIDATES.read_text(encoding='utf-8')),ensure_ascii=False,indent=2))
    elif args.command == 'export-catalog':
        from .company_catalog import load_companies
        from .site_family import identify
        rows=[]
        for company in load_companies():
            family=identify(company['url']).family
            if family not in ('greeting','ninehire'):
                continue
            rows.append({'company':company.get('name') or company.get('company') or company['id'],
                         'family':family,'url':company['url'],'link_selector':'a[href]',
                         'authorized':False,'status':'candidate_url_may_not_be_job_listing'})
        result={'_comment':'Disabled candidate export. Review access terms, correct listing pages and test selectors before opting in.',
                'boards':rows}
        target=Path(args.out)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'saved':str(target),'candidates':len(rows), 'authorized':0},ensure_ascii=False))
    elif args.command == 'configured':
        boards = load_domestic_config()
        print(json.dumps({'authorized_boards':len(boards), 'domains':[urlsplit(r['url']).hostname for r in boards]},ensure_ascii=False,indent=2))
    else:
        board={'family':args.family, 'company':args.company, 'url':args.url}
        html=Path(args.file).read_text(encoding='utf-8')
        print(json.dumps(diagnose_listing(html,board),ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
