"""Curated Korean employer priorities. Discovery links are NEVER auto-crawled.

Only entries referring to pre-registered documented public ATS boards may be
scheduled by the opt-in scheduler. A URL's existence is not permission to scrape.
"""
from __future__ import annotations
import argparse
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_COMPANY_FILE = Path(__file__).parent / 'assets' / 'korean_careers.json'
STAGES = {1:'국내 서비스 대기업', 2:'플랫폼·고성장 기업', 3:'중견·기술기업', 4:'스타트업·AI', 5:'대기업 그룹'}


def load_companies(path: str | Path | None = None) -> list[dict]:
    data = json.loads(Path(path or DEFAULT_COMPANY_FILE).read_text(encoding='utf-8'))
    items = data.get('companies')
    if not isinstance(items, list) or len(items) > 5000:
        raise ValueError('Expected a company list of up to 5000 rows')
    seen = set()
    from .board_catalog import load_boards, DEFAULT_FILE
    boards = {(b['provider'],b['id']) for b in load_boards(DEFAULT_FILE)}
    result = []
    for row in items:
        if not isinstance(row, dict):
            raise ValueError('Company must be an object')
        ident = row.get('id')
        if not isinstance(ident,str) or not ident or ident in seen or not all(c.isascii() and (c.islower() or c.isdigit() or c == '_') for c in ident):
            raise ValueError(f'Invalid or duplicate company id: {ident!r}')
        seen.add(ident)
        url = urlsplit(row.get('url',''))
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None,443):
            raise ValueError(f'Unsafe or invalid company URL: {ident}')
        if row.get('priority_stage') not in STAGES:
            raise ValueError(f'Invalid stage: {ident}')
        if not row.get('name') or not row.get('canonical_company'):
            raise ValueError(f'Missing company name/group: {ident}')
        provider, board = row.get('ats_provider'), row.get('ats_board')
        if (provider is None) != (board is None):
            raise ValueError(f'Incomplete ATS entry: {ident}')
        if provider is not None and (provider,board) not in boards:
            raise ValueError(f'ATS board not in verified-candidate catalog: {ident} {provider}/{board}')
        if row.get('collection_status') != ('existing_public_ats' if provider else 'link_only_pending_policy_and_adapter'):
            raise ValueError(f'Mismatched collection status: {ident}')
        result.append(row)
    return sorted(result, key=lambda r:(r['priority_stage'],r['id']))


def get_statistics(path: str | Path | None = None) -> dict:
    rows = load_companies(path)
    return {'companies':len(rows), 'stage_counts':{STAGES[i]:sum(r['priority_stage']==i for r in rows) for i in STAGES},
            'existing_ats_references':sum('ats_provider' in r for r in rows),
            'link_only':sum('ats_provider' not in r for r in rows),
            'note':'A confirmed careers URL is not a successful live crawler.'}


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Offline curated Korean career links')
    parser.add_argument('command',choices=['stats','list','json'])
    parser.add_argument('--stage',type=int, choices=sorted(STAGES))
    args = parser.parse_args()
    if args.command=='stats':
        print(json.dumps(get_statistics(),ensure_ascii=False,indent=2))
    else:
        rows = [r for r in load_companies() if args.stage is None or r['priority_stage']==args.stage]
        if args.command == 'json':
            print(json.dumps(rows,ensure_ascii=False,indent=2))
        else:
            for r in rows:
                print(f"[{r['priority_stage']}] {r['name']} | {r['url']} | {r['collection_status']}")

if __name__=='__main__':
    main()
