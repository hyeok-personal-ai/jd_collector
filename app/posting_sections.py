"""Shared extractive section boundaries for analysis, tags and embeddings."""
import re
from bs4 import BeautifulSoup

HEADINGS = {
    'duties':r'주요\s*업무|담당\s*업무|수행\s*업무|업무\s*내용|responsibilities|what you.ll do|이런\s*일을\s*(?:해요|합니다)',
    'required':r'자격\s*요건|지원\s*자격|필수\s*(?:요건|사항|조건)|requirements?|(?:minimum|basic|required) qualifications|必須条件|이런\s*분을\s*찾',
    'preferred':r'우대\s*(?:사항|조건|요건)|preferred qualifications|nice to have|이런\s*분이면\s*더',
    'conditions':r'근무\s*(?:조건|환경|형태|장소)|고용\s*형태|복리\s*후생|혜택|benefits|work conditions',
    'other':r'채용\s*절차|전형\s*절차|지원\s*방법|회사\s*소개|팀\s*소개|기술\s*스택|채용\s*과정|참고\s*사항|about you|about us|our team|how to apply',
}
HEADING = re.compile(r'(?im)^[\s#*•\-\[【]*(?P<heading>'+ '|'.join(
    f'(?P<{key}>{value})' for key,value in HEADINGS.items()) + r')[\]】\s:：\-]*')


def plain_body(job):
    raw=job.body_text or ''
    if re.search(r'<(?:p|div|li|br|h[1-6])\b',raw,re.I):
        raw=BeautifulSoup(raw,'html.parser').get_text('\n',strip=True)
    return raw


def extract_sections(job):
    raw=plain_body(job)
    sections={k:[] for k in ('duties','required','preferred','conditions','other')}
    matches=list(HEADING.finditer(raw))
    ranges=[('other',0,matches[0].start() if matches else len(raw))]
    for i,match in enumerate(matches):
        category=next(key for key in HEADINGS if match.group(key) is not None)
        ranges.append((category,match.end(),matches[i+1].start() if i+1<len(matches) else len(raw)))
    for category,start,end in ranges:
        for line in re.finditer(r'[^\r\n]+',raw[start:end]):
            text=line.group().strip()
            if not text:continue
            kind=category
            if re.search(r'우대|preferred|nice to have',text,re.I):kind='preferred'
            elif category=='other' and re.search(r'필수|required|must have',text,re.I):kind='required'
            offset=start+line.start()+len(line.group())-len(line.group().lstrip())
            sections[kind].append({'text':text,'start':offset,'end':offset+len(text),
                                   'source_url':getattr(job,'original_url',None),
                                   'alternative':bool(re.search(r'또는|혹은|\bor\b|중\s*(?:하나|한\s*개)|택\s*1',text,re.I))})
    return sections
