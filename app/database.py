"""MVP uses synchronous SQLAlchemy; easy SQLite setup, PostgreSQL compatible."""
from datetime import date, datetime, timezone
from collections.abc import Iterator
from sqlalchemy import Date, DateTime, Index, JSON, String, Text, LargeBinary, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from .config import DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_deadline", "deadline_on"),
        Index("ix_jobs_posted", "posted_on"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    original_url: Mapped[str] = mapped_column(Text)
    organization: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recruitment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(150), nullable=True)
    posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    source_status: Mapped[str] = mapped_column(String(20), default="unknown")
    content_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawPosting(Base):
    """Save parsed source payload separately so model upgrades need no re-crawl."""
    __tablename__ = "raw_postings"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_raw_posting"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawHtml(Base):
    """Latest compressed detail HTML to re-parse offline after parser upgrades."""
    __tablename__ = "raw_html"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_raw_html"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(300))
    html_gzip: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ListingState(Base):
    """Persistent per-source guard against repeatedly opening detail pages."""
    __tablename__ = "listing_states"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_listing_state"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(300))
    listing_fingerprint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_detail_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ListingPage(Base):
    """Persistent HTTP validators for *listing* pages (no response body kept)."""
    __tablename__ = "listing_pages"
    __table_args__ = (UniqueConstraint("source_id", "url", name="uq_listing_page"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NormalizationCache(Base):
    """Deduplicate calls to the local model for identical model/prompt/content."""
    __tablename__ = "normalization_cache"
    input_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiRequestUsage(Base):
    """Persisted daily API request count in Asia/Seoul local calendar days."""
    __tablename__ = "api_request_usage"
    __table_args__ = (UniqueConstraint("source_id", "usage_day", name="uq_api_usage_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    usage_day: Mapped[str] = mapped_column(String(10))
    call_count: Mapped[int] = mapped_column(default=0)




class PublicFeedCache(Base):
    """Public list JSON retained so HTTP 304 need not trigger a second fetch.

    Only used for published public listings, never credentialed endpoints.
    """
    __tablename__ = 'public_feed_cache'
    __table_args__ = (UniqueConstraint('source_id','url',name='uq_public_feed_cache'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | list] = mapped_column(JSON)
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_full_fetch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow)

class JobClassification(Base):
    """Separate derived facts from original Job so old SQLite DB needs no ALTER."""
    __tablename__ = "job_classifications"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_job_classification"),
                      Index("ix_classification_filters", "it_match", "career"))
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(300))
    it_match: Mapped[str] = mapped_column(String(12))
    career: Mapped[str] = mapped_column(String(20))
    min_years: Mapped[int | None] = mapped_column(nullable=True)
    max_years: Mapped[int | None] = mapped_column(nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PostingPresence(Base):
    """Per-board consecutive absence counts, populated only after successful item checks."""
    __tablename__ = "posting_presence"
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_posting_presence"),
                      Index("ix_posting_presence_scope", "source_id", "scope_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(300))
    scope_id: Mapped[str] = mapped_column(String(300))
    missing_scans: Mapped[int] = mapped_column(default=0)
    closed_by_absence: Mapped[bool] = mapped_column(default=False)
    last_present_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    seen_count: Mapped[int] = mapped_column(default=0)
    created_count: Mapped[int] = mapped_column(default=0)
    updated_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
if engine.dialect.name == 'sqlite':
    @event.listens_for(engine, 'connect')
    def configure_sqlite(connection, _record):
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA journal_mode=WAL')
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    # Version 1 -> 2 is additive: metrics and an explicit schema revision table.
    # Future destructive migrations must be performed before advancing revision.
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        revision = session.get(SchemaRevision, 1)
        if revision is None:
            session.add(SchemaRevision(id=1, version=2))
        elif revision.version > 2:
            raise RuntimeError('Database was created by a newer Recruit Radar version')
        else:
            revision.version = 2
        session.commit()


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


class SourceVerification(Base):
    """Read-only verification histories. Latest-status views are computed by source_key.

    A new table keeps existing SQLite installs compatible (no ALTER TABLE).
    No credentials, raw HTML or personal applicant data are persisted here.
    """
    __tablename__ = 'source_verifications'
    __table_args__ = (Index('ix_verification_source_time','source_key','checked_at'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(240), index=True)
    family: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    discovered: Mapped[int] = mapped_column(default=0)
    sample_attempted: Mapped[int] = mapped_column(default=0)
    sample_parsed: Mapped[int] = mapped_column(default=0)
    report: Mapped[dict] = mapped_column(JSON)


class SchemaRevision(Base):
    __tablename__ = 'schema_revision'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]


class HttpMetric(Base):
    __tablename__ = 'http_metrics'
    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    status: Mapped[int]
    elapsed_ms: Mapped[float]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
