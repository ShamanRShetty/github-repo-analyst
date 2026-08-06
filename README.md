# RepoScope — GitHub Repository Health Analyst

An AI agent that analyzes any public GitHub repository and returns a structured, scale-normalized health report — not just raw stats, but a score that accounts for whether the repo has 50 stars or 50,000.

Built with Google's Agent Development Kit (ADK), Gemini 2.5 Flash on Vertex AI, and FastAPI.

## Why This Isn't Just "Ask Gemini About a Repo"

Two design decisions separate this from a thin LLM wrapper:

1. **Scale-normalized scoring.** A "high" issue count means something different for a repo with 50,000 stars than one with 500. `compute_health_metrics` buckets the repo into a scale tier (micro/small/medium/large) and an issue-burden tier (low/moderate/high) based on its issue-to-star ratio, then hands the agent a scoring range to work within — so the model isn't guessing what "healthy" looks like from raw numbers alone.

2. **The agent doesn't get the final say on its own score.** The LLM computes a `health_score` and shows its work (`_score_workings`). The server then *independently recomputes* the score from the same raw inputs using a pure, deterministic scoring function (`_compute_server_score`). If the agent's number deviates from the server's recomputation by more than 3 points, the server's number wins and the correction is logged in the response's `data_quality.notes`. This catches arithmetic drift or rule-skipping in the model's output instead of trusting it blindly.

Each data point returned by GitHub's API also carries its own confidence score and a plain-language explanation of *why* — e.g., issue counts have known indexing lag from the Search API, PR draft counts are estimated from a sample when a repo has hundreds of open PRs, and so on. The health score itself gets penalized when confidence is low, so a data-quality problem shows up as a lower score, not a silently wrong one.

## What It Actually Checks

For a given `owner/repo`, the agent calls five tools in sequence and produces one report:

- **Repo metadata** — stars, forks, language, description, GitHub's own open-issue counter
- **True open issue count** — via the Search API (GitHub's repo metadata field can lag), plus a stale-issue count (no activity in 90+ days)
- **Open PR pipeline** — exact count via Link-header pagination, plus a sampled estimate of how many are drafts
- **Recent commit activity** — 30-day commit count, pulled from GitHub's participation stats with a paginated fallback if that endpoint isn't ready
- **Top contributors**

The final output includes a 0–100 health score, a summary, insights, actionable recommendations, and a `data_quality` block showing per-source confidence and any server-side correction.

## Tech Stack

- **Google ADK** (`google-adk`) — agent orchestration and tool-calling
- **Vertex AI** — LLM backend (Gemini 2.5 Flash), not the public Gemini API
- **FastAPI** + **Uvicorn** — HTTP API
- **httpx** — async GitHub API client, with DNS pre-resolution and retry-on-timeout built in (see `mcp_tools.py`) to work around slow DNS resolution in constrained environments
- **Pydantic** — response schema validation, including a hard `0 ≤ health_score ≤ 100` guard rail
- **Docker** — multi-stage build, deployed for Cloud Run (reads `$PORT` at container start)

## Architecture

```
main.py        FastAPI app — /analyze endpoint, runs the agent, then
                independently validates/corrects the returned score
agent.py       ADK agent definition (model, system prompt, tool list)
prompts.py     System prompt — the mandatory scoring chain-of-thought
                the model must follow (kept in lockstep with main.py's
                server-side scoring function)
mcp_tools.py   GitHub API tools: repo info, issues, PRs, commits,
                and the scale/issue-burden classifier
models.py      Pydantic request/response schemas
patch.py       Patches json.dumps before ADK loads, so binary data in
                telemetry payloads can't crash the process
ui.html        Single-page frontend served at /
```

## Running Locally

### Prerequisites
- Python 3.11+
- A Google Cloud project with Vertex AI enabled
- (Optional but recommended) a GitHub personal access token — unauthenticated requests hit GitHub's stricter rate limit

### Setup

```bash
pip install -r requirements.txt

export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
export GOOGLE_CLOUD_LOCATION=us-central1   # optional, defaults to this
export GITHUB_TOKEN=ghp_your_token_here    # optional, raises GitHub rate limits

uvicorn main:app --reload
```

Then open `http://localhost:8000` for the UI, or call the API directly:

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"repo": "facebook/react", "focus": "general"}'
```

### Docker

```bash
docker build -t reposcope .
docker run -p 8080:8080 \
  -e GOOGLE_CLOUD_PROJECT=your-gcp-project-id \
  -e GITHUB_TOKEN=ghp_your_token_here \
  reposcope
```

Cloud Run sets `$PORT` automatically; the container respects it.

## API

### `POST /analyze`

**Request**
```json
{ "repo": "owner/repo", "focus": "general" }
```

**Response** (`RepoHealth`)
```json
{
  "repo": "owner/repo",
  "health_score": 78,
  "summary": "...",
  "open_issues": 142,
  "open_prs": 23,
  "stale_issues": 40,
  "recent_commits": 67,
  "top_contributors": ["...", "..."],
  "insights": ["..."],
  "recommendations": ["..."],
  "data_quality": {
    "issues_confidence": 0.88,
    "prs_confidence": 0.93,
    "commits_confidence": 0.88,
    "overall_confidence": 0.9,
    "notes": ["..."]
  }
}
```

### `GET /health`
Basic liveness check for Cloud Run.

## Notes on Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Yes | Vertex AI project — the app fails to start without it |
| `GOOGLE_CLOUD_LOCATION` | No | Defaults to `us-central1` |
| `GITHUB_TOKEN` | No, but recommended | Without it, GitHub's unauthenticated rate limit (60 req/hr) will get hit quickly |
