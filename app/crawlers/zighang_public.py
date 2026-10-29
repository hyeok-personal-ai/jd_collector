"""Read JSON embedded in public SSR HTML; never execute scripts or request the API."""
import json
import re
import hashlib
import time
from datetime import date
from ..deadline_policy import today_kst
from ..development_focus import PUBLIC_JOB
from ..job_labels import label_job
from ..schema import JobDraft

COMPANY_SOURCE='zighang_company'
NON_DEVELOPMENT_FAMILIES={'기획_전략_경영','마케팅_광고_홍보','영업','디자인','인사_HR','상품기획_MD','미디어_콘텐츠'}


def development_row(row):
    families=row.get('depthOnes') or []
    if families and all(family in NON_DEVELOPMENT_FAMILIES for family in families):return False
    return label_job(JobDraft(source_id='zighang',external_id=row['id'],title=row['title'],
        original_url='https://www.zighang.com/recruitment/'+row['id'])).it_match=='yes'


def public_rows(html):
    if len(html)>20_000_000:
        raise ValueError('Oversized public SSR page')
    chunks=[]
    for match in re.finditer(r'<script\b[^>]*>\s*self\.__next_f\.push\((.*?)\)\s*;?\s*</script>',html,re.S):
        try:frame=json.loads(match[1])
        except ValueError:continue
        if isinstance(frame,list) and len(frame)>1 and frame[0]==1 and isinstance(frame[1],str):
            chunks.append(frame[1])
    # Flight T records contain raw text with a UTF-8 byte length, not a newline
    # terminator. A following JSON record may start immediately after that text.
    data=''.join(chunks).encode('utf-8')
    roots=[]
    strings={}
    decoder=json.JSONDecoder()
    offset=0
    while offset<len(data):
        if data[offset:offset+1] in (b'\n',b'\r'):
            offset+=1
            continue
        header=re.match(rb'([0-9a-fA-F]+):',data[offset:])
        if not header:
            newline=data.find(b'\n',offset)
            if newline<0:break
            offset=newline+1
            continue
        identifier=header[1].decode('ascii')
        start=offset+header.end()
        text_header=re.match(rb'T([0-9a-fA-F]+),',data[start:])
        if text_header:
            text_start=start+text_header.end()
            end=text_start+int(text_header[1],16)
            if end>len(data):break
            strings[identifier]=data[text_start:end].decode('utf-8')
            offset=end
            continue
        end=data.find(b'\n',start)
        if end<0:end=len(data)
        try:
            root=decoder.raw_decode(data[start:end].decode('utf-8'))[0]
        except ValueError:
            offset=end+1
            continue
        if isinstance(root,str):strings[identifier]=root
        roots.append(root)
        offset=end+1

    def resolve(node):
        if isinstance(node,str):
            if node.startswith('$$'):return node[1:]
            match=re.fullmatch(r'\$([0-9a-fA-F]+)',node)
            return strings.get(match[1],node) if match else node
        if isinstance(node,list):return [resolve(value) for value in node]
        if isinstance(node,dict):return {key:resolve(value) for key,value in node.items()}
        return node

    rows={}
    for root in roots:
        root=resolve(root)
        stack=[root]
        while stack:
            node=stack.pop()
            if isinstance(node,dict):
                identifier=node.get('id')
                if (isinstance(identifier,str) and PUBLIC_JOB.fullmatch('https://zighang.com/recruitment/'+identifier)
                        and isinstance(node.get('title'),str) and 'deadlineType' in node):
                    if identifier not in rows or 'status' in node:
                        rows[identifier]=node
                stack.extend(node.values())
            elif isinstance(node,list):stack.extend(node)
    return rows


def document_text(node):
    if not isinstance(node,dict):return ''
    if isinstance(node.get('text'),str):return node['text']
    return '\n'.join(filter(None,(document_text(child) for child in node.get('content',[]) if isinstance(child,dict))))


def row_draft(row,url):
    body='\n\n'.join(filter(None,(document_text(row.get('summary')),document_text(row.get('content')))))
    if not body:raise ValueError('Zighang public detail has no validated JobPosting body')
    lower,upper=row.get('careerMin'),row.get('careerMax')
    if type(lower) is int and type(upper) is int and 0<=lower<=upper<=80 and upper>0:
        body=f'지원 경력: {lower}~{upper}년\n\n'+body
    elif type(lower) is int and 0<lower<=80 and upper in (None,0):
        body=f'지원 경력: {lower}년 이상\n\n'+body
    def parsed_date(value):
        try:return date.fromisoformat(value[:10]) if isinstance(value,str) else None
        except ValueError:return None
    deadline=parsed_date(row.get('endDate'))
    state=row.get('status')
    status='closed' if state in ('EXPIRED','CLOSED') or (deadline and deadline<today_kst()) else (
        'open' if state in ('ACTIVE','OPEN','ONGOING','PUBLISHED') else 'unknown')
    apply=row.get('redirectUrl')
    company=row.get('company') or {}
    return JobDraft(source_id='zighang',external_id=row['id'],title=row['title'],original_url=url,
        organization=company.get('name') if isinstance(company,dict) else None,body_text=body,
        posted_on=parsed_date(row.get('createdAt')),deadline_on=deadline,deadline_text=row.get('deadlineType'),
        source_status=status,recruitment_type='채용',region=', '.join(row.get('regions') or []) or None,
        employment_type=', '.join(row.get('employeeTypes') or []) or None,
        apply_url=apply if isinstance(apply,str) and apply.startswith('https://') else url)


def development_candidates(rows, *, exclude=None):
    selected=[]
    for identifier,row in rows.items():
        if identifier==exclude or row.get('status') in ('EXPIRED','CLOSED'):continue
        end=row.get('endDate')
        try:
            if isinstance(end,str) and date.fromisoformat(end[:10])<today_kst():continue
        except ValueError:pass
        url='https://www.zighang.com/recruitment/'+identifier
        if development_row(row):
            selected.append({'id':identifier,'url':url,'title':row['title']})
    return selected


def company_urls(rows):
    urls=set()
    for row in rows.values():
        if not development_row(row):continue
        company=row.get('company') or {}
        identifier=company.get('id') if isinstance(company,dict) else None
        if isinstance(identifier,str) and PUBLIC_JOB.fullmatch('https://zighang.com/recruitment/'+identifier):
            urls.add('https://www.zighang.com/company/'+identifier)
    return sorted(urls)


def company_candidates(rows,source_page,known=()):
    """Only current candidates, plus explicit status updates for previously checked refs."""
    selected=[]
    for identifier,row in rows.items():
        if not development_row(row):continue
        candidate=development_candidates({identifier:row})
        if not candidate and identifier not in known:continue
        item={'id':identifier,'url':'https://www.zighang.com/recruitment/'+identifier,'title':row['title']}
        if row.get('status') and (document_text(row.get('summary')) or document_text(row.get('content'))):
            keys=('id','title','company','summary','content','status','endDate','deadlineType',
                  'createdAt','regions','employeeTypes','careerMin','careerMax','redirectUrl')
            revision=json.dumps({key:row.get(key) for key in keys},ensure_ascii=False,sort_keys=True)
            item['revision']='public-company:'+hashlib.sha256(revision.encode('utf-8')).hexdigest()
            item['payload']={'public_row':row,'source_page':source_page,'observed_at':time.time()}
        selected.append(item)
    return selected
