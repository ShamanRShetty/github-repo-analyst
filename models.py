from pydantic import BaseModel, Field, field_validator
from typing import Optional

class AnalyzeRequest(BaseModel):
    repo: str
    focus: Optional[str] = "general"

class DataQuality(BaseModel):
    issues_confidence: float        # 0.0–1.0
    prs_confidence: float
    commits_confidence: float
    overall_confidence: float       # mean of above
    notes: list[str]                # human-readable caveats

class RepoHealth(BaseModel):
    repo: str
    health_score: int = Field(ge=0, le=100)  # ✅ constraint added
    summary: str
    open_issues: int
    open_prs: int
    stale_issues: int
    recent_commits: int
    top_contributors: list[str]
    insights: list[str]
    recommendations: list[str]
    data_quality: Optional[DataQuality] = None

    # Guard: reject invalid AI output
    @field_validator("health_score")
    @classmethod
    def score_must_be_plausible(cls, v):
        if not (0 <= v <= 100):
            raise ValueError(f"health_score {v} is out of bounds")
        return v