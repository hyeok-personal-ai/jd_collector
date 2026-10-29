"""Selected public Zighang links, without accepting arbitrary fetch destinations."""
import re

PUBLIC_JOB = re.compile(r'https://(?:www\.)?zighang\.com/recruitment/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?(?:\?[^#\s]*)?(?:#[^\s]*)?')


def candidate_links(urls):
    rows = {}
    for url in urls:
        match = PUBLIC_JOB.fullmatch(url.strip())
        if not match:
            raise ValueError('직행의 공개 공고 상세 주소만 추가할 수 있습니다.')
        identifier = match[1].lower()
        rows[identifier] = {'id': identifier, 'url': 'https://www.zighang.com/recruitment/'+identifier}
    return list(rows.values())
