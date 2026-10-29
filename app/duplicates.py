"""Conservative cross-source duplicate candidates.

Only an identical, sufficiently specific application URL is strong enough to
suggest grouping. Never merge source postings or infer sameness by title alone.
"""
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .database import Job

TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "source", "referrer"}


def canonical_apply_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        return None
    path = (parts.path.rstrip("/") or "/").lower()
    # Homepage, generic portal, or account pages are not job-level identities.
    if path in {"/", "/jobs", "/recruit", "/careers", "/apply", "/login", "/signin"}:
        return None
    if any(token in path for token in ("/login", "/signin", "/register", "/signup")):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    return urlunsplit(("https", parts.hostname.lower(), path, urlencode(sorted(query)), ""))


def duplicate_candidates(jobs: list[Job]) -> list[dict]:
    by_url: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        canonical = canonical_apply_url(job.apply_url)
        if canonical:
            by_url[canonical].append(job)
    results = []
    for url, matches in by_url.items():
        if len({m.source_id for m in matches}) >= 2:
            results.append({"shared_apply_url": url, "count": len(matches),
                            "jobs": [{"id": j.id, "source_id": j.source_id,
                                      "title": j.title, "original_url": j.original_url} for j in matches]})
    results.sort(key=lambda group: (-group["count"], group["shared_apply_url"]))
    return results

# V1.3: second-stage *candidate* detection after exact canonical apply URL.
# Never silently fuse source rows: identical employers often hire multiple roles.
import re
from difflib import SequenceMatcher
from itertools import combinations

_GENERIC_TITLE = {'채용', '모집', '신입', '경력', '직원', '정규직', '인턴', '담당자', '공고', '수시채용', '급구'}


def _org_name(value: str | None) -> str:
    text = (value or '').lower()
    text = re.sub(r'\(주\)|㈜|주식회사|inc\.?|co\.?\s*ltd\.?|limited|corp\.?', '', text)
    return re.sub(r'[^가-힣a-z0-9]', '', text)


def _title_parts(value: str) -> tuple[str, set[str]]:
    text = re.sub(r'[^가-힣a-z0-9+#.]+', ' ', value.lower())
    words = [x for x in text.split() if x not in _GENERIC_TITLE]
    return ' '.join(words), {w for w in words if len(w) >= 2}


def _rank_pair(a: Job, b: Job) -> dict | None:
    if a.source_id == b.source_id:
        return None
    from .company_identity import normalize_company
    if not normalize_company(a.organization) or normalize_company(a.organization) != normalize_company(b.organization):
        return None
    if a.deadline_on and b.deadline_on and a.deadline_on != b.deadline_on:
        return None  # Avoid merging distinct cohorts.
    # Distinct recruitment rounds/req numbers often share almost identical titles.
    # Refuse a fuzzy candidate when explicit numeric qualifiers differ.
    a_numbers = set(re.findall(r'(?<![가-힣a-z0-9])\d+(?![가-힣a-z0-9])', a.title.lower()))
    b_numbers = set(re.findall(r'(?<![가-힣a-z0-9])\d+(?![가-힣a-z0-9])', b.title.lower()))
    if a_numbers != b_numbers:
        return None
    atitle, aw = _title_parts(a.title)
    btitle, bw = _title_parts(b.title)
    if not atitle or not btitle:
        return None
    ratio = SequenceMatcher(None, atitle, btitle).ratio()
    jaccard = len(aw & bw) / len(aw | bw) if aw | bw else 0
    # Similar title alone is insufficient: require a second supporting field.
    shared_deadline = bool(a.deadline_on and a.deadline_on == b.deadline_on)
    shared_region = bool(a.region and b.region and a.region.strip() == b.region.strip())
    if (ratio < .84 and jaccard < .85) or not (shared_deadline or shared_region):
        return None
    score = round(min(0.98, .47 + .31 * max(ratio, jaccard) + (.13 if shared_deadline else 0)
                      + (.07 if shared_region else 0)), 3)
    return {'confidence': score, 'reason': ['company_exact', 'title_similar']
            + (['deadline_equal'] if shared_deadline else [])
            + (['region_equal'] if shared_region else []),
            'jobs': [{'id': x.id, 'source_id': x.source_id, 'title': x.title,
                      'original_url': x.original_url} for x in (a, b)]}


def similar_job_candidates(jobs: list[Job], *, max_pairs: int = 10000,
                           max_group: int = 300) -> list[dict]:
    """Company-blocked candidate generation. Bounded comparisons and no auto-merge.

    Candidate recall is deliberately conservative. In particular missing company
    or both deadline and region results in no automatic suggestion.
    """
    company_buckets: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        org = _org_name(job.organization)
        if org:
            company_buckets[org].append(job)
    results = []
    compared = 0
    for org, group in company_buckets.items():
        if len(group) > max_group:
            # Large staffing agencies frequently post thousands of distinct jobs.
            continue
        for left, right in combinations(group, 2):
            if left.source_id == right.source_id:
                continue
            compared += 1
            if compared > max_pairs:
                return sorted(results, key=lambda x: -x['confidence'])
            match = _rank_pair(left, right)
            if match:
                results.append(match)
    return sorted(results, key=lambda x: -x['confidence'])


def exact_duplicate_groups(jobs: list[Job]) -> list[dict]:
    """Read-only cross-site clusters by job-specific application URL.

    No fuzzy transitive mergers. Same-site duplicates cannot form a group alone.
    """
    groups = duplicate_candidates(jobs)
    return [{'key': g['shared_apply_url'], 'confidence': 'exact_application_url',
             'review_required': False, 'members': g['jobs'], 'count': g['count']}
            for g in groups]
