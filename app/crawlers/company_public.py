"""Bounded public career-page collection from the curated employer catalog.

Only visible same-origin vacancy links and published JobPosting structured data.
Unknown layouts are reported as needing review, never treated as zero vacancies.
"""
from collections import Counter
import hashlib
import re
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

from bs4 import BeautifulSoup
from .base import BaseCrawler, JobRef
from .company_pages import parse_jsonld
from .domestic_ats import parse_domestic_detail
from ..schema import JobDraft
from ..site_family import identify

DETAIL = re.compile(r'/(?:c|o|job_posting|jobs?|positions?|postings?|recruits?|recruitments?|openings?|careers?)/[^/?]+|(?:job|recruit|posting|notice|anno)(?:id|_id|no|seq)=|/(?:rcrt|recruit)/view|[?&](?:idx|seq|sn)=', re.I)
CAREER = re.compile(r'채용|인재영입|인재채용|careers?|open positions|join us', re.I)
VACANCY = re.compile(r'개발|엔지니어|인턴|신입|경력|디자인|기획|매니저|마케팅|채용|연구|운영|보안|재무|회계|인사|법무|데이터|프로덕트|engineer|developer|intern|manager|designer|scientist|recruiter|sales|analyst|product|finance|operations', re.I)


def canonical(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Expected public HTTPS career URL')
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'gclid','fbclid'}
             and not (k == 'gh_jid' and re.search(r'/jobs/\d+', p.path))]
    path = quote(unquote(p.path or '/'), safe='/@:+')
    return urlunsplit(('https', p.hostname, path, urlencode(sorted(query)), ''))


