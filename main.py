import os
import json
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import repo_analyst_agent
from models import AnalyzeRequest, RepoHealth

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
        final_content = None

        # Run the agent (no nested event loop hacks needed)
        async for event in runner.run_async(
            user_id="api_user",
            session_id=session.id,
            new_message=message
        ):
            if event.content is None:
                continue

            # Prefer the proper .text attribute
            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:          # normal text
                    final_text += part.text

                elif hasattr(part, "inline_data") and part.inline_data:   # binary data
                    # This is the source of your bytes error
                    data = part.inline_data.data
                    mime = getattr(part.inline_data, "mime_type", "unknown")
                    print(f"WARNING: Received binary part ({mime}, {len(data)} bytes) — skipping for JSON response")
                    # You could base64-encode it if you really need it, but usually you don't for a repo analysis

        if not final_text.strip():
            raise HTTPException(status_code=500, detail="Agent returned empty response")

        # === Clean markdown code fences (your existing logic, improved) ===
        clean = final_text.strip()
        if clean.startswith("```"):
            # Remove opening fence and optional language
            clean = clean.split("\n", 1)[-1]          # take everything after first newline
            if clean.endswith("```"):
                clean = clean.rsplit("```", 1)[0]     # remove closing fence

        clean = clean.strip()

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Agent returned malformed JSON: {str(e)}\nRaw (first 500 chars): {clean[:500]}"
            )

        return RepoHealth(repo=request.repo, **parsed)

    except Exception as e:
        # Log the real error for debugging
        import traceback
        print("=== FULL ERROR ===")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    

