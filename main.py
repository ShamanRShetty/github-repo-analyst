import patch          # ← MUST be first: patches json.dumps before ADK loads

import os
import json
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import repo_analyst_agent
from models import AnalyzeRequest, RepoHealth

from models import DataQuality
import statistics

async def _build_data_quality(session_data: dict) -> DataQuality:
    """Pull confidence scores emitted by tools and attach to response."""
    issues_conf = session_data.get("issues_confidence", 1.0)
    prs_conf    = session_data.get("prs_confidence", 1.0)
    commits_conf = session_data.get("commits_confidence", 1.0)
    notes        = session_data.get("data_notes", [])

    return DataQuality(
        issues_confidence=issues_conf,
        prs_confidence=prs_conf,
        commits_confidence=commits_conf,
        overall_confidence=round(statistics.mean([issues_conf, prs_conf, commits_conf]), 2),
        notes=notes
    )

load_dotenv()

app = FastAPI(title="GitHub Repo Analyst Agent")

session_service = InMemorySessionService()
runner = Runner(
    agent=repo_analyst_agent,
    app_name="github_analyst",
    session_service=session_service
)

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

        final_text = ""

        async for event in runner.run_async(
            user_id="api_user",
            session_id=session.id,
            new_message=message
        ):
            if event.content is None:
                continue

            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:
                    final_text += part.text
                elif hasattr(part, "inline_data") and part.inline_data:
                    data = part.inline_data.data
                    mime = getattr(part.inline_data, "mime_type", "unknown")
                    print(f"WARNING: binary part ({mime}, {len(data)} bytes) — skipping")

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
            parsed.pop("data_quality", None)  # remove if exists
            data_quality = await _build_data_quality(parsed)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Agent returned malformed JSON: {str(e)}\nRaw (first 500 chars): {clean[:500]}"
            )

        return RepoHealth(repo=request.repo,data_quality=data_quality,**parsed)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print("=== FULL ERROR ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))