def visible_links(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.select('a[href]'):
        try:
            target = canonical(urljoin(url, a['href']))
        except ValueError:
            continue
        text = a.get_text(' ', strip=True) or a.get('aria-label', '')
        if urlsplit(target).hostname != urlsplit(url).hostname or target == url or not text:
            continue
        if re.search(r'/(apply|login|privacy|terms|signup|register)(/|\?|$)', target, re.I):
            continue
        found[target] = text[:300]
    return found


def greeting_bootstrap_links(html,url):
    soup=BeautifulSoup(html,'html.parser')
    node=soup.select_one('script#__NEXT_DATA__')
    if node is None:return {}
    try:data=json.loads(node.get_text())
    except ValueError:return {}
    result={}
    for query in data.get('props',{}).get('pageProps',{}).get('dehydratedState',{}).get('queries',[]):
        if query.get('queryKey')!=['openings']:continue
        rows=query.get('state',{}).get('data')
        if not isinstance(rows,list):continue
        for row in rows:
            if row.get('deploy') is not True or not str(row.get('openingId','')).isdecimal() or not row.get('title'):continue
            target=urljoin(url,'/o/'+str(row['openingId']))
            result[target]=row['title']
    return result


def parse_detail(html, url, company, title=''):
    if re.search(r'/teams/|/Company/Careers/(?:Intro|List)(?:/|$)',url,re.I):
        raise ValueError('Career navigation/team overview is not an individual vacancy')
    family = identify(url).family
    if re.search(r'/(?:[a-z]{2}/)?o/\d+',url):
        family='greeting'
    elif '/job_posting/' in url:
        family='ninehire'
    if family in ('greeting', 'ninehire'):
        return parse_domestic_detail(html, url, {'family': family, 'url': url, 'company': company['name']}, listing_title=title)
    structured = parse_jsonld(html, url, company['name'])
    if len(structured) == 1 and structured[0].body_text:
        return structured[0]
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script,style,nav,footer,header,form,aside,noscript'):
        node.decompose()
    heading = soup.select_one('h1')
    heading = heading.get_text(' ', strip=True) if heading else title
    main = soup.select_one('main,article,[role="main"],.job-description,.recruitment-content')
    text = main.get_text('\n', strip=True) if main else ''
    if not heading or not VACANCY.search(heading) or len(text) < 100 or not re.search(r'자격|업무|지원|responsibilit|qualificat|requirements|experience', text, re.I):
        raise ValueError('Vacancy detail not recognized; review page layout')
    return JobDraft(source_id='company_public', external_id='pending', title=heading,
                    organization=None if company.get('shared_portal') else company['name'], original_url=url, apply_url=url,
                    body_text=text[:50000], recruitment_type='채용', source_status='unknown')


class CompanyPublicCrawler(BaseCrawler):
    site_id = 'company_public'

    def __init__(self, company, *, max_pages=3, render=False):
        self.company = company
        routes_file=Path(__file__).parents[1]/'assets/career_routes.json'
        routes=json.loads(routes_file.read_text(encoding='utf-8')) if routes_file.exists() else {}
        self.url = canonical(routes.get(company['id'],{}).get('url',company['url']))
        self.allowed_hosts = frozenset({urlsplit(self.url).hostname})
        self.max_pages = max_pages
        self.discovered_hosts = set(self.allowed_hosts)
        self.render = render

    def stable(self, url):
        return self.company['id'] + ':' + hashlib.sha256(canonical(url).encode()).hexdigest()[:24]

    async def discover(self, fetcher):
        self.completed_scopes, self.scope_errors = {}, []
        def approve(destination):
            p=urlsplit(destination)
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):return False
            if p.hostname in self.discovered_hosts or p.hostname.removeprefix('www.')==urlsplit(self.url).hostname.removeprefix('www.'):
                self.discovered_hosts.add(p.hostname)
                return True
            return False
        fetcher.approve_redirect=approve
        queue, visited, emitted = [self.url], set(), set()
        while queue and len(visited) < self.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            fetcher.allowed_hosts = frozenset(self.discovered_hosts)
            html = await fetcher.get_text(url)
            rows = parse_jsonld(html, url, self.company['name'])
            url_counts = Counter(row.original_url for row in rows)
            for draft in rows:
                # JSON-LD and visible anchors for the same vacancy share one identity.
                key = (self.stable(draft.original_url) if url_counts[draft.original_url] == 1
                       else self.company['id'] + ':' + draft.external_id)
                if key not in emitted:
                    emitted.add(key)
                    yield JobRef(key, draft.original_url, payload={'draft': draft, 'scope_id': self.url})
            soup = BeautifulSoup(html, 'html.parser')
            initial_host = urlsplit(self.url).hostname.removeprefix('www.')
            for anchor in soup.select('a[href]'):
                if not CAREER.search(anchor.get_text(' ', strip=True)):
                    continue
                try:
                    target = canonical(urljoin(url, anchor['href']))
                except ValueError:
                    continue
                host = urlsplit(target).hostname
                known_ats = host.endswith(('.greetinghr.com', '.ninehire.site', '.roundhr.com', '.greenhouse.io', '.lever.co', '.ashbyhq.com', '.smartrecruiters.com', '.recruiter.co.kr'))
                if (known_ats or host == initial_host or host.endswith('.'+initial_host)) and target not in visited:
                    self.discovered_hosts.add(host)
                    queue.append(target)
            links = visible_links(html, url)
            candidates = [(u, t) for u, t in links.items() if DETAIL.search(u) and
                          (VACANCY.search(t) or re.search(r'/(?:[a-z]{2}/)?o/\d+|/job_posting/|/c/[a-zA-Z0-9_-]+',u))]
            if not candidates:
                candidates=list(greeting_bootstrap_links(html,url).items())
            if re.search(r'/(?:[a-z]{2}/)?o/\d+|/job_posting/',url) and not candidates:
                key=self.stable(url)
                if key not in emitted:
                    emitted.add(key)
                    yield JobRef(key,url,payload={'title':'','scope_id':self.url})
            if not candidates and not rows and self.render:
                html = await fetcher.render_html(url)
                links = visible_links(html, url)
                candidates = [(u, t) for u, t in links.items() if DETAIL.search(u) and VACANCY.search(t)]
            for target, title in candidates:
                key = self.stable(target)
                if key not in emitted:
                    emitted.add(key)
                    yield JobRef(key, target, payload={'title': title, 'scope_id': self.url})
            for target, title in links.items():
                if CAREER.search(title) and not DETAIL.search(target) and target not in visited:
                    queue.append(target)
        if not emitted:
            raise ValueError('No recognized public vacancies; layout/JS/access review required')
        # Bounded HTML traversal never proves a complete listing or job closure.

    async def parse(self, fetcher, ref):
        draft = ref.payload.get('draft')
        if draft is None:
            html = await fetcher.get_text(ref.url, capture=True)
            try:
                draft = parse_detail(html, ref.url, self.company, ref.payload['title'])
            except ValueError:
                if not self.render:
                    raise
                html = await fetcher.render_html(ref.url)
                fetcher.capture_rendered_html(ref.url, html)
                draft = parse_detail(html, ref.url, self.company, ref.payload['title'])
        return draft.model_copy(update={'source_id': self.site_id, 'external_id': ref.external_id})
