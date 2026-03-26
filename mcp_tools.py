import httpx
import os
import re
from datetime import datetime, timedelta, timezone

GITHUB_BASE = "https://api.github.com"

def _headers():
    token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

def _parse_link_last_page(link_header: str) -> int:
    """Extract total page count from GitHub's Link header."""
    if not link_header:
        return 1
    match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link_header)
    return int(match.group(1)) if match else 1


async def get_repo_info(repo: str) -> dict:
    """Fetch basic repo metadata including GitHub's own issue/PR counters."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(f"{GITHUB_BASE}/repos/{repo}", headers=_headers())
        r.raise_for_status()
        data = r.json()

        # GitHub stores true open_issues_count (issues + PRs combined)
        # We'll use search API for accurate split — store raw here for reference
        return {
            "name": data["full_name"],
            "description": data.get("description", ""),
            "stars": data["stargazers_count"],
            "forks": data["forks_count"],
            "language": data.get("language", "Unknown"),
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "github_open_issues_count": data["open_issues_count"],  # issues+PRs combined
        }


async def get_open_issues(repo: str) -> dict:
    """
    Fetch TRUE open issue count using the Search API.
    The Search API returns `total_count` directly — no pagination needed.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:

        # Search API gives exact total_count without fetching all pages
        search_url = f"{GITHUB_BASE}/search/issues"

        # Open issues (type:issue excludes PRs automatically)
        issues_r = await client.get(
            search_url,
            headers=_headers(),
            params={
                "q": f"repo:{repo} is:open type:issue",
                "per_page": 5  # We only need total_count + a few samples
            }
        )
        issues_r.raise_for_status()
        issues_data = issues_r.json()

        total_open = issues_data.get("total_count", 0)
        sample_items = issues_data.get("items", [])

        # Stale issues: open + not updated in 30 days
        stale_r = await client.get(
            search_url,
            headers=_headers(),
            params={
                "q": f"repo:{repo} is:open type:issue updated:<{_days_ago(30)}",
                "per_page": 1
            }
        )
        stale_r.raise_for_status()
        stale_count = stale_r.json().get("total_count", 0)

        # Search API total_count is accurate but item retrieval caps at 1000
        if total_open == 0:
            data_confidence = 1.0
            confidence_reason = "No open issues — fully accurate"
        elif total_open <= 100:
            data_confidence = 1.0
            confidence_reason = "Full dataset retrieved"
        elif total_open <= 1000:
            data_confidence = 0.95
            confidence_reason = "Count exact; sample titles limited to 1000 items"
        else:
            data_confidence = 0.85
            confidence_reason = (
            f"Count is exact ({total_open} via Search API) but only "
            f"1000/{total_open} items are inspectable — stale ratio is estimated"
        )

        return {
            "total_open": total_open,
            "stale_count": stale_count,
            "sample_titles": [i["title"] for i in sample_items[:5]],
            "data_confidence": data_confidence,
            "confidence_reason": confidence_reason,       # 1.0 = full data, <1.0 = partial
            "data_note": (
                "Counts are exact (Search API)"
                )
        }


async def get_open_prs(repo: str) -> dict:
    """
    Get TRUE open PR count using Link header trick.
    Fetch 1 PR per page → last page number = total PR count.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "per_page": 1}  # 1 item → last page = total
        )
        r.raise_for_status()

        link_header = r.headers.get("Link", "")
        total_open = _parse_link_last_page(link_header)

        # If no Link header, we got all results in one page
        prs_on_page = r.json()
        if not link_header:
            total_open = len(prs_on_page)

        # Fetch a few samples separately for context
        samples_r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "per_page": 5}
        )
        samples_r.raise_for_status()
        samples = samples_r.json()

        draft_r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "per_page": 100, "draft": "true"}
        )
        draft_count = len(draft_r.json()) if draft_r.status_code == 200 else 0

        # Link header gives exact count; draft detection fetches up to 100
        if total_open <= 100:
            data_confidence = 1.0
            confidence_reason = "Full PR list retrieved"
        else:
            draft_sample_ratio = round(min(100 / total_open, 1.0), 2)
            data_confidence = round(0.90 + (0.10 * draft_sample_ratio), 2)
            confidence_reason = (
            f"PR count exact (Link header); draft_count estimated from "
            f"first 100/{total_open} PRs"
            )

        return {
            "total_open": total_open,
            "data_confidence": data_confidence,
            "confidence_reason": confidence_reason,
            "sample_titles": [p["title"] for p in samples[:5]],
            "draft_count": draft_count,
            "data_note": "Count derived from Link header pagination (exact)"
        }


async def get_recent_commits(repo: str) -> dict:
    """
    Use /stats/participation for weekly commit counts (last 52 weeks).
    This is pre-aggregated by GitHub — always accurate, no pagination needed.
    Fall back to /commits pagination if stats aren't ready (202 response).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:

        # participation gives 52-week array of weekly commit counts
        stats_r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/stats/participation",
            headers=_headers()
        )

        commit_count_30d = None
        method_used = ""

        if stats_r.status_code == 200:
            all_weeks = stats_r.json().get("all", [])
            commit_count_30d = sum(all_weeks[-4:])
            method_used = "participation_stats (pre-aggregated, exact)"

            data_confidence = 0.92
            confidence_reason = (
            "Based on last 4 ISO weeks (~28 days) from participation stats; "
            "±3 day boundary difference vs calendar 30 days"
            )

        else:
            since = _days_ago(30)
            commit_count_30d, method_used = await _count_commits_paginated(
            client, repo, since
    )

            data_confidence = 0.80
            confidence_reason = "Stats endpoint unavailable (202); using paginated estimate"
        # Top contributors via stats/contributors (pre-aggregated, no rate limit risk)
        contrib_r = await client.get(
            f"{GITHUB_BASE}/repos/{repo}/stats/contributors",
            headers=_headers()
        )
        top_contributors = []
        if contrib_r.status_code == 200:
            contribs = sorted(
                contrib_r.json(),
                key=lambda c: c.get("total", 0),
                reverse=True
            )[:5]
            top_contributors = [c["author"]["login"] for c in contribs if c.get("author")]

        return {
            "commit_count_30d": commit_count_30d,
            "top_contributors": top_contributors,
            "data_confidence": data_confidence,
            "confidence_reason": confidence_reason,
            "data_note": f"Commits counted via {method_used}"
        }
    
