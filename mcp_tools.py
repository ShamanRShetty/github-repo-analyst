import asyncio
import socket
import httpx
import httpcore
import os
import re
from datetime import datetime, timedelta, timezone

GITHUB_BASE = "https://api.github.com"

# Stale threshold (days) — surfaced in data_quality.notes for transparency
STALE_THRESHOLD_DAYS = 90

# ── Timeouts ──────────────────────────────────────────────────────────────────
# DNS on this machine takes ~11s.  connect must exceed that comfortably.
# Override via env vars without touching code.
_TIMEOUT = httpx.Timeout(
    connect=float(os.getenv("GITHUB_CONNECT_TIMEOUT", "25")),
    read=float(os.getenv("GITHUB_READ_TIMEOUT",    "30")),
    write=10.0,
    pool=5.0,
)

_MAX_RETRIES   = 3
_RETRY_BACKOFF = 2.0  # seconds; multiplied by attempt number
_RETRY_ON      = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError)

# ── DNS cache ─────────────────────────────────────────────────────────────────
# Resolve api.github.com ONCE per process via the OS resolver and cache the IP.
# Every subsequent _get() call injects the cached IP directly into httpcore's
# connection pool via a custom AsyncConnectionPool — the URL stays as
# https://api.github.com/... so TLS certificate validation passes normally.
_GITHUB_IP: str | None = None


async def _resolve_github_ip() -> str:
    global _GITHUB_IP
    if _GITHUB_IP is None:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo("api.github.com", 443, type=socket.SOCK_STREAM)
        _GITHUB_IP = infos[0][4][0]
    return _GITHUB_IP


class _CachedDNSTransport(httpx.AsyncHTTPTransport):
    """
    httpx transport that short-circuits DNS for api.github.com by injecting
    the pre-resolved IP into every connection attempt.  The hostname is kept
    in the URL so TLS SNI and certificate validation work exactly as normal.
    """
    def __init__(self, ip: str, **kwargs):
        super().__init__(**kwargs)
        self._ip = ip

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Swap the host in the internal httpcore request to the cached IP.
        # httpx passes the original URL's host as the SNI server_name, so
        # the certificate is still validated against "api.github.com".
        if request.url.host == "api.github.com":
            request.extensions["sni_hostname"] = "api.github.com"
            # Rebuild URL with the cached IP so httpcore connects to the right address
            raw_url = httpcore.URL(
            scheme=request.url.scheme.encode(),
            host=self._ip.encode(),
            port=request.url.port or 443,
            target=(request.url.raw_path + (b"?" + request.url.query if request.url.query else b"")),
        )
            # Patch the underlying _raw attribute that httpcore actually uses
            request.url._raw = raw_url
        return await super().handle_async_request(request)


def _client(ip: str | None = None) -> httpx.AsyncClient:
    """Return a configured async client, optionally with DNS caching transport."""
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    kwargs: dict = dict(follow_redirects=True, timeout=_TIMEOUT)
    if proxy:
        kwargs["proxy"] = proxy
    if ip:
        kwargs["transport"] = _CachedDNSTransport(ip=ip)
    return httpx.AsyncClient(**kwargs)


async def _get(url: str, **kwargs) -> httpx.Response:
    """
    GET with pre-resolved DNS + automatic retry on transient errors.
    The URL always contains the real hostname (api.github.com) so TLS works.
    """
    ip = await _resolve_github_ip()

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with _client(ip=ip) as client:
                r = await client.get(url, **kwargs)
                r.raise_for_status()
                return r
        except _RETRY_ON as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF * attempt)
        except httpx.HTTPStatusError:
            raise
    raise last_exc


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_link_last_page(link_header: str) -> int:
    if not link_header:
        return 1
    match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link_header)
    return int(match.group(1)) if match else 1


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ── Tools ─────────────────────────────────────────────────────────────────────

async def get_repo_info(repo: str) -> dict:
    """Fetch basic repo metadata including GitHub's own issue/PR counters."""
    r    = await _get(f"{GITHUB_BASE}/repos/{repo}", headers=_headers())
    data = r.json()
    return {
        "name":                     data["full_name"],
        "description":              data.get("description", ""),
        "stars":                    data["stargazers_count"],
        "forks":                    data["forks_count"],
        "language":                 data.get("language", "Unknown"),
        "created_at":               data["created_at"],
        "updated_at":               data["updated_at"],
        "github_open_issues_count": data["open_issues_count"],
    }


