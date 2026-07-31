# Profile skill recommendation engine

This package analyses profile evidence already imported into the database and returns two separate
lists:

- `existing_skills`: every `account_skills` value, assigned a 1–5 weight; and
- `recommended_skills`: evidence-backed additions the freelancer may choose to add.

It does not write to the database. That keeps an LLM suggestion from automatically changing the
marketplace profile or inflating job-match scores. The caller should present and persist only the
skills the user accepts.

## Input contract

```python
from app.recommendation_engine import ProfileEvidence, recommend_profile_skills

evidence = ProfileEvidence(
    summary="I build production React and Node.js applications.",
    account_skills=["React.js", "JavaScript"],
    portfolio=[{"title": "Inventory API", "description": "FastAPI and PostgreSQL service"}],
    reviews=[{"rating": 5, "feedback": "Excellent API integration and database design."}],
    experience=[{"title": "Backend Developer", "description": "Designed REST APIs in Python."}],
)
result = await recommend_profile_skills(evidence)
```

The current application stores `summary` and `account_skills` on `PlatformConnection`, and
`portfolio` and `experience` on `FreelancerProfile`. Use
`ProfileEvidence.from_database_models(profile, connection, reviews=...)` to build the input.
The supplied `reviews` must be raw marketplace review records; `PlatformConnection.total_reviews`
is only a count and is not enough evidence to infer a skill.

## LLM contract

The full system prompt and JSON schema are in `prompt.py`. The prompt sends the exact values of
all five input fields in a `<profile_evidence>` JSON block and tells the model that those values are
untrusted data. It requires every current skill to be returned and requires new suggestions to cite
summary, portfolio, review, or experience evidence.

The engine uses the existing `LLM_PROVIDER` configuration (`gemini`, `anthropic`, or `nvidia`) and
requests schema-constrained JSON. It also validates the result locally: existing skills cannot be
dropped, weights are clamped to 1–5, duplicates are removed, and unsupported recommendations are
discarded.
