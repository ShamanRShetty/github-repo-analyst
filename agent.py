from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from mcp_tools import (
    get_repo_info,
    get_open_issues,
    get_open_prs,
    get_recent_commits,
    compute_health_metrics,
)
from prompts import SYSTEM_PROMPT

# Wrap each async function as an ADK FunctionTool
tools = [
    FunctionTool(func=compute_health_metrics),  # 1️⃣ scale context first
    FunctionTool(func=get_repo_info),           # 2️⃣ basic metadata
    FunctionTool(func=get_recent_commits),      # 3️⃣ activity signal
    FunctionTool(func=get_open_prs),            # 4️⃣ PR pipeline
    FunctionTool(func=get_open_issues),         # 5️⃣ issue burden details
]

repo_analyst_agent = Agent(
    name="github_repo_analyst",
    model="gemini-2.0-flash-001",
    description="Analyses a GitHub repository and returns a structured health report.",
    instruction=SYSTEM_PROMPT,
    tools=tools,
)