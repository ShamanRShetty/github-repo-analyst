import patch          # ← MUST be first: patches json.dumps before ADK loads

import asyncio
import os
import json
import statistics
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai import errors as genai_errors

from agent import repo_analyst_agent
from mcp_tools import compute_health_metrics
from models import AnalyzeRequest, RepoHealth, DataQuality
from fastapi.responses import HTMLResponse


load_dotenv()

app = FastAPI(title="GitHub Repo Analyst Agent")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("ui.html", "r", encoding="utf-8") as f:
        return f.read()

session_service = InMemorySessionService()
runner = Runner(
    agent=repo_analyst_agent,
    app_name="github_analyst",
    session_service=session_service
)

_LLM_MAX_RETRIES   = 3
_LLM_RETRY_BACKOFF = 5.0
_LLM_RETRY_CODES   = {503, 429}


# ── Scoring tables — must stay in sync with prompts.py ────────────────────────

_BASE_RANGES: dict[tuple[str, str], tuple[int, int]] = {
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

_TOTAL_ADJ_MIN  = -15
_TOTAL_ADJ_MAX  =  +8   # asymmetric — positive bonus capped lower than penalty floor
_CEILING_BONUS  =  +5
_FLOOR_PENALTY  = -15


# ── Adjustment functions — single source of truth ────────────────────────────

def _commit_adj(recent_commits: int) -> int:
    if recent_commits > 50:  return +5
    if recent_commits >= 20: return +3
    if recent_commits >= 5:  return -3
    return -7


def _pr_adj(open_prs: int, draft_count: int) -> int:
    """Rule order is mandatory: > 500 check must come first."""
    if open_prs > 500:
        return -5
    if open_prs > 100 and open_prs > 0 and (draft_count / open_prs) > 0.5:
        return -3
    if open_prs < 10:
        return +3
    return 0


def _stale_adj(stale_ratio: float) -> int:
    if stale_ratio < 0.20:   return +2
    if stale_ratio <= 0.40:  return  0
    if stale_ratio <= 0.60:  return -3
    return -6   # > 0.60 — mandatory, no exceptions


def _dq_adj(overall_confidence: float) -> int:
    """Two-tier penalty:  < 0.80 → -5,  0.80–0.84 → -3,  ≥ 0.85 → 0."""
    if overall_confidence < 0.80:
        return -5
    if overall_confidence < 0.85:
        return -3
    return 0


def _compute_server_score(
    low: int,
    high: int,
    recent_commits: int,
    open_prs: int,
    draft_count: int,
    stale_ratio: float,
    overall_confidence: float,
) -> tuple[int, dict]:
    """
    Pure scoring function — recomputes health_score from first principles.
    Returns (score, audit_dict).
    """
    base_score = (low + high) // 2

    c_adj = _commit_adj(recent_commits)
    p_adj = _pr_adj(open_prs, draft_count)
    s_adj = _stale_adj(stale_ratio)
    d_adj = _dq_adj(overall_confidence)

    total_raw     = c_adj + p_adj + s_adj + d_adj
    total_clamped = max(_TOTAL_ADJ_MIN, min(_TOTAL_ADJ_MAX, total_raw))
    clamped       = base_score + total_clamped
    ceiling       = high + _CEILING_BONUS
    floor_val     = low + _FLOOR_PENALTY
    score         = int(max(floor_val, min(ceiling, clamped)))

    audit = {
        "base_range":         [low, high],
        "base_score":         base_score,
        "stale_ratio":        round(stale_ratio, 4),
        "overall_confidence": round(overall_confidence, 2),
        "commit_adj":         c_adj,
        "pr_adj":             p_adj,
        "stale_adj":          s_adj,
        "dq_adj":             d_adj,
        "total_adj_raw":      total_raw,
        "total_adj_clamped":  total_clamped,
        "clamped_score":      clamped,
        "ceiling":            ceiling,
        "floor":              floor_val,
        "server_score":       score,
    }
    return score, audit


async def _fetch_health_context(repo: str) -> dict:
    """
    Call compute_health_metrics directly so the validator always has
    the authoritative scale + issue_burden.  Safe fallback on error.
    """
    try:
        return await compute_health_metrics(repo)
    except Exception as exc:
        print(f"[health_ctx] compute_health_metrics failed: {exc} — using fallback")
        return {"repo_scale": "large", "issue_burden": "low"}


def _base_range_from_context(ctx: dict) -> tuple[int, int]:
    scale  = ctx.get("repo_scale",   "large")
    burden = ctx.get("issue_burden", "low")
    return _BASE_RANGES.get((scale, burden), (68, 82))


def validate_and_correct_score(parsed: dict, health_ctx: dict) -> tuple[int, dict]:
    """
    Re-run the scoring formula with authoritative context.
    Correct the agent score when deviation > 3.
    Returns (final_score, audit_dict).
    """
    open_issues    = int(parsed.get("open_issues",    0))
    stale_issues   = int(parsed.get("stale_issues",   0))
    open_prs       = int(parsed.get("open_prs",       0))
    recent_commits = int(parsed.get("recent_commits", 0))

    # data_quality must still be in parsed at this point (popped later)
    raw_dq       = parsed.get("data_quality") or {}
    issues_conf  = _clamp(raw_dq.get("issues_confidence",  0.85))
    prs_conf     = _clamp(raw_dq.get("prs_confidence",     0.85))
    commits_conf = _clamp(raw_dq.get("commits_confidence", 0.85))
    overall_conf = round(statistics.mean([issues_conf, prs_conf, commits_conf]), 2)

    stale_ratio  = round(stale_issues / open_issues, 4) if open_issues > 0 else 0.0

    workings    = parsed.get("_score_workings") or {}
    draft_count = int(workings.get("draft_count", 0))

    low, high = _base_range_from_context(health_ctx)

    sv, audit = _compute_server_score(
        low=low, high=high,
        recent_commits=recent_commits,
        open_prs=open_prs,
        draft_count=draft_count,
        stale_ratio=stale_ratio,
        overall_confidence=overall_conf,
    )

    agent_score = int(parsed.get("health_score", sv))
    deviation   = abs(agent_score - sv)

    audit["agent_score"]     = agent_score
    audit["deviation"]       = deviation
    audit["score_corrected"] = deviation > 3

    print(
        f"[score_validator] "
        f"scale={health_ctx.get('repo_scale')} "
        f"burden={health_ctx.get('issue_burden')} "
        f"base=[{low},{high}] base_score={audit['base_score']} | "
        f"commit_adj={audit['commit_adj']} "
        f"pr_adj={audit['pr_adj']} "
        f"stale_adj={audit['stale_adj']} "
        f"dq_adj={audit['dq_adj']} "
        f"(overall_conf={overall_conf:.2f}) | "
        f"total_raw={audit['total_adj_raw']} "
        f"total_clamped={audit['total_adj_clamped']} | "
        f"server={sv} agent={agent_score} deviation={deviation} "
        f"corrected={audit['score_corrected']}"
    )

    if deviation > 3:
        return sv, audit
    return agent_score, audit


async def _run_agent(session_id: str, message: types.Content) -> str:
    last_exc: Exception | None = None

    for attempt in range(1, _LLM_MAX_RETRIES + 1):
        try:
            final_text = ""
            async for event in runner.run_async(
                user_id="api_user",
                session_id=session_id,
                new_message=message,
            ):
                if event.content is None:
                    continue
                for part in event.content.parts or []:
                    if hasattr(part, "text") and part.text:
                        final_text += part.text
                    elif hasattr(part, "inline_data") and part.inline_data:
                        mime = getattr(part.inline_data, "mime_type", "unknown")
                        print(f"WARNING: binary part ({mime}) — skipping")
            return final_text

        except genai_errors.ServerError as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "code", 0)
            if status in _LLM_RETRY_CODES and attempt < _LLM_MAX_RETRIES:
                wait = _LLM_RETRY_BACKOFF * attempt
                print(f"Gemini {status} on attempt {attempt} — retrying in {wait:.0f}s")
                last_exc = exc
                await asyncio.sleep(wait)
                continue
            raise

        except Exception:
            raise

    raise last_exc


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/analyze", response_model=RepoHealth)
async def analyze_repo(request: AnalyzeRequest):
    try:
        # 1. Pre-fetch authoritative scale/burden before the agent runs
        health_ctx = await _fetch_health_context(request.repo)
        print(
            f"[health_ctx] repo={request.repo} "
            f"scale={health_ctx.get('repo_scale')} "
            f"burden={health_ctx.get('issue_burden')} "
            f"issue_star_ratio={health_ctx.get('issue_star_ratio_pct')}%"
        )

        # 2. Run the agent
        session = session_service.create_session(
            app_name="github_analyst",
            user_id="api_user"
        )
        prompt  = f"Analyse the GitHub repository: {request.repo}. Focus: {request.focus}"
        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)]
        )
        final_text = await _run_agent(session.id, message)

        if not final_text.strip():
            raise HTTPException(status_code=500, detail="Agent returned empty response")

        # 3. Parse JSON, strip markdown fences
        clean = final_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
            if clean.endswith("```"):
                clean = clean.rsplit("```", 1)[0]
        clean = clean.strip()

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Agent returned malformed JSON: {e}\n"
                    f"Raw (first 500 chars): {clean[:500]}"
                ),
            )

        # 4. Validate and correct score — BEFORE popping data_quality
        final_score, score_audit = validate_and_correct_score(parsed, health_ctx)
        parsed["health_score"] = final_score
        parsed.pop("_score_workings", None)

        # 5. Build data_quality
        raw_dq       = parsed.pop("data_quality", None) or {}
        issues_conf  = _clamp(raw_dq.get("issues_confidence",  0.85))
        prs_conf     = _clamp(raw_dq.get("prs_confidence",     0.85))
        commits_conf = _clamp(raw_dq.get("commits_confidence", 0.85))
        notes        = raw_dq.get("notes", [])

        if isinstance(notes, dict):
            notes = [f"{k}: {v}" for k, v in notes.items()]
        elif not isinstance(notes, list):
            notes = []

        if score_audit.get("score_corrected"):
            notes.append(
                f"[server_score_correction] "
                f"agent={score_audit['agent_score']} "
                f"→ server={score_audit['server_score']} "
                f"(base=[{score_audit['base_range'][0]},{score_audit['base_range'][1]}] "
                f"commit_adj={score_audit['commit_adj']} "
                f"pr_adj={score_audit['pr_adj']} "
                f"stale_adj={score_audit['stale_adj']} "
                f"dq_adj={score_audit['dq_adj']} "
                f"stale_ratio={score_audit['stale_ratio']:.3f} "
                f"overall_conf={score_audit['overall_confidence']:.2f})"
            )

        data_quality = DataQuality(
            issues_confidence=issues_conf,
            prs_confidence=prs_conf,
            commits_confidence=commits_conf,
            overall_confidence=round(
                statistics.mean([issues_conf, prs_conf, commits_conf]), 2
            ),
            notes=[str(n) for n in notes],
        )

        return RepoHealth(repo=request.repo, data_quality=data_quality, **parsed)

    except HTTPException:
        raise
    except genai_errors.ServerError as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", 503)
        raise HTTPException(
            status_code=503,
            detail=f"Gemini API error ({status}): {exc}. Try again in a few seconds.",
        )
    except Exception as e:
        import traceback
        print("=== FULL ERROR ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _clamp(v) -> float:
    try:
        return round(min(1.0, max(0.0, float(v))), 2)
    except (TypeError, ValueError):
        return 0.85