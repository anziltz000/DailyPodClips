import json
import uuid
import time
import shutil
from pathlib import Path
from backend.config import DATA_DIR

PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_FILE = DATA_DIR / "projects.json"

def init_projects():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not PROJECTS_FILE.exists():
        PROJECTS_FILE.write_text(json.dumps({"projects": []}))

def load_projects():
    init_projects()
    try:
        return json.loads(PROJECTS_FILE.read_text()).get("projects", [])
    except Exception:
        return []

def save_projects(projects):
    PROJECTS_FILE.write_text(json.dumps({"projects": projects}, indent=2))

def create_project(name: str):
    project_id = str(uuid.uuid4())
    project = {
        "id": project_id,
        "name": name,
        "created_at": time.time(),
    }
    projects = load_projects()
    projects.append(project)
    save_projects(projects)
    
    # Create project dirs
    p_dir = PROJECTS_DIR / project_id
    (p_dir / "downloads").mkdir(parents=True, exist_ok=True)
    (p_dir / "processed").mkdir(parents=True, exist_ok=True)
    (p_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (p_dir / "temp").mkdir(parents=True, exist_ok=True)
    
    return project

def rename_project(project_id: str, new_name: str):
    projects = load_projects()
    for p in projects:
        if p["id"] == project_id:
            p["name"] = new_name
            break
    save_projects(projects)

def delete_project(project_id: str):
    projects = [p for p in load_projects() if p["id"] != project_id]
    save_projects(projects)
    
    p_dir = PROJECTS_DIR / project_id
    if p_dir.exists():
        shutil.rmtree(p_dir, ignore_errors=True)

def cleanup_old_projects(days=15):
    projects = load_projects()
    now = time.time()
    cutoff = now - (days * 86400)
    
    to_keep = []
    for p in projects:
        if p.get("created_at", 0) < cutoff:
            p_dir = PROJECTS_DIR / p["id"]
            if p_dir.exists():
                shutil.rmtree(p_dir, ignore_errors=True)
        else:
            to_keep.append(p)
            
    if len(to_keep) != len(projects):
        save_projects(to_keep)

def get_project_dir(project_id: str, sub_dir: str) -> Path:
    d = PROJECTS_DIR / project_id / sub_dir
    d.mkdir(parents=True, exist_ok=True)
    return d
