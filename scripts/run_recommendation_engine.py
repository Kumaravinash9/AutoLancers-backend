#!/usr/bin/env python
"""Run the profile-skill recommendation engine from a JSON evidence file.

    uv run python scripts/run_recommendation_engine.py
    uv run python scripts/run_recommendation_engine.py --input path/to/profile.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "app/recommendation_engine/demo_profile.json"

# Make the command work even when it is launched outside the repository root.  Both the package
# import and Settings' relative `.env` path then resolve to this project.
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from app.recommendation_engine import (  # noqa: E402
    ProfileEvidence,
    RecommendationEngineError,
    recommend_profile_skills,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Weight profile skills and suggest evidence-backed skills to add."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Profile evidence JSON file (default: {DEFAULT_INPUT.relative_to(PROJECT_ROOT)})",
    )
    return parser.parse_args()


def _load_evidence(path: Path) -> ProfileEvidence:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object with profile evidence")
    try:
        return ProfileEvidence(**payload)
    except TypeError as exc:
        raise ValueError(f"{path} has unsupported profile evidence fields: {exc}") from exc


def main() -> int:
    args = _arguments()
    try:
        evidence = _load_evidence(args.input)
        result = asyncio.run(recommend_profile_skills(evidence))
    except (RecommendationEngineError, ValueError) as exc:
        print(f"Recommendation engine failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
