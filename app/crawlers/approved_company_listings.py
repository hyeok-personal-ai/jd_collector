"""Explicitly approved first-party HTML career listing adapter (opt in).

Only follows detail links under configured same-origin URL prefix. No general
site discovery, hidden APIs, bypasses or inferred closures on partial lists.
The operator must confirm current terms and permissions before enabling.
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urljoin,urlsplit,urlunsplit
from bs4 import BeautifulSoup
from .base import BaseCrawler,JobRef
from .company_pages import parse_jsonld
from ..schema import JobDraft
from ..fetcher import PoliteFetcher


def load_listing_config(path: str | None=None) -> list[dict]:
    target=path or os.getenv('APPROVED_LISTINGS_FILE','')
    if not target:
        return []
    doc=json.loads(Path(target).read_text(encoding='utf-8'))
    data=doc.get('listings')
    if not isinstance(data,list) or len(data)>1000:
        raise ValueError('Expected listings array (max 1000)')
    seen=set()
    for row in data:
        if not isinstance(row,dict) or row.get('authorized') is not True:
            raise PermissionError('Each listing must have authorized=true after site policy review')
        url=urlsplit(str(row.get('url','')))
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in (None,443):
            raise ValueError('Approved listing must be a clean HTTPS URL')
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise ValueError('Literal IP address is prohibited')
        if url.hostname=='localhost' or url.hostname.endswith(('.local','.internal','.test')):
            raise ValueError('Private hostname is prohibited')
        if not row.get('company') or not row.get('detail_path_prefix'):
            raise ValueError('company and detail_path_prefix are required')
        mode=row.get('mode','html_css')
        if mode not in ('html_css','jsonld_itemlist'):
            raise ValueError('mode must be html_css or jsonld_itemlist')
        if mode=='html_css' and not row.get('selector'):
            raise ValueError('CSS selector required for html_css')
        prefix=row['detail_path_prefix']
        if not isinstance(prefix,str) or not prefix.startswith('/') or '..' in prefix or prefix=='/':
            raise ValueError('Specific detail_path_prefix is required')
        if row['url'] in seen:
            raise ValueError('Duplicate listing URL')
        seen.add(row['url'])
    return data


def extract_links(html: str, row: dict) -> list[tuple[str,str]]:
    if row.get('mode')=='jsonld_itemlist':
        return extract_jsonld_itemlist_links(html, row)
    base=urlsplit(row['url'])
    soup=BeautifulSoup(html,'html.parser')
    result={}
    for link in soup.select(row['selector']):
        if link.name!='a' or not link.get('href'):
            continue
        address=urlsplit(urljoin(row['url'],str(link['href'])))
        if (address.scheme!='https' or address.hostname!=base.hostname or address.port not in (None,443)
                or address.username or address.password or not address.path.startswith(row['detail_path_prefix'])):
            continue
        # Distinct job path is essential: prevent generic listings and login links.
        if address.path.rstrip('/')==row['detail_path_prefix'].rstrip('/'):
            continue
        canonical=urlunsplit(('https',base.hostname,address.path,address.query,''))
        title=link.get_text(' ',strip=True)
        if not title:
            continue
        result[canonical]=(canonical,title)
        if len(result)>=1000:
            break
    return list(result.values())


def extract_jsonld_itemlist_links(html: str, row: dict) -> list[tuple[str,str]]:
    """Only explicitly listed, same-origin, job-detail paths in JSON-LD ItemList.

    Never follows arbitrary web-discovered URLs. Incomplete/unrecognized lists
    cause a validation error (fail closed, no closure inference).
    """
    soup=BeautifulSoup(html,'html.parser')
    base=urlsplit(row['url'])
    found={}
    def visit(node):
        if isinstance(node,list):
            for obj in node:
                yield from visit(obj)
        elif isinstance(node,dict):
            kinds=node.get('@type',[])
            kinds=[kinds] if isinstance(kinds,str) else kinds
            if 'ListItem' in kinds or 'https://schema.org/ListItem' in kinds:
                item=node.get('item',{})
                target=item if isinstance(item,dict) else {'url':item}
                title=target.get('name') or node.get('name') or ''
                url=target.get('url') or node.get('url') or ''
                yield (str(url),str(title))
            for key in ('@graph','itemListElement'):
                if key in node:
                    yield from visit(node[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data=json.loads(script.string or script.get_text())
        except (ValueError,TypeError):
            continue
        for address,title in visit(data):
            parts=urlsplit(urljoin(row['url'],address))
            if (parts.scheme!='https' or parts.hostname!=base.hostname or
                parts.port not in (None,443) or parts.username or parts.password or
                not parts.path.startswith(row['detail_path_prefix']) or
                parts.path.rstrip('/')==row['detail_path_prefix'].rstrip('/')):
                continue
            url=urlunsplit(('https',base.hostname,parts.path,parts.query,''))
            if title.strip():
                found[url]=(url,title.strip())
            if len(found)>=1000:
                break
    return list(found.values())


class ApprovedCompanyListingCrawler(BaseCrawler):
    site_id='approved_company_listings'

    def __init__(self, *, max_pages: int | None=None):
        rows=load_listing_config()
        self.rows=rows[:max_pages] if max_pages else rows
        self.allowed_hosts=frozenset(urlsplit(row['url']).hostname for row in self.rows) or frozenset({'example.org'})

    async def discover(self,fetcher:PoliteFetcher) -> AsyncIterator[JobRef]:
        self.completed_scopes,self.scope_errors={},[]
        for row in self.rows:
            try:
                html=await fetcher.get_text(row['url'])
                links=extract_links(html,row)
                if not links:
                    raise ValueError('No same-origin job detail links found; parser review required')
                for url,title in links:
                    external=hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]
                    yield JobRef(external,url,None,payload={'company':row['company'],'title':title,'scope_id':row['url']})
            except Exception as exc:
                self.scope_errors.append(f"{row['url']}: {type(exc).__name__}: {str(exc)[:160]}")
        # Deliberately no completed scopes: an HTML list may be paginated/dynamic.

    async def parse(self,fetcher:PoliteFetcher,ref:JobRef) -> JobDraft:
        html=await fetcher.get_text(ref.url,capture=True)
        drafts=parse_jsonld(html,ref.url,ref.payload['company'])
        if drafts:
            x=drafts[0]
            return x.model_copy(update={'source_id':self.site_id,'external_id':ref.external_id,
                                       'original_url':ref.url})
        soup=BeautifulSoup(html,'html.parser')
        h1=soup.select_one('h1')
        main=soup.select_one('main') or soup.select_one('article')
        title=(h1.get_text(' ',strip=True) if h1 else '') or ref.payload['title']
        body=(main or soup.body)
        text=body.get_text(' ',strip=True)[:30000] if body else None
        if not title or not text or len(text)<40:
            raise ValueError('Insufficient detail HTML for a reliable posting')
        return JobDraft(source_id=self.site_id,external_id=ref.external_id,
                        title=title,organization=ref.payload['company'],original_url=ref.url,
                        apply_url=ref.url,body_text=text,recruitment_type='채용',source_status='unknown')
