"""Offline representative HTML fragments; NOT live-site integration tests."""
import gzip
from datetime import date
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.crawlers.real_sites import listing_refs, detail_draft, _page_url, SARMIN_PARAMS
from app.crawlers.base import JobRef
from app.database import Base, RawHtml
from app.repository import save_raw_html
from urllib.parse import parse_qs, urlsplit


def test_jobkorea_stable_id_and_tracking_removed():
    html = '''<div class="list"><a href="/Recruit/GI_Read/50147033?Oem_Code=C1">공고 A</a>
       <a href="/Recruit/GI_Read/50147033?sc=416">공고 A</a>
       <a href="https://evil.example/Recruit/GI_Read/50147034">나쁜 링크</a></div>'''
    refs = listing_refs("jobkorea", html, "https://www.jobkorea.co.kr/theme/entry-level-internship")
    assert len(refs) == 1
    assert refs[0].external_id == "50147033"
    assert refs[0].url == "https://www.jobkorea.co.kr/Recruit/GI_Read/50147033"


def test_saramin_ids_and_original_query_filters():
    html = '''<a href="/zf_user/jobs/relay/view?rec_idx=55126586&view_type=public-recruit">데이터 엔지니어 채용</a>
    <a href="/zf_user/jobs/relay/view?rec_idx=55126586&view_type=more">same</a>'''
    refs = listing_refs("saramin", html, "https://www.saramin.co.kr/zf_user/jobs/public/list")
    assert len(refs) == 1
    assert refs[0].url.endswith("rec_idx=55126586")
    params = parse_qs(urlsplit(_page_url("saramin", 3)).query)
    assert params["page"] == ["3"]
    assert params["cat_mcls"] == ["2"]
    assert params["exp_cd"] == ["1,2"]
    assert params["page_count"] == ["100"]


def test_inthiswork_editorial_ignored_and_structured_detail():
    html = '''<section><article><h3><a href="/archives/404812">비모소프트｜백엔드 개발자 (전환형인턴)</a></h3></article>
      <article><h3><a href="/archives/221333">개발자 취업 절벽</a></h3><div class="category">취업토크</div></article>
      <article><h3><a href="/archives/404812">비모소프트｜백엔드 개발자 (전환형인턴)</a></h3></article></section>'''
    refs = listing_refs("inthiswork", html, "https://inthiswork.com/it")
    assert len(refs) == 1
    ref = refs[0]
    detail = '''<article><h1 class="entry-title">비모소프트｜백엔드 개발자 (전환형인턴)</h1>
      <time datetime="2026-10-12T12:00:00+09:00"></time>
      <div class="entry-content"><p>백엔드 서비스의 서버 개발을 맡을 인턴 개발자를 모집합니다.
      기술 스택: Python, FastAPI, PostgreSQL, Docker</p><p>지원 마감: 2026.10.30</p>
      <a href="https://jobs.company.example/jobs/22">지원하기</a></div></article>'''
    draft = detail_draft("inthiswork", ref, detail)
    assert draft.organization == "비모소프트"
    assert draft.employment_type == "인턴"
    assert draft.deadline_on == date(2026, 10, 30)
    assert draft.posted_on == date(2026, 10, 12)
    assert draft.apply_url == "https://jobs.company.example/jobs/22"
    assert len(draft.body_text) > 50


def test_saramin_blank_js_shell_not_saved():
    ref = JobRef("55126586", "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=55126586")
    try:
        detail_draft("saramin", ref, "<html><title>사람인</title><header>로그인</header></html>")
    except ValueError as exc:
        assert "detail layout" in str(exc)
    else:
        assert False, "Must not store fake posting for an empty JS shell"


def test_gzip_raw_html_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'html.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        save_raw_html(db, "inthiswork", "404812", "<h1>test</h1>")
        save_raw_html(db, "inthiswork", "404812", "<h1>test</h1>")
        db.commit()
    with factory() as db:
        results = db.scalars(select(RawHtml)).all()
        assert len(results) == 1
        assert gzip.decompress(results[0].html_gzip).decode() == "<h1>test</h1>"
    engine.dispose()


def test_cross_site_duplicates_only_specific_apply_url():
    from types import SimpleNamespace
    from app.duplicates import canonical_apply_url, duplicate_candidates
    assert canonical_apply_url("https://company.example/") is None
    jobs = [
        SimpleNamespace(id=1, source_id="inthiswork", title="백엔드 개발", original_url="https://inthiswork.com/a",
                        apply_url="https://company.example/careers/129?utm_source=inthiswork"),
        SimpleNamespace(id=2, source_id="saramin", title="서버 개발자", original_url="https://saramin.co.kr/b",
                        apply_url="https://company.example/careers/129?utm_source=saramin"),
        SimpleNamespace(id=3, source_id="jobkorea", title="다른 포지션", original_url="https://jobkorea.co.kr/c",
                        apply_url="https://company.example/careers/130"),
    ]
    groups = duplicate_candidates(jobs)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert {j["id"] for j in groups[0]["jobs"]} == {1, 2}
