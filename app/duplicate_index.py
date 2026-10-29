"""In-memory candidate generation with bounded, company-blocked inverted lists.

Never merges records. Phase 1 exact application URL; phase 2 candidate search.
This is intended for batch review on bounded result sets. For millions of jobs,
materialize block keys in PostgreSQL and run incremental updates there.
"""
from __future__ import annotations
from collections import defaultdict
from .duplicates import _org_name, _title_parts, _rank_pair

from .company_identity import normalize_company

def candidate_pairs(jobs, *, max_candidates_per_job: int=40, max_pairs: int=100000):
    """Prefer newest rows, bound all comparisons; no cross-company comparisons.

    May omit a true duplicate where title tokens diverge; exact apply-url pass
    remains independent. This is a candidate retriever, not a merge decision.
    """
    if max_candidates_per_job<1 or max_pairs<1:
        raise ValueError('Candidate limits must be positive')
    buckets=defaultdict(list)
    for job in jobs:
        company=normalize_company(job.organization)
        if company:
            buckets[company].append(job)
    comparisons=0
    for company,group in buckets.items():
        inverted=defaultdict(list)
        by_id={j.id:j for j in group}
        for job in sorted(group,key=lambda j:j.id):
            _,tokens=_title_parts(job.title)
            # Require shared meaningful token. Bounded postings per title token.
            candidate_ids=set()
            for token in sorted(tokens):
                for prev in inverted[token][-max_candidates_per_job:]:
                    candidate_ids.add(prev.id)
            selected=sorted(candidate_ids,reverse=True)[:max_candidates_per_job]
            for prev_id in selected:
                prev=by_id[prev_id]
                if prev.source_id==job.source_id:
                    continue
                comparisons+=1
                if comparisons>max_pairs:
                    return
                yield prev,job
            for token in tokens:
                inverted[token].append(job)


def indexed_similar_job_candidates(jobs, *, max_candidates_per_job: int=40,
                                   max_pairs: int=100000, with_stats: bool=False):
    # Newer IDs first on output. Avoid costly all-pairs O(N^2) scans.
    result=[]
    examined=0
    for a,b in candidate_pairs(jobs,max_candidates_per_job=max_candidates_per_job,max_pairs=max_pairs):
        examined+=1
        candidate=_rank_pair(a,b)
        if candidate:
            result.append(candidate)
    result.sort(key=lambda x:-x['confidence'])
    return {'compared':examined,'candidates':result} if with_stats else result
