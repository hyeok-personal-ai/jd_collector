import asyncio
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from app.crawlers.demo import DemoCrawler
from app.crawlers.base import JobRef
from app.database import Base, Job, ListingState, NormalizationCache, RawPosting
from app.normalization import MissingFields, clean_draft, normalize_draft
from app.runner import run_crawler
from app.schema import JobDraft


class CountingDemo(DemoCrawler):
    def __init__(self):
        self.parse_count = 0
        self.revision = "one"

    async def discover(self, fetcher):
        yield JobRef("1001", "https://example.org/jobs/1001", self.revision)
        yield JobRef("1001", "https://example.org/jobs/1001", self.revision)  # Duplicate on same page

    async def parse(self, fetcher, ref):
        self.parse_count += 1
        return await super().parse(fetcher, ref)


def test_no_repeated_detail_fetch_and_revision_refresh(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'incremental.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    crawler = CountingDemo()

    async def exercise():
        first = await run_crawler(crawler, session_factory=factory)
        second = await run_crawler(crawler, session_factory=factory)
        assert first.created_count == 1
        assert second.created_count == 0
        assert crawler.parse_count == 1
        assert second.seen_count == 1
        crawler.revision = "two"
        third = await run_crawler(crawler, session_factory=factory)
        assert third.created_count == third.updated_count == 0
        assert crawler.parse_count == 2
        with factory() as session:
            assert session.scalar(select(ListingState)).listing_fingerprint == "two"
            assert len(session.scalars(select(Job)).all()) == 1
            assert len(session.scalars(select(RawPosting)).all()) == 1
    asyncio.run(exercise())
    engine.dispose()


def test_rule_normalization_and_memoized_local_llm(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'norm.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    raw = JobDraft(
        source_id="source", external_id="abc", title="  개발자   채용 ",
        original_url="https://example.org/jobs/abc", employment_type="기간제",
        body_text="서울특별시 소재 A기관에서 정규직 데이터 분석가를 모집합니다. 지원은 온라인으로 진행됩니다.",
    )
    calls = []

    async def fake_extract(draft, model):
        calls.append(model)
        return MissingFields(organization="A기관", region="서울", recruitment_type="채용")

    async def exercise():
        first = await normalize_draft(raw, factory, llm_model="fake:4b", extractor=fake_extract)
        second = await normalize_draft(raw, factory, llm_model="fake:4b", extractor=fake_extract)
        assert first == second
        assert first.title == "개발자 채용"
        assert first.employment_type == "계약직"  # Rule-based value, never overwritten by LLM.
        assert first.organization == "A기관"
        assert len(calls) == 1
        # Same syndicated text on another site uses the same LLM memoization key.
        syndicated = raw.model_copy(update={"source_id": "other", "external_id": "def", "original_url": "https://other.example/jobs/def"})
        third = await normalize_draft(syndicated, factory, llm_model="fake:4b", extractor=fake_extract)
        assert third.organization == first.organization
        assert len(calls) == 1
        with factory() as session:
            assert len(session.scalars(select(NormalizationCache)).all()) == 1
    asyncio.run(exercise())
    engine.dispose()
