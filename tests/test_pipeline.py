import asyncio
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.crawlers.demo import DemoCrawler
from app.database import Base, Job, get_session
from app.repository import upsert_job
from app.api import app


def test_demo_parser_and_upsert(tmp_path: Path):
    demo = DemoCrawler()

    async def gather():
        refs = [r async for r in demo.discover(None)]
        drafts = [await demo.parse(None, r) for r in refs]
        return refs, drafts

    refs, drafts = asyncio.run(gather())
    assert len(refs) == 3
    assert drafts[0].title == "백엔드 개발자 채용"
    assert drafts[0].attachments[0].name == "공고 안내문"
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(test_engine)
    with Session(test_engine) as s:
        assert upsert_job(s, drafts[0]) == "created"
        s.commit()
        assert upsert_job(s, drafts[0]) == "unchanged"
        s.commit()
        changed = drafts[0].model_copy(update={"title": "수정된 공고"})
        assert upsert_job(s, changed) == "updated"
        s.commit()
        assert len(s.scalars(select(Job)).all()) == 1
        assert s.scalar(select(Job)).title == "수정된 공고"
    test_engine.dispose()


def test_search_api(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    async def get_draft():
        crawler = DemoCrawler()
        ref = [r async for r in crawler.discover(None)][0]
        return await crawler.parse(None, ref)

    with Session(engine) as session:
        upsert_job(session, asyncio.run(get_draft()))
        session.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            response = client.get("/api/jobs", params={"keyword": "백엔드", "region": "서울"})
            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == 1
            assert payload["items"][0]["external_id"] == "1001"
            assert client.get("/api/jobs?region=부산").json()["total"] == 0
            assert client.get("/api/jobs/9999").status_code == 404
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
