"""Read-only live check of each configured public ATS board, NEVER an access bypass.

Reports are snapshots. Success means SAMPLE LIST+PARSE only, not license approval,
full pagination, data completeness, or permanent reachability.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import time
from pathlib import Path
from .board_catalog import load_boards
from .crawlers.public_feeds import (
    GreenhousePublicCrawler,LeverPublicCrawler,AshbyPublicCrawler,SmartRecruitersPublicCrawler)
from .fetcher import PoliteFetcher

CLASSES={'greenhouse':GreenhousePublicCrawler,'lever':LeverPublicCrawler,
         'ashby':AshbyPublicCrawler,'smartrecruiters':SmartRecruitersPublicCrawler}


async def verify(*, provider: str | None = None, max_boards: int = 0, samples: int = 2,
                 live: bool = False, only_disabled: bool = False, focus: str = 'all') -> list[dict]:
    boards=[b for b in load_boards() if (provider is None or b['provider']==provider)
            and (not only_disabled or not b.get('enabled',True))
            and (focus == 'all' or b.get('focus') == focus)]
    if max_boards:
        boards=boards[:max_boards]
    report=[]
    if not live:
        return [{**b,'status':'NOT_CHECKED','reason':'add --live to make network requests'} for b in boards]
    for b in boards:
        cls=CLASSES[b['provider']]
        crawler=cls(max_pages=1)
        crawler.boards=[b['id']]
        result={'provider':b['provider'],'board':b['id'],'company':b.get('company'),
                'status':'failed','samples':[], 'confidence':'live_sample_only'}
        start=time.monotonic()
        try:
            async with PoliteFetcher(crawler.allowed_hosts) as fetcher:
                async for ref in crawler.discover(fetcher):
                    draft=await crawler.parse(fetcher,ref)
                    result['samples'].append({'external_id':ref.external_id, 'title':draft.title,
                                              'region':draft.region, 'text_chars':len(draft.body_text or '')})
                    if len(result['samples'])>=samples:
                        break
            if result['samples']:
                result['status']='sample_ok'
            else:
                result['status']='empty_or_failed'
            if getattr(crawler,'scope_errors',[]):
                result['errors']=crawler.scope_errors
                if not result['samples']:
                    result['status']='failed'
        except Exception as exc:
            result['error']=f'{type(exc).__name__}: {str(exc)[:250]}'
        result['elapsed_seconds']=round(time.monotonic()-start,2)
        report.append(result)
    return report


def main():
    from .console import configure_console
    configure_console()
    parser=argparse.ArgumentParser(description='Public ATS listing smoke verification; no writes or credentials')
    parser.add_argument('--live',action='store_true',help='Actually send public GET requests (robots checked)')
    parser.add_argument('--provider',choices=sorted(CLASSES))
    parser.add_argument('--max-boards',type=int,default=0,help='0 means all selected boards')
    parser.add_argument('--samples',type=int,default=2,help='Per-board samples, 1..10')
    parser.add_argument('--output',default='',help='Optional report JSON path')
    parser.add_argument('--catalog',default='',help='Override public ATS board catalog JSON')
    parser.add_argument('--only-disabled',action='store_true',help='Only validate newly imported disabled boards')
    parser.add_argument('--focus',choices=['all','kr_it'],default='all')
    args=parser.parse_args()
    if not 1<=args.samples<=10 or args.max_boards<0:
        parser.error('samples 1..10 and max-boards>=0 required')
    if args.catalog:
        import os
        os.environ['PUBLIC_BOARD_CATALOG'] = args.catalog
    report=asyncio.run(verify(provider=args.provider,max_boards=args.max_boards,
                              samples=args.samples,live=args.live,only_disabled=args.only_disabled,focus=args.focus))
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).write_text(text+'\n',encoding='utf-8')
    print(text)

if __name__=='__main__':
    main()
