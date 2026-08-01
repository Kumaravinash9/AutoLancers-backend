"""Types at the recommendation engine boundary.

``ProfileEvidence`` is intentionally storage-agnostic.  Marketplace imports often differ in
shape, and a review table can be introduced without changing the LLM contract.  The database
adapter at the bottom bridges the fields that already exist in this application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from app.db.models import FreelancerProfile, PlatformConnection


EvidenceEntry = str | Mapping[str, Any]


@dataclass(frozen=True)
class ProfileEvidence:
    """The marketplace evidence that may be used to recommend profile skills.

    ``reviews`` expects the review records themselves (for example feedback text, rating, title,
    and date), rather than merely a review count.  Every supplied item is passed to the LLM as
    data.  Callers should remove data they are not permitted to send to their chosen provider.
    """

    summary: str = ""
    account_skills: Sequence[str] = field(default_factory=tuple)
    portfolio: Sequence[EvidenceEntry] = field(default_factory=tuple)
    reviews: Sequence[EvidenceEntry] = field(default_factory=tuple)
    experience: Sequence[EvidenceEntry] = field(default_factory=tuple)

    def prompt_data(self) -> dict[str, object]:
        """Return a JSON-ready representation without inventing or flattening evidence."""
        return {
            "summary": self.summary.strip(),
            "account_skills": _unique_skill_names(self.account_skills),
            "portfolio": list(self.portfolio),
            "reviews": list(self.reviews),
            "experience": list(self.experience),
        }

    @classmethod
    def from_database_models(
        cls,
        profile: FreelancerProfile,
        connection: PlatformConnection,
        *,
        reviews: Sequence[EvidenceEntry] = (),
    ) -> ProfileEvidence:
        """Adapt the application models plus externally stored marketplace review records.

        The current ``PlatformConnection`` row has an aggregate ``total_reviews`` but no raw
        review records.  Supplying the latter here keeps the engine ready for the review model or
        importer that owns them, rather than treating a count as evidence of a skill.
        """
        return cls(
            summary=connection.summary or "",
            account_skills=connection.account_skills or [],
            portfolio=profile.portfolio or [],
            reviews=reviews,
            experience=profile.experience or [],
        )


@dataclass(frozen=True)
class WeightedSkill:
    """One weighted skill and the profile sections that support it."""

    name: str
    weight: int  # 1 (peripheral) through 5 (core strength)
    evidence_sources: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "weight": self.weight,
            "evidence_sources": list(self.evidence_sources),
        }


@dataclass(frozen=True)
class ProfileSkillRecommendation:
    """LLM analysis separated into already-listed and user-actionable skills."""

    existing_skills: tuple[WeightedSkill, ...]
    recommended_skills: tuple[WeightedSkill, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "existing_skills": [skill.as_dict() for skill in self.existing_skills],
            "recommended_skills": [skill.as_dict() for skill in self.recommended_skills],
        }


def _unique_skill_names(skills: Sequence[str]) -> list[str]:
    """Trim and de-duplicate account skill names without changing their displayed spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for skill in skills:
        name = str(skill or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out
