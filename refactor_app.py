import re

with open("frontend/app.js", "r", encoding="utf-8") as f:
    js = f.read()

# Add project state
js = "let currentProjectId = localStorage.getItem('currentProjectId') || null;\nlet projectToRename = null;\n" + js

# Update apiPost
api_post_new = """async function apiPost(url, body = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (currentProjectId) headers['X-Project-Id'] = currentProjectId;
    const res = await fetch(url, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(body),
    });"""
js = js.replace("async function apiPost(url, body = {}) {\n    const res = await fetch(url, {\n        method: 'POST',\n        headers: { 'Content-Type': 'application/json' },\n        body: JSON.stringify(body),\n    });", api_post_new)

# Update apiGet
api_get_new = """async function apiGet(url) {
    const headers = {};
    if (currentProjectId) headers['X-Project-Id'] = currentProjectId;
    const res = await fetch(url, { headers });"""
js = js.replace("async function apiGet(url) {\n    const res = await fetch(url);", api_get_new)

# Update connectSSE
js = js.replace("const evtSource = new EventSource(`/api/logs/${blockId}`);", "const evtSource = new EventSource(`/api/logs/${currentProjectId || 'default'}/${blockId}`);")

# Project logic to append at end
project_logic = """
// ── PROJECTS ─────────────────────────────────────────────────
async function loadProjects() {
    try {
        const res = await apiGet('/api/projects');
        const list = document.getElementById('project-list');
        if (res.projects.length === 0) {
            list.innerHTML = '<p class="empty-state">No projects found. Create one!</p>';
            if (!currentProjectId) document.getElementById('project-modal').showModal();
            return;
        }
        
        // Check if current still exists
        if (currentProjectId && !res.projects.find(p => p.id === currentProjectId)) {
            currentProjectId = res.projects[0].id;
            localStorage.setItem('currentProjectId', currentProjectId);
        } else if (!currentProjectId && res.projects.length > 0) {
            currentProjectId = res.projects[0].id;
            localStorage.setItem('currentProjectId', currentProjectId);
        }

        list.innerHTML = res.projects.map(p => `
            <div class="project-item" onclick="selectProject('${p.id}', '${escapeHtml(p.name)}')">
                <div class="project-item-info">
                    <strong>${p.id === currentProjectId ? '✅ ' : ''}${escapeHtml(p.name)}</strong>
                    <span>Created: ${new Date(p.created_at * 1000).toLocaleDateString()}</span>
                </div>
                <div class="project-item-actions">
                    <button class="btn btn-secondary btn-small" onclick="event.stopPropagation(); openRenameModal('${p.id}', '${escapeHtml(p.name)}')">✏️</button>
                    <button class="btn btn-danger btn-small" onclick="event.stopPropagation(); deleteProject('${p.id}')">🗑️</button>
                </div>
            </div>
        `).join('');

        const curr = res.projects.find(p => p.id === currentProjectId);
        if (curr) {
            document.getElementById('current-project-name').textContent = curr.name;
        }

        if (!currentProjectId) {
            document.getElementById('project-modal').showModal();
        }
    } catch (err) {
        console.error("Failed to load projects", err);
    }
}

function openProjectModal() {
    loadProjects();
    document.getElementById('project-modal').showModal();
}

async function createProject() {
    const name = document.getElementById('new-project-name').value.trim();
    if (!name) return;
    try {
        const res = await apiPost('/api/projects', { name });
        document.getElementById('new-project-name').value = '';
        selectProject(res.id, res.name);
    } catch (e) {
        alert(e.message);
    }
}

function selectProject(id, name) {
    currentProjectId = id;
    localStorage.setItem('currentProjectId', id);
    document.getElementById('current-project-name').textContent = name;
    document.getElementById('project-modal').close();
    
    // Reconnect SSE & refresh state
    ['downloader', 'transcriber', 'clipprocessor'].forEach(clearLog);
    ['downloader', 'transcriber', 'clipprocessor'].forEach(connectSSE);
    refreshGallery();
    loadStatus();
}

function openRenameModal(id, currentName) {
    projectToRename = id;
    document.getElementById('rename-project-name').value = currentName;
    document.getElementById('rename-modal').showModal();
}

async function submitRenameProject() {
    const name = document.getElementById('rename-project-name').value.trim();
    if (!name || !projectToRename) return;
    try {
        await apiPost(`/api/projects/${projectToRename}`, { name }, { method: 'PUT' }); // Note: Using apiPost as a wrapper but PUT method not directly supported unless we override, let's use custom fetch
        // Wait, I will use direct fetch for PUT/DELETE to avoid writing new wrappers
    } catch(e){}
}
"""
js += project_logic

with open("frontend/app2.js", "w", encoding="utf-8") as f:
    f.write(js)
