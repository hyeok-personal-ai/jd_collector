"""Curated, editable public ATS board registry (no automatic web-wide discovery)."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from functools import lru_cache

DEFAULT_FILE = Path(__file__).resolve().parent / 'assets' / 'public_boards.json'
TOKEN = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')
PROVIDERS = {'greenhouse','lever','ashby','smartrecruiters'}


def load_boards(path: str | Path | None = None) -> list[dict]:
    path = Path(path or os.getenv('PUBLIC_BOARD_CATALOG', str(DEFAULT_FILE)))
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('boards'), list):
        raise ValueError('Board catalog must be an object with boards array')
    results = []
    seen = set()
    for item in payload['boards']:
        if not isinstance(item, dict):
            raise ValueError('Invalid catalog entry')
        provider, name = item.get('provider'), item.get('id')
        if provider not in PROVIDERS or not isinstance(name, str) or not TOKEN.fullmatch(name):
            raise ValueError(f'Invalid provider/board identifier: {provider!r}/{name!r}')
        if (provider, name) in seen:
            raise ValueError(f'Duplicate board: {provider}/{name}')
        seen.add((provider, name))
        results.append(item)
    if len(results) > 20000:
        raise ValueError('Maximum catalog size is 20000 boards; split into separate catalogs')
    return results


def default_ids(provider: str) -> str:
    return ','.join(row['id'] for row in load_boards() if row['provider'] == provider and row.get('enabled', True))


@lru_cache(maxsize=16)
def _company_map(path: str, mtime_ns: int) -> dict[tuple[str,str],str]:
    """Fast organization lookup without reparsing the catalog for every job."""
    return {(r['provider'], r['id']): str(r.get('company') or r['id'])
            for r in load_boards(path)}


def company_name(provider: str, board: str) -> str:
    path = Path(os.getenv('PUBLIC_BOARD_CATALOG', str(DEFAULT_FILE)))
    return _company_map(str(path), path.stat().st_mtime_ns).get((provider, board), board)