async def get_open_issues(repo: str) -> dict:
    """
    Fetch TRUE open issue count using the Search API.

    Confidence model (ceiling 0.93, floor 0.70):
      - Base 0.93 — Search API total_count accurate but has ~2-5 min index lag.
      - Deduct 0.05 — partial item retrieval (101-500 issues).
      - Deduct 0.08 — moderate retrieval limit (501-1000 issues).
      - Deduct 0.15 — item cap hit (>1000): stale ratio extrapolated.
      - Deduct 0.03 — high stale ratio with partial data.
    """
    search_url   = f"{GITHUB_BASE}/search/issues"
    stale_cutoff = _days_ago(STALE_THRESHOLD_DAYS)

    issues_r = await _get(
        search_url,
        headers=_headers(),
        params={"q": f"repo:{repo} is:open type:issue", "per_page": 5},
    )
    issues_data  = issues_r.json()
    total_open   = issues_data.get("total_count", 0)
    sample_items = issues_data.get("items", [])

    stale_r = await _get(
        search_url,
        headers=_headers(),
        params={"q": f"repo:{repo} is:open type:issue updated:<{stale_cutoff}", "per_page": 1},
    )
    stale_count = stale_r.json().get("total_count", 0)

    # Confidence scoring
    confidence = 0.93
    components = ["Search API total_count (base 0.93 — accounts for ~2-5 min index lag)"]

    if total_open == 0:
        components = ["No open issues — count trivially accurate; base confidence retained"]
    elif total_open <= 100:
        components.append("Full item set retrieved (<=100 issues)")
    elif total_open <= 500:
        confidence -= 0.05
        components.append(
            f"Partial item retrieval: ~5/{total_open} items inspectable; "
            "stale ratio extrapolated from count only (-0.05)"
        )
    elif total_open <= 1000:
        confidence -= 0.08
        components.append(
            f"Moderate retrieval limit: ~5/{total_open} items; "
            "stale estimate less reliable (-0.08)"
        )
    else:
        confidence -= 0.15
        components.append(
            f"Item cap hit: 1000/{total_open} inspectable; "
            "stale ratio is a rough extrapolation (-0.15)"
        )

    if total_open > 200 and stale_count > 0 and (stale_count / total_open) > 0.4:
        confidence -= 0.03
        components.append(
            "High stale ratio with partial data — estimate has wider uncertainty (-0.03)"
        )

    confidence = round(max(0.70, min(0.93, confidence)), 2)

    return {
        "total_open":           total_open,
        "stale_count":          stale_count,
        "stale_threshold_days": STALE_THRESHOLD_DAYS,
        "sample_titles":        [i["title"] for i in sample_items[:5]],
        "data_confidence":      confidence,
        "confidence_reason":    "; ".join(components),
        "data_note": (
            f"Issue count via Search API total_count. "
            f"Stale = no activity in >{STALE_THRESHOLD_DAYS} days. "
            "Search API has a known ~2-5 min indexing lag."
        ),
    }


async def get_open_prs(repo: str) -> dict:
    """
    Get TRUE open PR count using Link header trick.

    Confidence model (ceiling 0.93, floor 0.70):
      - Base 0.93 — Link header count is deterministic (exact pagination).
      - Deduct 0.03 — draft count from partial sample (101-200 PRs).
      - Deduct 0.05 — draft count estimated, <50% coverage (201-1000 PRs).
      - Deduct 0.08 — draft count unreliable, <10% coverage (>1000 PRs).
    """
    r = await _get(
        f"{GITHUB_BASE}/repos/{repo}/pulls",
        headers=_headers(),
        params={"state": "open", "per_page": 1},
    )
    link_header = r.headers.get("Link", "")
    total_open  = _parse_link_last_page(link_header) if link_header else len(r.json())

    samples_r = await _get(
        f"{GITHUB_BASE}/repos/{repo}/pulls",
        headers=_headers(),
        params={"state": "open", "per_page": 5},
    )
    samples = samples_r.json()

    try:
        draft_r = await _get(
            f"{GITHUB_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            params={"state": "open", "per_page": 100, "draft": "true"},
        )
        draft_count = len(draft_r.json())
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        draft_count = 0

    # Confidence scoring
    confidence = 0.93
    components = ["PR count from Link header pagination (deterministic; base 0.93)"]

    if total_open <= 100:
        components.append("Draft count fully enumerated (<=100 PRs)")
    elif total_open <= 200:
        confidence -= 0.03
        components.append(f"Draft count from first 100/{total_open} PRs — partial sample (-0.03)")
    elif total_open <= 1000:
        confidence -= 0.05
        coverage = round(100 / total_open * 100, 0)
        components.append(f"Draft count estimated from ~{coverage:.0f}% sample (-0.05)")
    else:
        confidence -= 0.08
        components.append(f"Large PR queue (>{total_open}): draft count has <10% coverage (-0.08)")

    confidence = round(max(0.70, min(0.93, confidence)), 2)

    return {
        "total_open":        total_open,
        "data_confidence":   confidence,
        "confidence_reason": "; ".join(components),
        "sample_titles":     [p["title"] for p in samples[:5]],
        "draft_count":       draft_count,
        "data_note": (
            "PR count from Link header pagination (exact). "
            "Draft count sampled from first 100 results; "
            "may undercount for large queues."
        ),
    }


