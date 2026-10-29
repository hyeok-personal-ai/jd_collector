"""Curate public ATS board URLs without crawling the web or guessing board ids.

Input: a newline-delimited list of provider job URLs, or a CSV with columns
`url,company`. Newly imported rows are *disabled* until live verification.
Only exact, trusted ATS domains are accepted (no arbitrary URL fetch).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .board_catalog import load_boards, TOKEN

HOST_PROVIDER = {
    'job-boards.greenhouse.io': 'greenhouse',
    'boards.greenhouse.io': 'greenhouse',
    'jobs.lever.co': 'lever',
    'jobs.eu.lever.co': 'lever',
    'jobs.ashbyhq.com': 'ashby',
    'jobs.smartrecruiters.com': 'smartrecruiters',
}


def parse_board_url(raw_url: str) -> tuple[str, str]:
    """Extract company board slug from a supported public careers URL."""
    url = urlsplit(raw_url.strip())
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise ValueError('Only HTTPS public job URLs without credentials or alternate ports are allowed')
    provider = HOST_PROVIDER.get(url.hostname or '')
    if provider is None:
        raise ValueError('URL is not from a supported public ATS host')
    parts = [x for x in url.path.split('/') if x]
    if not parts or not TOKEN.fullmatch(parts[0]) or parts[0].lower() in ('api', 'embed', 'v1', 'job_board'):
        raise ValueError('ATS URL has no valid company identifier')
    return provider, parts[0]


def read_input(path: Path) -> list[dict]:
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        if path.suffix.lower() == '.csv':
            reader = csv.DictReader(f)
            if not reader.fieldnames or 'url' not in reader.fieldnames:
                raise ValueError('CSV input must contain a `url` column')
            return [{'url': (row.get('url') or '').strip(), 'company': (row.get('company') or '').strip()}
                    for row in reader if (row.get('url') or '').strip()]
        return [{'url': line.strip(), 'company': ''} for line in f
                if line.strip() and not line.lstrip().startswith('#')]


def safe_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.boards-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
            out.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def import_urls(catalog: Path, urls_file: Path, *, enable: bool = False, dry_run: bool = False) -> dict:
    data = json.loads(catalog.read_text(encoding='utf-8'))
    boards = load_boards(catalog)
    keys = {(r['provider'], r['id'].lower()) for r in boards}
    inserted = 0
    skipped = 0
    for row in read_input(urls_file):
        provider, identifier = parse_board_url(row['url'])
        key = (provider, identifier.lower())
        if key in keys:
            skipped += 1
            continue
        boards.append({'provider': provider, 'id': identifier,
                       'company': row['company'] or identifier,
                       'evidence': row['url'], 'confidence': 'url_imported_unverified',
                       'enabled': enable})
        keys.add(key)
        inserted += 1
    if len(boards) > 20000:
        raise ValueError('Catalog too large')
    data['boards'] = boards
    data['version'] = max(int(data.get('version', 1)), 2)
    if not dry_run:
        safe_write(catalog, data)
    return {'added': inserted, 'skipped_existing': skipped, 'total': len(boards),
            'enabled_new': enable, 'dry_run': dry_run}


def apply_report(catalog: Path, report_file: Path, *, dry_run: bool = False) -> dict:
    """Enable verified sampled boards; never disable boards based on one failure."""
    data = json.loads(catalog.read_text(encoding='utf-8'))
    report = json.loads(report_file.read_text(encoding='utf-8'))
    if not isinstance(report, list):
        raise ValueError('Verification report must be a list')
    statuses = {(r['provider'], r['board']): r['status'] for r in report}
    enabled = 0
    for b in data['boards']:
        status = statuses.get((b['provider'], b['id']))
        if status is None:
            continue
        b['last_probe_status'] = status
        if status == 'sample_ok' and not b.get('enabled', True):
            b['enabled'] = True
            enabled += 1
    if not dry_run:
        safe_write(catalog, data)
    return {'enabled': enabled, 'reported': len(report), 'dry_run': dry_run}


def main() -> None:
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Manage documented public ATS company board URLs')
    sub = parser.add_subparsers(dest='cmd', required=True)
    imp = sub.add_parser('import-urls', help='Import TXT/CSV ATS job URLs into catalog')
    imp.add_argument('--input', required=True)
    imp.add_argument('--catalog', default='sources/public_boards.json')
    imp.add_argument('--enable', action='store_true', help='Enable unverified imported boards (not recommended)')
    imp.add_argument('--dry-run', action='store_true')
    applied = sub.add_parser('apply-report', help='Enable boards whose sample extraction passed')
    applied.add_argument('--input', required=True, help='Output of app.verify_boards --live')
    applied.add_argument('--catalog', default='sources/public_boards.json')
    applied.add_argument('--dry-run', action='store_true')
    stats = sub.add_parser('stats', help='Count curated boards per provider')
    stats.add_argument('--catalog', default='sources/public_boards.json')
    args = parser.parse_args()
    catalog = Path(args.catalog)
    if args.cmd == 'import-urls':
        print(json.dumps(import_urls(catalog, Path(args.input),enable=args.enable,dry_run=args.dry_run),ensure_ascii=False))
    elif args.cmd == 'apply-report':
        print(json.dumps(apply_report(catalog,Path(args.input),dry_run=args.dry_run),ensure_ascii=False))
    else:
        rows = load_boards(catalog)
        print(json.dumps({'total': len(rows), 'enabled': sum(x.get('enabled',True) for x in rows),
            'providers': dict(Counter(x['provider'] for x in rows))}, ensure_ascii=False))

if __name__ == '__main__':
    main()
