"""Read-only ATS fingerprinting and planning. Never treats a fingerprint as access permission.

No network access, no private endpoint discovery, no browser mitigation logic.
An exact public-ATS hostname can use an existing documented public-feed connector;
third-party SaaS CMS detection is only a hint for a *reviewed* HTML template.
"""
from __future__ import annotations
import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup

PUBLIC = {
    'job-boards.greenhouse.io': ('greenhouse', 'public_greenhouse'),
    'boards.greenhouse.io': ('greenhouse', 'public_greenhouse'),
    'jobs.lever.co': ('lever', 'public_lever'),
    'jobs.ashbyhq.com': ('ashby', 'public_ashby'),
    'careers.smartrecruiters.com': ('smartrecruiters', 'public_smartrecruiters'),
}
PLATFORMS = {
    'career.greetinghr.com': 'greeting',
    'ninehire.site': 'ninehire',
    'recruit.roundhr.com': 'roundhr',
    'jobs.workable.com': 'workable',
    'myworkdayjobs.com': 'workday',
}

@dataclass(frozen=True)
class FamilyDetection:
    family: str
    evidence: str
    certainty: str
    recommended_adapter: str
    auto_enabled: bool


def _suffix(host: str, pattern: str) -> bool:
    return host == pattern or host.endswith('.' + pattern)


def _contains_jobposting(value) -> bool:
    if isinstance(value,list):
        return any(_contains_jobposting(v) for v in value)
    if not isinstance(value,dict):
        return False
    types=value.get('@type') or []
    types=[types] if isinstance(types,str) else types
    if any(str(t).split('/')[-1]=='JobPosting' for t in types):
        return True
    return any(_contains_jobposting(value[k]) for k in ('@graph','itemListElement','item') if k in value)


def identify(url: str, html: str | None = None) -> FamilyDetection:
    parts=urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('HTTPS public URL required')
    host=parts.hostname.lower()
    for domain,(name,connector) in PUBLIC.items():
        if _suffix(host,domain):
            return FamilyDetection(name,'documented public ATS hostname','high',connector,True)
    for domain,name in PLATFORMS.items():
        if _suffix(host,domain):
            return FamilyDetection(name,'ATS-hosted career subdomain','high',
                                   'domestic_ats' if name in ('greeting','ninehire') else 'approved_company_listings',False)
    if not html:
        return FamilyDetection('unknown','URL alone not enough for custom domain','unknown','manual_review',False)
    soup=BeautifulSoup(html,'html.parser')
    # CDN/static asset metadata fingerprints, rather than a word in page text.
    assets=[]
    for tag in soup.select('script[src], link[href], iframe[src]'):
        address=tag.get('src') or tag.get('href') or ''
        assets.append(urlsplit(address).hostname or '')
    for domain,name in PLATFORMS.items():
        if any(_suffix(a,domain) for a in assets):
            return FamilyDetection(name,'linked ATS-managed script/asset/iframe','medium',
                                       'domestic_ats' if name in ('greeting','ninehire') else 'approved_company_listings',False)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed=json.loads(script.string or script.get_text())
        except (ValueError,TypeError):
            continue
        if _contains_jobposting(parsed):
            return FamilyDetection('jobposting_jsonld','embedded schema.org/JobPosting','high','company_jsonld',False)
    if soup.select_one('script#__NEXT_DATA__,script[data-nuxt-data],div#__nuxt'):
        return FamilyDetection('client_rendered','Next.js/Nuxt marker; not a job-data contract','medium','approved_company_listings',False)
    return FamilyDetection('generic_html','No stable ATS signature in supplied HTML','low','approved_company_listings',False)


def catalog_report(companies: list[dict]) -> dict:
    family_counts=Counter()
    stages=Counter()
    portal_members=defaultdict(list)
    verification=Counter()
    exact_members=defaultdict(list)
    for row in companies:
        family=identify(row['url']).family
        family_counts[family]+=1
        stages[str(row['priority_stage'])]+=1
        verification[row.get('url_verification','legacy_entry_not_reverified')]+=1
        # Company and brand portals that use a single URL can share one listing request.
        part=urlsplit(row['url'])
        portal=f'{part.scheme}://{part.netloc}{part.path.rstrip("/") or "/"}'
        portal_members[portal].append(row['id'])
        exact_members[row['url']].append(row['id'])
    groups=[{'portal':url,'companies':ids} for url,ids in portal_members.items() if len(ids)>1]
    return {'employer_targets':len(companies), 'stages':dict(sorted(stages.items())),
            'url_based_families':dict(sorted(family_counts.items())),
            'url_verification':dict(sorted(verification.items())),
            'unique_portal_paths':len(portal_members),
            'unique_exact_urls':len(exact_members),
            'exact_url_reuse_groups':[{'url':url,'companies':ids} for url,ids in sorted(exact_members.items()) if len(ids)>1],
            'shared_portal_groups':sorted(groups,key=lambda x:(-len(x['companies']),x['portal'])),
            'note':'URL patterns are hypotheses. Shared path (ignoring query) is only a potential grouping; exact URLs can share one request after authorization. Candidate homepages are not active crawlers.'}


def main():
    from .console import configure_console
    configure_console()
    parser=argparse.ArgumentParser(description='Offline site-family planner; does not fetch network data')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('report')
    detect=sub.add_parser('identify')
    detect.add_argument('url')
    detect.add_argument('--html',default='',help='Local HTML file, no network fetch')
    args=parser.parse_args()
    if args.command=='report':
        from .company_catalog import load_companies
        print(json.dumps(catalog_report(load_companies()),ensure_ascii=False,indent=2))
    else:
        html=Path(args.html).read_text(encoding='utf-8') if args.html else None
        print(json.dumps(asdict(identify(args.url,html)),ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
