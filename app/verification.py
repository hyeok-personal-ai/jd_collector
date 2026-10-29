"""Read-only collection QA: compare extracted fields and annotate uncertainty.

A parsable HTML fixture is NOT proof of live reachability. Presence/coverage is
NOT extraction accuracy; accuracy exists only when human reference values exist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .schema import JobDraft

MANDATORY = ('title', 'organization', 'original_url', 'body_text')
OPTIONAL = ('employment_type', 'region', 'posted_on', 'deadline_on', 'apply_url')
COMPARE = set(MANDATORY + OPTIONAL + ('deadline_text', 'source_status'))


def _nonempty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _datum(value: Any) -> str:
    if value is None:
        return ''
    return ' '.join(str(value).split()).casefold()


def inspect_draft(draft: JobDraft, reference: dict | None = None) -> dict:
    """Report evidence from a single parsed result, never assert source truth."""
    fields = {field: _nonempty(getattr(draft, field)) for field in MANDATORY + OPTIONAL}
    outcome: dict = {
        'external_id': draft.external_id,
        'title': draft.title[:240],
        'url': draft.original_url,
        'fields_present': fields,
        'required_coverage': round(sum(fields[x] for x in MANDATORY) / len(MANDATORY), 4),
        'optional_coverage': round(sum(fields[x] for x in OPTIONAL) / len(OPTIONAL), 4),
        'reference_comparisons': {},
    }
    if reference is not None:
        for name, truth in reference.items():
            if name not in COMPARE:
                raise ValueError(f'Invalid reference field: {name}')
            actual = getattr(draft, name)
            outcome['reference_comparisons'][name] = {
                'match': _datum(actual) == _datum(truth),
                # Do not persist reference values that may inadvertently contain PII.
            }
    return outcome


def error_category(errors: list[str]) -> str | None:
    """Label operational blockers separately from parser/validation failures."""
    combined = ' '.join(errors).casefold()
    if not combined:
        return None
    if 'dnsresolutionerror' in combined or 'name resolution' in combined:
        return 'dns_failure'
    if 'robots.txt' in combined or 'permissionerror' in combined:
        return 'access_or_robots'
    if 'connectionerror' in combined or 'timeoutexception' in combined:
        return 'network_failure'
    if 'http 403' in combined or 'http 401' in combined:
        return 'http_access_denied'
    if 'http 429' in combined:
        return 'rate_limited'
    return 'parser_or_other'


def make_report(*, source_key: str, url: str, family: str, mode: str,
                discovered: int = 0, attempted: int = 0,
                checks: list[dict] | None = None, errors: list[str] | None = None,
                reachable: bool | None = None, extraction_method: str = '',
                meta: dict | None = None) -> dict:
    if mode not in ('offline_fixture', 'live_public', 'live_domestic', 'catalog_only'):
        raise ValueError('Unknown verification mode')
    checks = checks or []
    errors = errors or []
    if min(discovered, attempted) < 0 or attempted < len(checks):
        raise ValueError('Invalid counts')
    if mode == 'catalog_only':
        status = 'not_checked'
    elif errors and not checks:
        status = 'failed'
    elif discovered == 0:
        status = 'empty_unverified' if not errors else 'failed'
    elif attempted == 0:
        status = 'list_only'
    elif not checks:
        status = 'failed'
    elif errors or len(checks) < attempted:
        status = 'partial'
    elif any(check['required_coverage'] < 0.75 for check in checks):
        status = 'needs_review'
    else:
        status = 'sample_ok'
    reference_results = [x['match'] for check in checks for x in check.get('reference_comparisons', {}).values()]
    return {
        'source_key': source_key,
        'url': url,
        'family': family,
        'mode': mode,
        'status': status,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'reachable': reachable if mode.startswith('live') else None,
        'discovered': discovered,
        'sample_attempted': attempted,
        'sample_parsed': len(checks),
        'required_coverage': round(sum(c['required_coverage'] for c in checks) / len(checks), 4) if checks else None,
        'optional_coverage': round(sum(c['optional_coverage'] for c in checks) / len(checks), 4) if checks else None,
        'reference_fields_checked': len(reference_results),
        'reference_fields_correct': sum(reference_results),
        'reference_exact_match_rate': round(sum(reference_results)/len(reference_results), 4) if reference_results else None,
        'sample_checks': checks,
        'errors': [str(e)[:240] for e in errors[:20]],
        'error_category': error_category(errors),
        'extraction_method': extraction_method,
        'meta': meta or {},
        'warnings': [
            'Snapshot/sample is not complete pagination; do not infer closure or full coverage.',
            'Field presence is not evidence of extraction correctness.'
        ],
    }


def report_summary(reports: list[dict]) -> dict:
    counts = {key: sum(x['status'] == key for x in reports)
              for key in ('sample_ok','needs_review','partial','failed','empty_unverified','list_only','not_checked')}
    live = [r for r in reports if r['mode'].startswith('live')]
    return {
        'total_sources': len(reports),
        'statuses': counts,
        'live_samples_attempted': len(live),
        'live_sample_ok': sum(r['status'] == 'sample_ok' for r in live),
        'sampled_details_parsed': sum(r['sample_parsed'] for r in reports),
        'note': 'These are snapshot/sample results, not full-site success rates or guaranteed data correctness.'
    }
