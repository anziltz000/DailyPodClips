import re
import os

with open("backend/main.py", "r", encoding="utf-8") as f:
    code = f.read()

# Add imports
imports = """
import psutil
import aiofiles
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from backend.projects import (
    init_projects, load_projects, create_project, rename_project,
    delete_project, cleanup_old_projects, get_project_dir
)
"""
code = re.sub(r'import psutil\nimport aiofiles\nfrom fastapi import FastAPI, HTTPException, Request\nfrom fastapi\.responses import FileResponse, JSONResponse\nfrom fastapi\.staticfiles import StaticFiles\nfrom sse_starlette\.sse import EventSourceResponse\nfrom pydantic import BaseModel', imports.strip(), code)

# Update global state to be per-project
state_repl = """
# -- Global State --
init_projects()
# project_id -> {"active_processes": {}, "log_subscribers": {}, "current_video": None, "current_transcript": None}
project_state = {}

def get_pstate(pid: str):
    if not pid:
        pid = "default"
    if pid not in project_state:
        project_state[pid] = {
            "active_processes": {},
            "log_subscribers": {},
            "current_video": None,
            "current_transcript": None,
        }
    return project_state[pid]
"""
code = re.sub(r'# -- Global State --.*?current_transcript: Optional\[str\] = None', state_repl.strip(), code, flags=re.DOTALL)

# Now, we need to inject project_id into functions.
# Helper to replace broadcast_log
code = code.replace("async def broadcast_log(block_id: str, message: str):", "async def broadcast_log(project_id: str, block_id: str, message: str):")
code = code.replace("log_subscribers[block_id]", "get_pstate(project_id)['log_subscribers'].get(block_id, [])")
code = code.replace("if block_id not in log_subscribers:", "if block_id not in get_pstate(project_id)['log_subscribers']:\n        get_pstate(project_id)['log_subscribers'][block_id] = []")
code = code.replace("for queue in log_subscribers[block_id]:", "for queue in get_pstate(project_id)['log_subscribers'][block_id]:")

# Helper to replace run_subprocess_with_logs
code = code.replace("async def run_subprocess_with_logs(block_id: str, cmd: list, cwd: str = None, env: dict = None) -> int:", "async def run_subprocess_with_logs(project_id: str, block_id: str, cmd: list, cwd: str = None, env: dict = None) -> int:")
code = code.replace("await broadcast_log(block_id", "await broadcast_log(project_id, block_id")
code = code.replace("active_processes[block_id] = process", "get_pstate(project_id)['active_processes'][block_id] = process")
code = code.replace("active_processes.pop(block_id, None)", "get_pstate(project_id)['active_processes'].pop(block_id, None)")

with open("backend/main2.py", "w", encoding="utf-8") as f:
    f.write(code)