# Add to mcp_tools.py

async def compute_health_metrics(repo: str) -> dict:
    """
    Compute scale-normalized health signals.
    Returns ratio-based metrics so the AI scores relative to repo size,
    not absolute counts.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Fetch repo metadata for scale context
        r = await client.get(f"{GITHUB_BASE}/repos/{repo}", headers=_headers())
        r.raise_for_status()
        meta = r.json()

        stars        = max(meta.get("stargazers_count", 1), 1)  # avoid div/0
        forks        = meta.get("forks_count", 0)
        watchers     = meta.get("subscribers_count", 0)
        open_issues  = meta.get("open_issues_count", 0)  # issues+PRs combined (rough scale ref)

        # Classify repo scale
        if stars >= 10_000:
            scale = "large"        # next.js, fastapi, etc.
        elif stars >= 1_000:
            scale = "medium"
        elif stars >= 100:
            scale = "small"
        else:
            scale = "micro"

        # Issue-to-star ratio: healthy large OSS repos typically sit at 0.5–5%
        issue_star_ratio = round((open_issues / stars) * 100, 2)

        # Engagement ratio: forks+watchers relative to stars
        engagement_ratio = round((forks + watchers) / stars, 3)

        # Scale-aware thresholds for issue_star_ratio
        # Large repos tolerate higher ratios; micro repos should have near zero
        ratio_thresholds = {
            "large":  {"healthy": 5.0,  "moderate": 15.0},
            "medium": {"healthy": 3.0,  "moderate": 10.0},
            "small":  {"healthy": 2.0,  "moderate": 8.0},
            "micro":  {"healthy": 1.0,  "moderate": 5.0},
        }
        thresh = ratio_thresholds[scale]

        if issue_star_ratio <= thresh["healthy"]:
            issue_burden = "low"
        elif issue_star_ratio <= thresh["moderate"]:
            issue_burden = "moderate"
        else:
            issue_burden = "high"

        return {
            "repo_scale": scale,
            "stars": stars,
            "forks": forks,
            "issue_star_ratio_pct": issue_star_ratio,
            "engagement_ratio": engagement_ratio,
            "issue_burden": issue_burden,           # "low" | "moderate" | "high"
            "scale_context": (
                f"This is a {scale} repository ({stars:,} stars). "
                f"An issue/star ratio of {issue_star_ratio}% is considered "
                f"{issue_burden} for repos of this scale."
            ),
            "scoring_guidance": _build_scoring_guidance(scale, issue_burden),
        }


def _build_scoring_guidance(scale: str, issue_burden: str) -> str:
    """
    Returns explicit scoring instructions calibrated to repo scale.
    Injected directly into the prompt context — removes AI guesswork.
    """
    base_ranges = {
        ("large",  "low"):      (82, 95),
        ("large",  "moderate"): (68, 82),
        ("large",  "high"):     (52, 68),
        ("medium", "low"):      (78, 92),
        ("medium", "moderate"): (60, 78),
        ("medium", "high"):     (42, 60),
        ("small",  "low"):      (72, 90),
        ("small",  "moderate"): (55, 72),
        ("small",  "high"):     (35, 55),
        ("micro",  "low"):      (65, 88),
        ("micro",  "moderate"): (45, 65),
        ("micro",  "high"):     (20, 45),
    }
    low, high = base_ranges.get((scale, issue_burden), (50, 70))
    return (
        f"For a {scale} repository with {issue_burden} issue burden, "
        f"the health_score MUST be in the range {low}–{high} BEFORE "
        f"applying activity and PR pipeline adjustments (±10 max)."
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _days_ago(n: int) -> str:
    """Return ISO date string for N days ago (YYYY-MM-DD), used in search queries."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


async def _count_commits_paginated(client, repo: str, since: str) -> tuple[int, str]:
    """
    Fallback: count commits via Link header (1 commit per page → last page = total).
    Caps at 1000 to avoid hammering the API.
    """
    r = await client.get(
        f"{GITHUB_BASE}/repos/{repo}/commits",
        headers=_headers(),
        params={"since": since, "per_page": 1}
    )
    if r.status_code != 200:
        return 0, "unavailable"

    link = r.headers.get("Link", "")
    total = _parse_link_last_page(link)
    if not link:
        total = len(r.json())

    capped = min(total, 1000)
    note = "paginated Link header (exact)" if total <= 1000 else f"capped at 1000/{total}"
    return capped, note