from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    external_id: str
    title: str
    description: str
    url: str
    skills_listed: list[str]
    budget_type: str | None
    budget_min: float | None
    budget_max: float | None
    currency: str | None
    bid_count: int | None
    posted_at: dt.datetime | None
    score: float
    reasons: list[dict[str, Any]]
    rejected: bool
    rejection_reason: str | None
    proposal_text: str | None
    status: str
    first_seen_at: dt.datetime


class JobPatch(BaseModel):
    proposal_text: str | None = None
    status: str | None = Field(default=None, pattern="^(new|drafted|approved|dismissed)$")


class SkillIn(BaseModel):
    name: str
    weight: float = 1.0


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str
    headline: str
    skills: list[dict[str, Any]]
    keywords_include: list[str]
    keywords_exclude: list[str]
    fixed_project_min: float
    hourly_min: float
    currency: str
    max_existing_bids: int
    min_match_score: float
    weight_skills: float
    weight_budget: float
    weight_competition: float
    weight_recency: float
    proposal_notes: str


class ProfileIn(BaseModel):
    display_name: str = ""
    headline: str = ""
    skills: list[SkillIn] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    fixed_project_min: float = 0.0
    hourly_min: float = 0.0
    currency: str = "USD"
    max_existing_bids: int = 25
    min_match_score: float = 55.0
    weight_skills: float = 60.0
    weight_budget: float = 20.0
    weight_competition: float = 10.0
    weight_recency: float = 10.0
    proposal_notes: str = ""


class AuthStatus(BaseModel):
    connected: bool
    platform: str = "freelancer"
    scope: str | None = None
    expires_at: dt.datetime | None = None
    detail: str | None = None
