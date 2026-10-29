"""Common normalized model shared by all site adapters."""
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)


class JobDraft(BaseModel):
    source_id: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    original_url: str = Field(min_length=1)
    organization: str | None = None
    recruitment_type: str | None = None
    employment_type: str | None = None
    region: str | None = None
    posted_on: date | None = None
    deadline_on: date | None = None
    deadline_text: str | None = None
    body_text: str | None = None
    apply_url: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    source_status: Literal["open", "closed", "unknown"] = "unknown"

    model_config = ConfigDict(str_strip_whitespace=True)


class JobOut(JobDraft):
    id: int
    first_seen_at: datetime
    last_seen_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
