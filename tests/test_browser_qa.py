"""Opt-in browser QA. Run with RADAR_RUN_BROWSER_QA=1 and Playwright Chromium installed."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RADAR_RUN_BROWSER_QA") != "1", reason="opt-in browser QA")


def seed_and_serve(port):
    import numpy as np
    import uvicorn
    from datetime import timedelta
    from app.backfill import relabel_all
    from app.database import SessionLocal, init_db
    from app.deadline_policy import today_kst
    from app.experience import ExperienceStore, index_documents
    from app.repository import upsert_job
    from app.schema import JobDraft
    from app.vector_index import DIM, build_batch

    class Tokenizer:
        def encode(self, text, **kwargs):
            return type("Tokens", (), {"offsets": [(i, i + 1) for i in range(len(text))]})()

    class Embedding:
        def embed(self, chunks, **kwargs):
            for _ in chunks:
                vector = np.zeros(DIM, dtype="<f4")
                vector[0] = 1
                yield vector

    init_db()
    today = today_kst()
    with SessionLocal() as db:
        for i in range(25):
            tech = "Java Spring Boot SQL" if i < 22 else "Node.js NestJS TypeScript"
            title = "신입 백엔드 개발자 QA " + str(i).zfill(2)
            upsert_job(db, JobDraft(
                source_id="demo", external_id=f"qa-{i}", title=title, organization="QA 합성 회사",
                original_url=f"https://example.org/postings/{i}",
                apply_url=f"https://example.org/apply/{i}", source_status="closed" if i == 24 else "open",
                region="서울", employment_type="인턴" if i == 0 else "정규직", recruitment_type="채용",
                posted_on=today - timedelta(days=i), deadline_on=today + timedelta(days=i + 1),
                body_text=f"주요 업무\n서버 API 설계 및 개발\n자격요건\n{tech} 개발 경험\n우대사항\nDocker 배포 경험",
            ))
        db.commit()
    relabel_all()
    store = ExperienceStore()
    for variant, skill in (("일반형", "Python"), ("Node형", "Node.js"), ("Java형", "Java")):
        store.save(variant + " QA", "resume", variant, f"{skill} API를 구현했습니다. Docker로 배포하고 실패 경로를 테스트했습니다.")
    model, tokenizer = Embedding(), Tokenizer()
    index_documents(model, tokenizer, store, limit=3)
    build_batch(model, tokenizer, limit=25)
    uvicorn.run("app.api:app", host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture(scope="module")
def radar_url(tmp_path_factory):
    data = tmp_path_factory.mktemp("radar-browser")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "RADAR_WORKER": "0", "RADAR_DATA_DIR": str(data),
           "DATABASE_URL": "sqlite:///" + (data / "radar.db").as_posix(),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    log_path = data / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-X", "utf8", __file__, str(port)],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    pytest.fail(log_path.read_text(encoding="utf-8"))
                try:
                    with urlopen(url + "/api/health", timeout=0.5) as response:
                        if json.load(response)["ok"]:
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.25)
            else:
                pytest.fail("QA server did not become ready")
            yield url
        finally:
            # Uvicorn is the isolated test child. It never runs collectors or user data.
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(chromium, radar_url):
    context = chromium.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(8000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(radar_url)
    page.locator(".job-row").first.wait_for()
    try:
        yield page
        assert not errors
    finally:
        context.close()


def expect(locator):
    from playwright.sync_api import expect
    return expect(locator)


def open_position(page, button="#postingReport"):
    if button == "#postingReport":
        page.locator(".job-title").first.click()
    page.locator(button).click()
    page.frame_locator("#reportFrame").get_by_role("heading", name="회사·포지션 지원 리포트").wait_for()


def save(page, label, prefix):
    page.locator("#reportDownloads summary").click()
    with page.expect_download() as event:
        page.get_by_role("button", name=label, exact=True).click()
    file = event.value
    assert file.suggested_filename.startswith(prefix)
    return Path(file.path()).read_bytes()


def test_search_filters_pagination_bookmark_and_csv(page):
    expect(page.locator("#summary")).to_have_text("24건")
    page.locator("#next").click()
    expect(page.locator("#pages")).to_have_text("2 / 2")
    page.locator("#prev").click()
    page.locator("#openSkills").click()
    page.locator("#skillSearch").fill("Java")
    page.get_by_role("button", name="Java 선택", exact=True).click()
    page.locator("#closeSkills").click()
    expect(page.locator("#skillFilterSummary")).to_be_empty()
    page.locator("#openSkills").click()
    page.locator("#skillSearch").fill("Java")
    page.get_by_role("button", name="Java 선택", exact=True).click()
    page.locator("#applySkills").click()
    expect(page.locator("#summary")).to_have_text("22건")
    page.locator("#openAdvanced").click()
    page.get_by_label("고용 형태", exact=True).select_option("intern")
    page.locator("#doneAdvanced").click()
    expect(page.locator("#summary")).to_have_text("1건")
    page.get_by_role("button", name="인턴 해제", exact=True).click()
    expect(page.locator("#summary")).to_have_text("22건")
    page.locator(".bookmark-button").first.click()
    page.locator("#bookmarkedOnly").check()
    expect(page.locator("#summary")).to_have_text("1건")
    page.locator(".result-menu summary").first.click()
    with page.expect_download() as event:
        page.locator("#exportFiltered").click()
    assert "QA" in Path(event.value.path()).read_text(encoding="utf-8-sig")
    page.reload()
    expect(page.locator("#summary")).to_have_text("1건")


def test_detail_application_comparison_market_and_exports(page):
    page.locator(".job-title").first.click()
    page.locator("#applicationPanel summary").click()
    page.locator("#applicationStatus").select_option("planned")
    page.locator("#applicationNote").fill("QA 합성 지원 메모")
    page.locator("#saveApplication").click()
    expect(page.locator("#applicationMessage")).to_contain_text("저장")
    page.get_by_text("유사 공고·중복 확인", exact=True).click()
    page.locator("#findSimilar").click()
    expect(page.locator("#relatedPostings")).not_to_be_empty()
    page.locator("#findDuplicates").click()
    expect(page.locator("#relatedPostings")).not_to_contain_text("확인하고 있습니다")
    page.locator("#closeDetail").click()
    checks = page.locator(".job-select input")
    checks.nth(0).check()
    checks.nth(1).check()
    page.locator("#compareSelected").click()
    expect(page.locator("#comparisonResult")).to_contain_text("공통")
    open_position(page, "#comparisonReport")
    data = json.loads(save(page, "JSON", "radar-position"))
    assert len(data["items"]) == 2 and not data["items"][0]["personal_assessments"]
    page.locator("#closeReport").click()
    page.locator("#closeComparison").click()
    page.locator(".result-menu summary").first.click()
    page.locator("#openAnalysis").click()
    page.locator("#analyzeJobs").click()
    expect(page.locator("#analysisResult table")).to_be_visible()


def test_recommendations_and_position_personalization(page):
    page.locator("#openExperience").click()
    page.locator("#selectAllVariants").click()
    page.locator("#compareVariants").click()
    expect(page.locator("#recommendationStatus")).to_contain_text("24건 비교")
    expect(page.locator(".variant-stat")).to_have_count(3)
    page.locator("#recommendationReport").click()
    page.frame_locator("#reportFrame").get_by_role("heading", name="문서 유형별 지원 우선순위 비교").wait_for()
    assert save(page, "Markdown", "radar-variants").startswith(b"#")
    page.locator("#closeReport").click()
    page.locator("#closeExperience").click()
    page.get_by_label("키워드").fill("QA 00")
    page.get_by_role("button", name="검색", exact=True).click()
    expect(page.locator("#summary")).to_have_text("1건")
    page.locator("#openExperience").click()
    expect(page.locator("#recommendationResults")).to_be_empty()
    expect(page.locator("#recommendationReport")).to_be_disabled()
    page.locator("#recommendDocuments").click()
    expect(page.locator("#recommendationResults .recommendation-card")).to_have_count(1)
    page.locator("#closeExperience").click()
    open_position(page)
    page.get_by_label("개인 적합도").select_option(label="Java형")
    page.get_by_role("button", name="미리보기에 반영").click()
    expect(page.frame_locator("#reportFrame").get_by_text("Java형", exact=False).first).to_be_visible()
    assert save(page, "PDF", "radar-position").startswith(b"%PDF-")
    assert b"<html" in save(page, "HTML", "radar-position").lower()


def test_management_documents_and_mobile(page):
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#openSkills").click()
    expect(page.locator("#applySkills")).to_be_in_viewport()
    page.keyboard.press("Escape")
    page.locator("#openCollection").click()
    expect(page.locator("#collectionSettings")).to_be_visible()
    page.locator("#autoCollect").uncheck()
    page.get_by_role("button", name="설정 저장").click()
    expect(page.locator("#controlMessage")).to_contain_text("설정을 저장")
    page.locator("#collectNow").click()
    expect(page.locator("#controlMessage")).to_contain_text("대기열")
    page.locator("#stopQueue").click()
    expect(page.locator("#controlMessage")).to_contain_text("취소")
    page.locator("#backupNow").click()
    expect(page.locator("#controlMessage")).to_contain_text("백업 저장")
    page.locator("#closeCollection").click()
    page.locator("#openExperience").click()
    page.locator("#documentEditor summary").click()
    page.locator("#documentFile").set_input_files({
        "name": "qa-resume.txt", "mimeType": "text/plain",
        "buffer": "QA 문서입니다. Python으로 서버를 구현하고 배포했습니다.".encode(),
    })
    page.get_by_role("button", name="문서 저장", exact=True).click()
    expect(page.locator("#documentList")).to_contain_text("qa-resume.txt")
    card = page.locator(".document-card").filter(has_text="qa-resume.txt")
    page.once("dialog", lambda dialog: dialog.accept())
    card.get_by_role("button", name="제거").click()
    expect(card).to_have_count(0)


def test_source_catalog_and_empty_or_failed_search(page):
    page.get_by_text("출처별 수집 현황 · 기업 목록", exact=True).click()
    page.locator("#targetFilter").fill("카카오")
    expect(page.locator("#companies")).to_contain_text("카카오")
    page.get_by_text("유사도 상태 · 중복 관리", exact=True).click()
    page.locator("#viewDuplicateGroups").click()
    expect(page.locator("#reviewedGroups")).to_contain_text("아직 묶은 공고가 없습니다")
    page.get_by_label("키워드").fill("없는합성검색어")
    page.get_by_role("button", name="검색", exact=True).click()
    expect(page.get_by_role("heading", name="검색 결과 없음")).to_be_visible()
    page.locator(".empty-state").get_by_role("button", name="수집 관리").click()
    expect(page.locator("#collectionSettings")).to_be_visible()
    page.locator("#closeCollection").click()
    page.route("**/api/jobs?*", lambda route: route.fulfill(status=503, body="synthetic"))
    page.locator("#resetFilters").click()
    expect(page.locator("#error")).to_contain_text("503")
    page.unroute("**/api/jobs?*")
    page.locator("#resetFilters").click()
    expect(page.locator("#summary")).to_have_text("24건")


if __name__ == "__main__":
    seed_and_serve(int(sys.argv[1]))
