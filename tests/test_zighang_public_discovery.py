import json
import asyncio
from pathlib import Path
from app.aggregate_store import AggregateStore
from app.crawlers.aggregators import ZighangPublicCrawler, zighang_draft
from app.crawlers.base import JobRef
from app.crawlers.zighang_public import public_rows, development_candidates

MAIN='84355916-dc2b-4888-8485-4d0b2b468922'
RELATED='63caec0b-3eb4-4ff3-9922-1664f69589e5'


def row(identifier,title='백엔드 개발자',**fields):
    return {'id':identifier,'title':title,'deadlineType':'상시채용','endDate':None,
            'company':{'name':'테스트'},'regions':['서울'],'employeeTypes':['정규직'],**fields}


def html(rows):
    # The server may split a JSON record across Flight script chunks.
    text='9:'+json.dumps({'recruitment':rows[0],'related':rows[1:]},ensure_ascii=False)+'\n'
    midpoint=len(text)//2
    return ''.join('<script>self.__next_f.push('+json.dumps([1,part],ensure_ascii=False)+')</script>'
                   for part in (text[:midpoint],text[midpoint:]))


def test_related_candidates_are_filtered_before_detail_requests():
    rows=[row(MAIN,status='ACTIVE'),row(RELATED),
          row('00000000-0000-0000-0000-000000000001',status='EXPIRED'),
          row('00000000-0000-0000-0000-000000000002',endDate='2000-01-01'),
          row('00000000-0000-0000-0000-000000000003',title='사업개발 매니저')]
    parsed=public_rows(html(rows))
    assert [r['id'] for r in development_candidates(parsed,exclude=MAIN)]==[RELATED]


def test_expired_rolling_posting_uses_explicit_public_state_without_jsonld():
    document={'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'API 서비스 개발 업무'}]}]}
    raw=html([row(MAIN,status='EXPIRED',content=document)])
    draft=zighang_draft(raw,MAIN,'https://www.zighang.com/recruitment/'+MAIN)
    assert draft.source_status=='closed'
    assert draft.deadline_on is None
    assert 'API 서비스 개발' in draft.body_text
    assert public_rows('<script>self.__next_f.push(window.secret())</script>')=={}


def test_live_expired_public_fixture_is_preserved_as_closed():
    path=Path('reports/v2.4/zighang-dev-closure-check.html')
    if not path.exists():
        import pytest
        pytest.skip('Live public fixture not packaged')
    draft=zighang_draft(path.read_text(encoding='utf-8'),MAIN,'https://www.zighang.com/recruitment/'+MAIN)
    assert draft.source_status=='closed'
    assert draft.organization=='풀릭스'


def test_focused_crawler_expands_public_related_queue_without_api_calls(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.meta('zighang','collection_mode','development')
    crawler=ZighangPublicCrawler(store)
    document={'type':'doc','content':[{'type':'text','text':'프로그래밍 업무'}]}
    class Fetcher:
        async def get_text(self,url,**kwargs):
            assert url=='https://www.zighang.com/recruitment/'+MAIN
            return html([row(MAIN,status='ACTIVE',content=document),row(RELATED)])
    draft=asyncio.run(crawler.parse(Fetcher(),JobRef(MAIN,'https://www.zighang.com/recruitment/'+MAIN)))
    assert draft.source_status=='open'
    assert [r['id'] for r in store.due('zighang',focus='development')]==[RELATED]


def test_flight_utf8_text_length_with_no_newline_before_next_record():
    body='긴 본문\n둘째 줄: JavaScript 개발 경험'
    document={'type':'doc','content':[{'type':'text','text':'$a'}]}
    stream='a:T'+format(len(body.encode('utf-8')),'x')+','+body
    stream+='16:'+json.dumps({'job':row(MAIN,status='EXPIRED',content=document)},ensure_ascii=False)+'\n'
    raw='<script>self.__next_f.push('+json.dumps([1,stream],ensure_ascii=False)+')</script>'
    draft=zighang_draft(raw,MAIN,'https://www.zighang.com/recruitment/'+MAIN)
    assert draft.source_status=='closed'
    assert draft.body_text==body
    assert '$a' not in draft.body_text


def test_live_failed_posting_has_body_and_explicit_expired_state():
    path=Path('reports/v2.4/zighang-failed-8779.html')
    if not path.exists():
        import pytest
        pytest.skip('Live public fixture not packaged')
    identifier='8779a5c1-39c1-42b1-8102-a3a34128efbc'
    draft=zighang_draft(path.read_text(encoding='utf-8'),identifier,'https://www.zighang.com/recruitment/'+identifier)
    assert draft.source_status=='closed'
    assert '프론트엔드' in draft.title
    assert len(draft.body_text)>3000
    assert '지원 경력: 0~3년' in draft.body_text
