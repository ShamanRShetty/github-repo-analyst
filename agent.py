from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from mcp_tools import get_repo_info, get_open_issues, get_open_prs, get_recent_commits
from prompts import SYSTEM_PROMPT

# Wrap each async function as an ADK FunctionTool
tools = [
    FunctionTool(func=get_repo_info),
    FunctionTool(func=get_open_issues),
    FunctionTool(func=get_open_prs),
    FunctionTool(func=get_recent_commits),
]

repo_analyst_agent = Agent(
    name="github_repo_analyst",
    model="gemini-3-flash-preview",
    description="Analyses a GitHub repository and returns a structured health report.",
    instruction=SYSTEM_PROMPT,
    tools=tools,
)