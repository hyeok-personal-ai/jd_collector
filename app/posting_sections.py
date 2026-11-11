"""Shared extractive section boundaries for analysis, tags and embeddings."""
import re
from bs4 import BeautifulSoup

HEADINGS = {
    'duties':r'주요\s*업무|담당\s*업무|수행\s*업무|업무\s*내용|(?:key\s+|main\s+|roles and\s+)?responsibilities(?: and opportunities)?|what you(?:.ll| will) do|your impact|이런\s*일을\s*(?:해요|합니다)|합류하면\s*함께할\s*업무예요',
    'required':r'자격\s*요건(?:\s*및\s*기술)?|지원\s*자격|필수(?:\s*(?:요건|사항|조건))?|requirements?|(?:(?:minimum|basic|required|key)\s+)?qualifications|your (?:skills and qualifications|qualifications and skills)|what we.re looking for|how do i know if the role is right for me\??|必須条件|이런\s*분(?:을\s*찾(?:고\s*있(?:어요|습니다)|습니다|아요)|과\s*함께하고\s*싶어요)',
    'preferred':r'helpful experience(?:\s*\(not required\))?|우대(?:\s*(?:사항|조건|요건))?|(?:preferred|ideal) (?:qualifications|skills|experience)|nice[ -]to[ -]have|plus if you have|이런\s*분(?:이면|이라면)\s*더\s*(?:좋아요|좋습니다)',
    'conditions':r'life\s*&\s*benefits?|근무\s*(?:조건|환경|형태|장소)|고용\s*형태|복리\s*후생|혜택(?:\s*및\s*복지)?|보상(?:과\s*복리후생)?|benefits(?: and perks)?|work conditions|supporting your best work and your best life',
    'other':r'joining process|소개(?=\s*$)|함께\s*배우게\s*될\s*기술|함께\s*일하고\s*싶은\s*분|채용\s*절차|전형\s*절차|지원\s*방법|지원하러\s*가기|회사\s*소개|팀\s*소개|기술\s*스택|채용\s*과정|참고\s*사항|참고해\s*주세요|이렇게\s*합류해요|합류\s*여정|꼭\s*확인해\s*주세요|about (?:you|us|the role)|our team|how to apply|hiring process|why join\b[^\n]{0,80}|contact|culture|your growth|inclusion and diversity|what you.ll grow into|additional resources|[a-z]+ values(?=\s*[:：]?\s*$)',
}
# Clear conversational headings survive when a source flattens HTML to one line.
# Other inline words require an explicit colon; ordinary prose is not a heading.
FLAT_HEADINGS={
    'duties':r'이런\s*일을\s*(?:해요|합니다)',
    'required':r'이런\s*분(?:을\s*찾(?:고\s*있(?:어요|습니다)|습니다|아요)|과\s*함께하고\s*싶어요)',
    'preferred':r'이런\s*분(?:이면|이라면)\s*더\s*(?:좋아요|좋습니다)',
    'other':r'참고해\s*주세요|이렇게\s*합류해요',
}
HEADING = re.compile(r'(?im)^[ \t#*•\-\[【]*(?P<heading>'+ '|'.join(
    f'(?P<{key}>{value})' for key,value in HEADINGS.items()) + r')(?![a-z가-힣])[\]】 \t:：!\-]*')
FLAT_HEADING=re.compile(r'(?i)(?<!\S)(?:'+'|'.join(
    f'(?P<{key}>{value})' for key,value in FLAT_HEADINGS.items())+r')(?![a-z가-힣])[ \t!：:]*')
COLON_HEADING=re.compile(r'(?i)(?<!\S)(?:'+'|'.join(
    f'(?P<{key}>{value})' for key,value in HEADINGS.items())+r')[ \t]*[:：][ \t]*')
NON_REQUIRED=re.compile(r'필수(?:사항|조건|요건)?(?:이|가|는)?\s*(?:아니|아님|없)|not\s+(?:strictly\s+)?required|(?:언어|프레임워크)[^\n()]{0,24}(?:제약\s*없|무관)|제약\s*없음',re.I)


def plain_text(raw):
    raw=raw or ''
    if re.search(r'<(?:p|div|li|br|h[1-6])\b',raw,re.I):
        raw=BeautifulSoup(raw,'html.parser').get_text('\n',strip=True)
    return raw


def plain_body(job):
    return plain_text(job.body_text)


def extract_sections(job):
    raw=plain_body(job)
    sections={k:[] for k in ('duties','required','preferred','conditions','other')}
    matches=list(HEADING.finditer(raw))
    for pattern in (FLAT_HEADING,COLON_HEADING):
        for match in pattern.finditer(raw):
            if not any(match.start()<old.end() and old.start()<match.end() for old in matches):matches.append(match)
    matches=[match for match in matches if not (
        match.groupdict().get('required') and
        NON_REQUIRED.search(raw[match.start():raw.find('\n',match.start()) if '\n' in raw[match.start():] else len(raw)]))]
    matches.sort(key=lambda match:match.start())
    ranges=[('other',0,matches[0].start() if matches else len(raw))]
    for i,match in enumerate(matches):
        category=next(key for key in HEADINGS if match.groupdict().get(key) is not None)
        ranges.append((category,match.end(),matches[i+1].start() if i+1<len(matches) else len(raw)))
    for category,start,end in ranges:
        for line in re.finditer(r'[^\r\n]+',raw[start:end]):
            text=line.group().strip()
            if not text:continue
            kind=category
            # Only short explicit requirement phrases override their section.
            # A long introduction mentioning "essential to the service" or
            # generic hiring preference must not relabel all its technologies.
            if len(text)<=180 and re.search(r'(?:우대(?:합니다|해요|사항)?|preferred|nice to have|is a plus|will be beneficial)[.!\s]*$',text,re.I):kind='preferred'
            elif category=='other' and len(text)<=180 and re.search(r'^(?:must have|필수\s*[:：])|(?:필수(?:입니다)?|(?:is\s+)?required)[.!\s]*$',text,re.I):kind='required'
            offset=start+line.start()+len(line.group())-len(line.group().lstrip())
            sections[kind].append({'text':text,'start':offset,'end':offset+len(text),
                                   'source_url':getattr(job,'original_url',None),
                                   'non_required':bool(NON_REQUIRED.search(text)),
                                   'alternative':bool(re.search(r'또는|혹은|\bor\b|\bat least one\b|\bany of\b|중\s*(?:하나|한\s*개)|하나\s*이상|최소\s*하나|택\s*1',text,re.I))})
    # Zighang stores its summary followed by the employer description. Do not
    # count the shorter summary as independent requirements or let it hide
    # qualifications (degree/years) that only appear in the detailed text.
    # This legacy boundary is deliberately restricted to this collector.
    if getattr(job, 'source_id', None) == 'zighang':
        boundary = re.search(r'(?m)^소개\s*$', raw)
        if boundary and any(r['start'] > boundary.end() for r in sections['required']):
            for category in ('required', 'preferred', 'duties'):
                detailed = [r for r in sections[category] if r['start'] > boundary.end()]
                if detailed:
                    sections[category] = detailed
                    for row in detailed:
                        row['provenance'] = 'detail_after_summary'
    return sections
