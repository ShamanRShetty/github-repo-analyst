import httpx
import os
from datetime import datetime, timedelta, timezone

GITHUB_BASE = "https://api.github.com"

def _headers():
    token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

# Add follow_redirects=True to every client — this fixes the 301 error
async def get_repo_info(repo: str) -> dict:
    """MCP Tool: Fetch basic repo metadata."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(f"{GITHUB_BASE}/repos/{repo}", headers=_headers())
        r.raise_for_status()
        data = r.json()
        return {
            "name": data["full_name"],
            "description": data.get("description", ""),
            "stars": data["stargazers_count"],
            "forks": data["forks_count"],
            "language": data.get("language", "Unknown"),
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
        }

async def get_open_issues(repo: str) -> dict:
    """MCP Tool: Fetch open issues (excludes PRs)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/issues",
            headers=_headers(),
            params={"state": "open", "per_page": 100}
        )
        r.raise_for_status()
        issues = [i for i in r.json() if "pull_request" not in i]
        stale = [i for i in issues if datetime.fromisoformat(
            i["created_at"].replace("Z", "+00:00")) < cutoff]
        return {
            "total_open": len(issues),
            "stale_count": len(stale),
            "sample_titles": [i["title"] for i in issues[:5]]
        }

async def get_open_prs(repo: str) -> dict:
    """MCP Tool: Fetch open pull requests."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "per_page": 100}
        )
        r.raise_for_status()
        prs = r.json()
        return {
            "total_open": len(prs),
            "sample_titles": [p["title"] for p in prs[:5]],
            "draft_count": sum(1 for p in prs if p.get("draft"))
        }

async def get_recent_commits(repo: str) -> dict:
    """MCP Tool: Fetch commit activity for last 30 days."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/commits",
            headers=_headers(),
            params={"since": since, "per_page": 100}
        )
        r.raise_for_status()
        commits = r.json()
        authors = {}
        for c in commits:
            author = c.get("commit", {}).get("author", {}).get("name", "Unknown")
            authors[author] = authors.get(author, 0) + 1
        top = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "commit_count_30d": len(commits),
            "top_contributors": [name for name, _ in top]
        }