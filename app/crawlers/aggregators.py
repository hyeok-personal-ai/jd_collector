"""Official public WordPress REST and robots-advertised Zighang sitemap/SSR.

Zighang's API origin disallows bots; it is deliberately never requested here.
No login, private endpoint, POST or anti-bot workaround is used.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from zoneinfo import ZoneInfo
from html import unescape
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from .base import BaseCrawler, JobRef, PostingUnavailable
from .company_pages import parse_jsonld
from ..aggregate_store import AggregateStore
from ..schema import JobDraft, Attachment
from ..deadline_policy import review_metadata, FLEXIBLE_BODY

WP_ROOT='https://inthiswork.com/wp-json/wp/v2'
JOB_CATEGORIES={191700167,191700168}


def wp_draft(row):
    if not isinstance(row,dict) or not row.get('id') or not row.get('link'):
        raise ValueError('Invalid WordPress post')
    if row.get('status')!='publish':
        raise ValueError('Non-public WordPress post')
    title=BeautifulSoup(row['title']['rendered'],'html.parser').get_text(' ',strip=True)
    protected=row.get('content',{}).get('protected',False)
    soup=BeautifulSoup('' if protected else row['content']['rendered'],'html.parser')
    for n in soup.select('script,style,iframe'): n.decompose()
    text=soup.get_text('\n',strip=True).replace('\u200b','')
    attachments=[]
    for n in soup.select('img[src],a[href]'):
        u=n.get('src') if n.name=='img' else n.get('href')
        if u and u.startswith('https://') and (n.name=='img' or re.search(r'\.(pdf|hwp|docx?|png|jpe?g)(\?|$)',u,re.I)):
            attachments.append(Attachment(name=n.get('alt') or n.get_text(' ',strip=True) or '공고 이미지/첨부',url=u))
    apply=row['link'] if row['link'].startswith('https://') and urlsplit(row['link']).hostname!='inthiswork.com' else None
    for n in soup.select('a[href]'):
        u=n.get('href','')
        if apply is None and u.startswith('https://') and any(w in n.get_text() for w in ('지원','채용사이트','공고 원문')):
            apply=u;break
    org=re.split('[｜|\uff5c]',title,maxsplit=1)[0].strip()
    if org==title:org=None
    deadline=None
    lines=text.splitlines()
    region=employment=None
    for i,line in enumerate(lines):
        heading=line.strip(' Ι|[]:•ㆍ')
        match=re.match(r'^(근무지역|근무지|회사위치|근무장소|근무주소|근무형태|고용형태)\s*[:：]?\s*(.*)',heading)
        if not match:continue
        value=match[2].strip() or (lines[i+1].strip() if i+1<len(lines) else '')
        if match[1] in ('근무형태','고용형태'):
            if len(value)<80 and re.search(r'정규|계약|인턴|기간제|아르바이트',value):employment=value
        elif len(value)<200 and re.search(r'서울|경기|수원|성남|용인|화성|인천|부산|대전|대구|광주|울산|세종|충청|충북|충남|경상|경북|경남|전라|전북|전남|강원|제주|Seoul|Korea|Tokyo|Singapore',value,re.I):
            region=value[:150]
    for i,line in enumerate(lines):
        if re.search(r'접수기간|지원기간|모집기간|접수기한|지원기한|접수마감|마감일|마감기한',line):
            area=' '.join(lines[i:i+3])[:250]
            dates=[]
            matches=list(re.finditer(r'(20\d{2})[.년/\-]\s*(\d{1,2})[.월/\-]\s*(\d{1,2})',area))
            for match in matches:
                year,month,day=match.groups()
                try:dates.append(date(int(year),int(month),int(day)))
                except ValueError:pass
            # A lone date before '~ 채용 시 마감' is the opening date, not a deadline.
            tail=area[matches[0].end():] if matches else ''
            rolling_start=(len(dates)==1 and re.search(r'[~～〜]|부터',tail)
                           and FLEXIBLE_BODY.search(tail))
            if dates and not rolling_start:deadline=max(dates)
    closed=deadline and deadline<datetime.now(ZoneInfo('Asia/Seoul')).date()
    hint=FLEXIBLE_BODY.search(text)
    return JobDraft(source_id='inthiswork',external_id=str(row['id']),title=title,
        original_url=f"https://inthiswork.com/archives/{row['id']}",organization=org,recruitment_type='채용',
        region=region,employment_type=employment,
        posted_on=date.fromisoformat(row['date'][:10]),body_text=text or None,attachments=attachments,
        apply_url=apply,deadline_on=deadline,deadline_text='본문 비밀번호 보호; 공개 제목만 저장' if protected else (hint[0] if hint else None),
        source_status='closed' if closed else ('open' if not protected and set(row.get('categories',[]))&JOB_CATEGORIES else 'unknown'))


def sitemap_urls(text):
    if len(text)>20_000_000 or '<!DOCTYPE' in text.upper():raise ValueError('Unsafe or oversized sitemap')
    root=ET.fromstring(text)
    return [(n.findtext('{*}loc') or '',n.findtext('{*}lastmod') or '') for n in root]


def zighang_draft(html, identifier, url, *, rows=None):
    from .zighang_public import public_rows, row_draft
    rows=public_rows(html) if rows is None else rows
    row=rows.get(identifier)
    if row and 'status' in row:
        return row_draft(row,url)
    drafts=parse_jsonld(html,url,'')
    if len(drafts)!=1 or not drafts[0].body_text:
        raise ValueError('Zighang public detail has no validated JobPosting')
    draft=drafts[0]
    soup=BeautifulSoup(html,'html.parser')
    apply=None
    for n in soup.select('a[href]'):
        u=n.get('href','')
        if u.startswith('https://') and any(w in n.get_text() for w in ('공고 원문','지원하기','다른 플랫폼')):
            apply=u;break
    visible=soup.get_text(' ',strip=True)
    hint=FLEXIBLE_BODY.search(visible)
    if (draft.deadline_on and draft.deadline_on<datetime.now(ZoneInfo('Asia/Seoul')).date()) or re.search(r'마감일\s*마감(?:\s|$)',visible):
        status='closed'
    elif draft.deadline_on or re.search(r'마감일\s*(?:D-\d+|상시|채용시)|채용시\s*마감',visible):
        status='open'
    else:status='unknown'
    return draft.model_copy(update={'source_id':'zighang','external_id':identifier,
        'original_url':url,'apply_url':apply or url,'source_status':status,'deadline_text':hint[0] if hint else None})


class InThisWorkPublicCrawler(BaseCrawler):
    site_id='inthiswork'
    allowed_hosts=frozenset({'inthiswork.com'})

    def __init__(self,store=None,max_pages=None):
        self.store=store or AggregateStore();self.max_pages=max_pages;self.review={}

    async def discover(self,fetcher):
        self.completed_scopes={};self.scope_errors=[]
        # Public recruitment categories, all roles, no IT-only filter.
        s=self.store;source=self.site_id
        s.meta(source,'scope','현재 공개 신입/인턴·주니어경력 전체 (비공개/커뮤니티 제외)')
        last=s.meta(source,'last_scan') or 0
        full_scan=time.time()-last>21600 or bool(s.pending_pages(source,1))
        if time.time()-last>21600 and not s.pending_pages(source,1):
            s.reset_pages(source)
            s.meta(source,'scan_started',time.time())
        started=s.meta(source,'scan_started') or 0
        first=f'{WP_ROOT}/posts?categories=191700167,191700168&per_page=100&page=1&orderby=id&order=desc'
        response=await fetcher.get_response(first)
        data=json.loads(response.text)
        if not isinstance(data,list) or response.total_pages is None:raise ValueError('WordPress pagination totals missing')
        if not full_scan and s.meta(source,'scanned_unique_count')!=response.total:
            s.reset_pages(source)
            started=time.time()
            s.meta(source,'scan_started',started)
            full_scan=True
        s.meta(source,'expected',response.total)
        pages=[first.replace('page=1&orderby',f'page={i}&orderby') for i in range(1,response.total_pages+1)]
        s.active_pages(source,pages)
        s.save_page(source,first,[{'id':str(r['id']),'url':f"https://inthiswork.com/archives/{r['id']}",'revision':r['modified'],'payload':r} for r in data])
        for url in s.pending_pages(source,self.max_pages or 10000):
            result=await fetcher.get_response(url)
            rows=json.loads(result.text)
            if not isinstance(rows,list):raise ValueError('WordPress page is not a list')
            s.save_page(source,url,[{'id':str(r['id']),'url':f"https://inthiswork.com/archives/{r['id']}",'revision':r['modified'],'payload':r} for r in rows])
        if full_scan and not s.pending_pages(source,1):
            s.meta(source,'last_scan',time.time())
            s.meta(source,'scanned_unique_count',s.scan_count(source,started))
        if started:
            for row in s.absent(source,started):
                yield JobRef(row['id'],row['url'],listing_fingerprint='availability:'+str(int(started)),
                             payload={'draft':row['payload'],'refresh':True})
        for row in s.due(source,10000):
            refresh=bool(started and row['seen']<started)
            yield JobRef(row['id'],row['url'],listing_fingerprint=('availability:'+str(int(started))) if refresh else row['revision'],
                         payload={'draft':row['payload'],'refresh':refresh})

    async def parse(self,fetcher,ref):
        draft=await self._parse(fetcher,ref)
        self.review[ref.external_id]=review_metadata(draft)
        return draft

    async def _parse(self,fetcher,ref):
        if ref.payload.get('refresh'):
            try:
                row=json.loads(await fetcher.get_text(f'{WP_ROOT}/posts/{ref.external_id}'))
            except RuntimeError as exc:
                if 'HTTP 404' not in str(exc):raise
                # A removed/private page is not proof of a deadline. Preserve the saved text,
                # remove the open assertion and tell the applicant to verify the source.
                return wp_draft(ref.payload['draft']).model_copy(update={'source_status':'unknown',
                    'deadline_text':'공개 모집 목록에서 제외·원문 비공개/삭제; 지원 상태 확인 필요'})
            draft=wp_draft(row)
            if not set(row.get('categories',[]))&JOB_CATEGORIES:
                return draft.model_copy(update={'source_status':'closed','deadline_text':'원본에서 모집 분류 제외 확인'})
            return draft
        return wp_draft(ref.payload['draft'])

    def on_success(self,ref):self.store.mark(self.site_id,ref.external_id,'done',**self.review.get(ref.external_id,{}))
    def on_failure(self,ref,error):self.store.mark(self.site_id,ref.external_id,'failed',str(error))


class ZighangPublicCrawler(BaseCrawler):
    site_id='zighang'
    allowed_hosts=frozenset({'zighang.com','www.zighang.com'})

    def __init__(self,store=None,scan_pages=5,max_pages=None):
        self.store=store or AggregateStore();self.scan_pages=max_pages or scan_pages;self.review={}

    async def discover(self,fetcher):
        self.completed_scopes={};self.scope_errors=[]
        s=self.store;source=self.site_id
        if s.meta(source,'collection_mode') == 'development':
            from .zighang_public import COMPANY_SOURCE,public_rows,company_candidates,company_urls
            s.meta(source,'scope','공개 기업 첫 목록·관련 공고에서 새 개발 공고 발견; 전체 기업·전체 페이지 확보를 뜻하지 않음')
            for url in s.due_discovery_pages(COMPANY_SOURCE,5):
                try:
                    html=await fetcher.get_text(url)
                    rows=public_rows(html)
                    candidates=company_candidates(rows,url,s.checked_refs(source,rows))
                    s.add_focus(source,candidates,provenance='public_company_page')
                    s.pages(COMPANY_SOURCE,company_urls(rows))
                    s.mark_discovery_page(COMPANY_SOURCE,url,count=len(candidates))
                except PermissionError:
                    s.mark_discovery_page(COMPANY_SOURCE,url,state='failed',error='Access policy/security challenge; no bypass')
                    raise
                except Exception as exc:
                    s.mark_discovery_page(COMPANY_SOURCE,url,state='failed',error=str(exc))
                    if any(code in str(exc) for code in ('HTTP 401','HTTP 403','HTTP 429')):raise
            for row in s.due(source,10000,focus='development'):
                yield JobRef(row['id'],row['url'],listing_fingerprint=row['revision'],payload=row['payload'])
            return
        s.meta(source,'scope','공식 사이트맵의 공개 공고; 신규 주소 탐색과 저장 공고 마감 재확인')
        index=await fetcher.get_text('https://zighang.com/seo/sitemap/sitemap-index.xml')
        pages=[u for u,_ in sitemap_urls(index) if re.fullmatch(r'https://zighang.com/seo/sitemap/sitemap-recruitment-\d+\.xml',u)]
        if not pages:raise ValueError('No official recruitment sitemaps')
        pages.sort(key=lambda u:int(re.search(r'-(\d+)\.xml',u)[1]))
        s.pages(source,pages)
        stale=s.stale_pages(source,self.scan_pages)
        for url in stale+s.pending_pages(source,max(0,self.scan_pages-len(stale))):
            text=await fetcher.get_text(url)
            rows=[]
            for target,revision in sitemap_urls(text):
                m=re.fullmatch(r'https://(?:www\.)?zighang.com/recruitment/([0-9a-f-]{36})',target)
                if m:rows.append({'id':m[1],'url':f'https://www.zighang.com/recruitment/{m[1]}','revision':revision})
            if not rows:raise ValueError('Empty/unrecognized recruitment sitemap')
            s.save_page(source,url,rows)
        s.meta(source,'last_scan',time.time())
        for row in s.due(source,10000):
            yield JobRef(row['id'],row['url'],listing_fingerprint=row['revision'])

    async def parse(self,fetcher,ref):
        from .zighang_public import row_draft,company_urls,COMPANY_SOURCE
        payload=ref.payload or {}
        if (isinstance(payload.get('public_row'),dict) and payload['public_row'].get('id')==ref.external_id
                and 0<=time.time()-payload.get('observed_at',0)<3600):
            draft=row_draft(payload['public_row'],ref.url)
            self.review[ref.external_id]=review_metadata(draft)
            return draft
        try:html=await fetcher.get_text(ref.url,capture=True)
        except RuntimeError as exc:
            if 'HTTP 404' in str(exc) or 'HTTP 410' in str(exc):raise PostingUnavailable(str(exc)) from exc
            raise
        from .zighang_public import public_rows,development_candidates
        rows=public_rows(html)
        draft=zighang_draft(html,ref.external_id,ref.url,rows=rows)
        if self.store.meta(self.site_id,'collection_mode')=='development':
            self.store.add_focus(self.site_id,development_candidates(rows,exclude=ref.external_id),provenance='public_ssr_related')
            self.store.pages(COMPANY_SOURCE,company_urls(rows))
        self.review[ref.external_id]=review_metadata(draft)
        return draft

    def on_success(self,ref):self.store.mark(self.site_id,ref.external_id,'done',**self.review.get(ref.external_id,{}))
    def on_failure(self,ref,error):self.store.mark(self.site_id,ref.external_id,'failed',str(error))
    def on_unavailable(self,ref):self.store.mark(self.site_id,ref.external_id,'unavailable','원문 404/410; 본문 수집 불가',active=False)
