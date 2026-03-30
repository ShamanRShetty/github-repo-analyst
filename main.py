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
from models import AnalyzeRequest, RepoHealth, DataQuality

load_dotenv()

app = FastAPI(title="GitHub Repo Analyst Agent")

session_service = InMemorySessionService()
runner = Runner(
    agent=repo_analyst_agent,
    app_name="github_analyst",
    session_service=session_service
)

# Gemini API retry config — 503 spikes are usually resolved within seconds
_LLM_MAX_RETRIES   = 3
_LLM_RETRY_BACKOFF = 5.0   # seconds; multiplied by attempt (5s, 10s, 15s)
_LLM_RETRY_CODES   = {503, 429}   # UNAVAILABLE and RESOURCE_EXHAUSTED


async def _run_agent(session_id: str, message: types.Content) -> str:
    """
    Stream the agent and collect all text output.
    Retries automatically on transient Gemini API errors (503 / 429).
    """
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
        session = session_service.create_session(
            app_name="github_analyst",
            user_id="api_user"
        )

        prompt = f"Analyse the GitHub repository: {request.repo}. Focus: {request.focus}"
        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)]
        )

        final_text = await _run_agent(session.id, message)

        if not final_text.strip():
            raise HTTPException(status_code=500, detail="Agent returned empty response")

        # Strip markdown code fences if the model wrapped its JSON
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
                detail=f"Agent returned malformed JSON: {str(e)}\nRaw (first 500 chars): {clean[:500]}"
            )

        # ── data_quality ──────────────────────────────────────────────────────
        # The agent builds data_quality from the tool outputs in its JSON.
        # We validate and normalise it here rather than overwriting with
        # session-state values that were never populated.
        raw_dq = parsed.pop("data_quality", None) or {}

        issues_conf  = _clamp(raw_dq.get("issues_confidence",  0.85))
        prs_conf     = _clamp(raw_dq.get("prs_confidence",     0.85))
        commits_conf = _clamp(raw_dq.get("commits_confidence", 0.85))
        notes        = raw_dq.get("notes", [])

        # Ensure notes is a list of strings (agent occasionally returns a dict)
        if isinstance(notes, dict):
            notes = [f"{k}: {v}" for k, v in notes.items()]
        elif not isinstance(notes, list):
            notes = []

        data_quality = DataQuality(
            issues_confidence=issues_conf,
            prs_confidence=prs_conf,
            commits_confidence=commits_conf,
            overall_confidence=round(statistics.mean([issues_conf, prs_conf, commits_conf]), 2),
            notes=[str(n) for n in notes],
        )

        return RepoHealth(repo=request.repo, data_quality=data_quality, **parsed)

    except HTTPException:
        raise
    except genai_errors.ServerError as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", 503)
        detail = f"Gemini API error ({status}): {exc}. Try again in a few seconds."
        raise HTTPException(status_code=503, detail=detail)
    except Exception as e:
        import traceback
        print("=== FULL ERROR ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _clamp(v) -> float:
    """Coerce agent-supplied confidence values to a valid 0.0–1.0 float."""
    try:
        return round(min(1.0, max(0.0, float(v))), 2)
    except (TypeError, ValueError):
        return 0.85