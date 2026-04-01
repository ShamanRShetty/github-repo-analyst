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

tools = [
    FunctionTool(func=compute_health_metrics),
    FunctionTool(func=get_repo_info),
    FunctionTool(func=get_recent_commits),
    FunctionTool(func=get_open_prs),
    FunctionTool(func=get_open_issues),
]

repo_analyst_agent = Agent(
    name="github_repo_analyst",
    model="gemini-2.5-flash",  # universally available on all GCP projects
    description="Analyses a GitHub repository and returns a structured health report.",
    instruction=SYSTEM_PROMPT,
    tools=tools,
)