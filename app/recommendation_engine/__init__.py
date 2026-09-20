"""Evidence-grounded skill recommendations for marketplace profiles.

The engine deliberately has no database writes.  It analyses the supplied profile evidence and
returns existing and suggested skill weights; the caller decides whether and where to persist the
user's accepted suggestions.
"""

from app.recommendation_engine.engine import (
    RecommendationEngineError,
    recommend_profile_skills,
)
from app.recommendation_engine.models import (
    ProfileEvidence,
    ProfileSkillRecommendation,
    WeightedSkill,
)

__all__ = [
    "ProfileEvidence",
    "ProfileSkillRecommendation",
    "RecommendationEngineError",
    "WeightedSkill",
    "recommend_profile_skills",
]
