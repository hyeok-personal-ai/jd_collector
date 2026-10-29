from datetime import date
import pytest
from app.crawlers.aggregators import wp_draft
from app.crawlers.public_feeds import parse_lever
from app.normalization import clean_draft
from app.schema import JobDraft


@pytest.mark.parametrize('period',['2026년 08월 01일 ~ 채용시 마감',
    '2026.07.03(금) ~ T.O 충원 시 채용 마감(조기마감 될 수 있음)',
    '2026.07.03부터 채용 시 마감'])
def test_rolling_application_start_is_not_a_deadline(period):
    row={'id':1,'link':'https://inthiswork.com/archives/1','status':'publish','date':'2026-09-15T00:00:00',
         'title':{'rendered':'개발자'},'categories':[191700167],
         'content':{'rendered':'<p>접수기간</p><p>'+period+'</p>'}}
    draft=wp_draft(row)
    assert draft.deadline_on is None
    assert draft.source_status=='open'


def test_finite_deadline_is_kept_when_early_close_is_possible():
    row={'id':1,'link':'https://inthiswork.com/archives/1','status':'publish','date':'2026-09-15T00:00:00',
         'title':{'rendered':'개발자'},'categories':[191700167],
         'content':{'rendered':'<p>접수기간: 2026.09.15 ~ 2099.12.31 (조기마감 가능)</p>'}}
    assert wp_draft(row).deadline_on==date(2099,12,31)


def test_lever_sections_are_saved_when_description_is_empty():
    draft=parse_lever({'id':'one','text':'개발자','hostedUrl':'https://jobs.lever.co/demo/one',
        'descriptionPlain':'','lists':[{'text':'지원 자격','content':'<li>Python 개발 경험</li>'}],
        'additionalPlain':'유연 근무'},'demo')
    assert 'Python 개발 경험' in draft.body_text
    assert '지원 자격' in draft.body_text
    assert '유연 근무' in draft.body_text


@pytest.mark.parametrize('region,expected',[('해외, KR','해외'),('Seoul, KR','Seoul, KR')])
def test_only_contradictory_overseas_country_suffix_is_removed(region,expected):
    draft=JobDraft(source_id='demo',external_id='1',title='개발자',original_url='https://example.org',region=region)
    assert clean_draft(draft).region==expected


def test_application_deadline_heading_is_recognized():
    row={'id':1,'link':'https://inthiswork.com/archives/1','status':'publish','date':'2026-10-28T00:00:00',
         'title':{'rendered':'개발자'},'categories':[191700167],
         'content':{'rendered':'<p>접수기한</p><p>2099.11.15 오후 11:59</p>'}}
    assert wp_draft(row).deadline_on==date(2099,11,15)