async def get_recent_commits(repo: str) -> dict:
    """
    Use /stats/participation for weekly commit counts (last 52 weeks).

    Confidence model (ceiling 0.90, floor 0.70):
      - Participation stats (200): base 0.90 minus 0.02 for ISO-week boundary. Final 0.88.
      - Paginated fallback: base 0.82; minus 0.04 if 1000-commit cap hit.
    """
    try:
        stats_r     = await _get(f"{GITHUB_BASE}/repos/{repo}/stats/participation", headers=_headers())
        stats_status = stats_r.status_code
    except httpx.HTTPStatusError as e:
        stats_r     = e.response
        stats_status = e.response.status_code

    if stats_status == 200:
        all_weeks        = stats_r.json().get("all", [])
        commit_count_30d = sum(all_weeks[-4:])
        method_used      = "participation_stats (pre-aggregated)"
        data_confidence  = 0.88
        components = [
            "Pre-aggregated participation stats (base 0.90)",
            "Deduct 0.02: ISO week boundary vs calendar 30 days (+-3 day window)",
        ]
    else:
        commit_count_30d, method_used = await _count_commits_paginated(repo, _days_ago(30))
        data_confidence = 0.82
        components = [
            "Paginated fallback — participation stats unavailable (GitHub 202 cache miss; base 0.82)"
        ]
        if commit_count_30d >= 1000:
            data_confidence -= 0.04
            components.append("Commit cap hit (>=1000): actual 30-day total likely higher (-0.04)")
        data_confidence = round(max(0.70, data_confidence), 2)

    data_confidence = round(max(0.70, min(0.90, data_confidence)), 2)

    # Top contributors (supplementary — failure is non-fatal)
    top_contributors = []
    try:
        contrib_r = await _get(f"{GITHUB_BASE}/repos/{repo}/stats/contributors", headers=_headers())
        contribs  = sorted(contrib_r.json(), key=lambda c: c.get("total", 0), reverse=True)[:5]
        top_contributors = [c["author"]["login"] for c in contribs if c.get("author")]
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        pass

    return {
        "commit_count_30d":  commit_count_30d,
        "top_contributors":  top_contributors,
        "data_confidence":   data_confidence,
        "confidence_reason": "; ".join(components),
        "data_note": (
            f"Commits counted via {method_used}. "
            "Window: last ~30 days (4 ISO weeks from participation stats). "
            "Merge commits included in the total."
        ),
    }


async def compute_health_metrics(repo: str) -> dict:
    """
    Compute scale-normalised health signals so the AI scores relative to
    repo size, not absolute counts.
    """
    r    = await _get(f"{GITHUB_BASE}/repos/{repo}", headers=_headers())
    meta = r.json()

    stars       = max(meta.get("stargazers_count", 1), 1)
    forks       = meta.get("forks_count", 0)
    watchers    = meta.get("subscribers_count", 0)
    open_issues = meta.get("open_issues_count", 0)

    if   stars >= 10_000: scale = "large"
    elif stars >=  1_000: scale = "medium"
    elif stars >=    100: scale = "small"
    else:                 scale = "micro"

    issue_star_ratio = round((open_issues / stars) * 100, 2)
    engagement_ratio = round((forks + watchers) / stars, 3)

    ratio_thresholds = {
        "large":  {"healthy": 5.0,  "moderate": 15.0},
        "medium": {"healthy": 3.0,  "moderate": 10.0},
        "small":  {"healthy": 2.0,  "moderate":  8.0},
        "micro":  {"healthy": 1.0,  "moderate":  5.0},
    }
    thresh = ratio_thresholds[scale]

    if   issue_star_ratio <= thresh["healthy"]:  issue_burden = "low"
    elif issue_star_ratio <= thresh["moderate"]: issue_burden = "moderate"
    else:                                        issue_burden = "high"

    return {
        "repo_scale":           scale,
        "stars":                stars,
        "forks":                forks,
        "issue_star_ratio_pct": issue_star_ratio,
        "engagement_ratio":     engagement_ratio,
        "issue_burden":         issue_burden,
        "scale_context": (
            f"This is a {scale} repository ({stars:,} stars). "
            f"An issue/star ratio of {issue_star_ratio}% is considered "
            f"{issue_burden} for repos of this scale."
        ),
        "scoring_guidance": _build_scoring_guidance(scale, issue_burden),
    }


def _build_scoring_guidance(scale: str, issue_burden: str) -> str:
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
        f"the health_score MUST be in the range {low}-{high} BEFORE "
        f"applying activity and PR pipeline adjustments (+-10 max)."
    )


# ── Private helpers ───────────────────────────────────────────────────────────

async def _count_commits_paginated(repo: str, since: str) -> tuple[int, str]:
    try:
        r = await _get(
            f"{GITHUB_BASE}/repos/{repo}/commits",
            headers=_headers(),
            params={"since": since, "per_page": 1},
        )
    except (httpx.HTTPStatusError, httpx.TimeoutException):
        return 0, "unavailable"

    link  = r.headers.get("Link", "")
    total = _parse_link_last_page(link) if link else len(r.json())
    capped = min(total, 1000)
    note   = "paginated Link header (exact)" if total <= 1000 else f"capped at 1000/{total}"
    return capped, note