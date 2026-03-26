from pydantic import BaseModel
from typing import Optional

class AnalyzeRequest(BaseModel):
    repo: str                        # e.g. "owner/repo-name"
    focus: Optional[str] = "general" # "issues" | "prs" | "activity" | "general"

class RepoHealth(BaseModel):
    repo: str
    health_score: int                # 0–100
    summary: str
    open_issues: int
    open_prs: int
    stale_issues: int                # older than 30 days
    recent_commits: int              # last 30 days
    top_contributors: list[str]
    insights: list[str]
    recommendations: list[str]