"""Small, reviewed legal-name/brand spelling aliases for matching candidates.

Do NOT map subsidiaries to parent companies: subsidiary and parent postings
may be different roles. Similarity evidence is still required after blocking.
"""
import re

ALIASES = {
    '네이버':'naver', 'navercorp':'naver','naver':'naver',
    '카카오':'kakao','kakao':'kakao',
    '쿠팡':'coupang','coupang':'coupang',
    '당근':'daangn','당근마켓':'daangn','daangn':'daangn',
    '우아한형제들':'woowahan','배달의민족':'woowahan','woowahan':'woowahan',
    '토스':'toss','비바리퍼블리카':'toss','vivarepublica':'toss',
    '직방':'zigbang','zigbang':'zigbang',
    '컬리':'kurly','kurly':'kurly',
    '무신사':'musinsa','musinsa':'musinsa',
    '여기어때':'gccompany','여기어때컴퍼니':'gccompany',
    '퓨리오사ai':'furiosaai','furiosaai':'furiosaai',
}


def normalize_company(value: str | None) -> str:
    text = (value or '').lower()
    text = re.sub(r'\(주\)|㈜|주식회사|inc\.?|co\.?\s*ltd\.?|limited|corp\.?', '', text)
    org = re.sub(r'[^가-힣a-z0-9]', '', text)
    return ALIASES.get(org,org)
