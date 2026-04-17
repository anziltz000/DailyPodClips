/* ═══════════════════════════════════════════════════════════
   DailyPodClips — Frontend JavaScript
   SSE log streaming, API calls, gallery management, Projects
   ═══════════════════════════════════════════════════════════ */

let currentProjectId = localStorage.getItem('currentProjectId') || null;
let projectToRename = null;

// ── SSE Log Connections ──────────────────────────────────────
const sseConnections = {};

function connectSSE(blockId) {
    if (sseConnections[blockId]) {
        sseConnections[blockId].close();
    }
    const logEl = document.getElementById(`log-${blockId}`);
    const pid = currentProjectId || 'default';
    const evtSource = new EventSource(`/api/logs/${pid}/${blockId}`);

    evtSource.addEventListener('log', (e) => {
        appendLog(logEl, e.data);
    });

    evtSource.addEventListener('ping', () => {});

    evtSource.onerror = () => {
        console.warn(`SSE reconnecting for ${blockId}...`);
    };

    sseConnections[blockId] = evtSource;
}

function appendLog(logEl, text) {
    const line = document.createElement('span');
    line.className = 'log-line';

    if (text.startsWith('❌') || text.startsWith('⚠')) {
        line.classList.add(text.startsWith('❌') ? 'error' : 'warn');
    } else if (text.startsWith('✅') || text.startsWith('🎉')) {
        line.classList.add('success');
    }

    line.textContent = text + '\n';
    logEl.appendChild(line);

    const blockId = logEl.id.replace('log-', '');
    const checkbox = document.getElementById(`autoscroll-${blockId}`);
    if (checkbox && checkbox.checked) {
        logEl.scrollTop = logEl.scrollHeight;
    }
}

function clearLog(blockId) {
    const logEl = document.getElementById(`log-${blockId}`);
    if (logEl) logEl.innerHTML = '';
}

// ── API Helpers ──────────────────────────────────────────────
async function apiPost(url, body = {}, method = 'POST') {
    const headers = { 'Content-Type': 'application/json' };
    if (currentProjectId) headers['X-Project-Id'] = currentProjectId;
    const res = await fetch(url, {
        method: method,
        headers: headers,
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
}

async function apiGet(url) {
    const headers = {};
    if (currentProjectId) headers['X-Project-Id'] = currentProjectId;
    const res = await fetch(url, { headers });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
}

async function apiDelete(url) {
    const headers = {};
    if (currentProjectId) headers['X-Project-Id'] = currentProjectId;
    const res = await fetch(url, { method: 'DELETE', headers });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    return data;
}

function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    if (loading) {
        btn.disabled = true;
        btn.classList.add('loading');
    } else {
        btn.disabled = false;
        btn.classList.remove('loading');
    }
}

function setStatus(text, busy = false) {
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    dot.className = busy ? 'status-dot busy' : 'status-dot';
    txt.textContent = text;
}

function notifyUser(title, body) {
    try {
        if ("Notification" in window && Notification.permission === "granted") {
            new Notification(title, { body, icon: '/static/favicon.png' });
        }
    } catch (err) {}
}

// ── PROJECTS ─────────────────────────────────────────────────
async function loadProjects() {
    try {
        const res = await apiGet('/api/projects');
        const list = document.getElementById('project-list');
        if (res.projects.length === 0) {
            list.innerHTML = '<p class="empty-state">No projects found. Create one!</p>';
            const modal = document.getElementById('project-modal');
            if (!currentProjectId && !modal.open) modal.showModal();
            return;
        }
        
        if (currentProjectId && !res.projects.find(p => p.id === currentProjectId)) {
            currentProjectId = null;
            localStorage.removeItem('currentProjectId');
            document.getElementById('current-project-name').textContent = "No Project Selected";
        }

        list.innerHTML = res.projects.map(p => `
            <div class="project-item" onclick="selectProject('${p.id}', '${escapeHtml(p.name)}')">
                <div class="project-item-info">
                    <strong>${p.id === currentProjectId ? '✅ ' : ''}${escapeHtml(p.name)}</strong>
                    <span>Created: ${new Date(p.created_at * 1000).toLocaleDateString()}</span>
                </div>
                <div class="project-item-actions">
                    <button type="button" class="btn btn-secondary btn-small" onclick="event.stopPropagation(); openRenameModal('${p.id}', '${escapeHtml(p.name)}')">✏️</button>
                    <button type="button" class="btn btn-danger btn-small" onclick="event.stopPropagation(); doDeleteProject('${p.id}')">🗑️</button>
                </div>
            </div>
        `).join('');

        const curr = res.projects.find(p => p.id === currentProjectId);
        if (curr) {
            document.getElementById('current-project-name').textContent = curr.name;
        } else {
            document.getElementById('current-project-name').textContent = "No Project Selected";
        }

        const modal = document.getElementById('project-modal');
        if (!currentProjectId && !modal.open) {
            modal.showModal();
        }
    } catch (err) {
        console.error("Failed to load projects", err);
    }
}

function openProjectModal() {
    loadProjects();
    const modal = document.getElementById('project-modal');
    if (!modal.open) modal.showModal();
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
        await apiPost(`/api/projects/${projectToRename}`, { name }, 'PUT');
        document.getElementById('rename-modal').close();
        loadProjects();
    } catch(e) {
        alert(e.message);
    }
}

async function doDeleteProject(id) {
    if (!confirm("Are you sure you want to delete this project and all its files?")) return;
    try {
        await apiDelete(`/api/projects/${id}`);
        if (currentProjectId === id) {
            currentProjectId = null;
            localStorage.removeItem('currentProjectId');
            document.getElementById('current-project-name').textContent = "No Project Selected";
        }
        loadProjects();
    } catch(e) {
        alert(e.message);
    }
}

// ── BLOCK 1: DOWNLOADER ──────────────────────────────────────
async function startDownload() {
    if (!currentProjectId) return alert("Select a project first!");
    const url = document.getElementById('download-url').value.trim();
    if (!url) return alert('Please enter a video URL');
    const cookies = document.getElementById('download-cookies').value.trim();

    clearLog('downloader');
    setLoading('btn-download', true);
    setStatus('Downloading...', true);

    try {
        const result = await apiPost('/api/download', { url, cookies: cookies || null });
        setStatus(`Downloaded: ${result.filename}`);
        notifyUser('Download Complete', result.filename);
        loadStatus();
    } catch (err) {
        setStatus('Download failed');
        appendLog(document.getElementById('log-downloader'), `❌ Error: ${err.message}`);
        notifyUser('Download Failed', err.message);
    } finally {
        setLoading('btn-download', false);
    }
}

// ── BLOCK 2: TRANSCRIBER ─────────────────────────────────────
async function startTranscribe() {
    if (!currentProjectId) return alert("Select a project first!");
    clearLog('transcriber');
    setLoading('btn-transcribe', true);
    setStatus('Transcribing...', true);

    try {
        const result = await apiPost('/api/transcribe');
        setStatus(`Transcribed: ${result.filename}`);
        const previewEl = document.getElementById('transcript-preview');
        const contentEl = document.getElementById('transcript-content');
        previewEl.style.display = 'block';
        contentEl.textContent = result.transcript;
        notifyUser('Transcription Complete', 'Transcript ready.');
        loadStatus();
    } catch (err) {
        setStatus('Transcription failed');
        appendLog(document.getElementById('log-transcriber'), `❌ Error: ${err.message}`);
        notifyUser('Transcription Failed', err.message);
    } finally {
        setLoading('btn-transcribe', false);
    }
}

// ── GDRIVE AUTH ──────────────────────────────────────────────
async function getGDriveAuthUrl() {
    try {
        const result = await apiGet('/api/gdrive/auth-url');
        const box = document.getElementById('gdrive-auth-url-box');
        const link = document.getElementById('gdrive-auth-link');
        box.style.display = 'block';
        link.href = result.auth_url;
        link.textContent = 'Click here to authorize with Google';
    } catch (err) {
        alert(`Error: ${err.message}`);
    }
}

async function submitGDriveCode() {
    const code = document.getElementById('gdrive-code').value.trim();
    if (!code) return alert('Please paste the authorization code');
    try {
        await apiPost('/api/gdrive/auth-code', { code });
        alert('✅ Google Drive authenticated successfully!');
        document.getElementById('gdrive-auth-url-box').style.display = 'none';
    } catch (err) {
        alert(`Auth error: ${err.message}`);
    }
}

async function uploadToGDrive() {
    const folderId = document.getElementById('gdrive-folder-id').value.trim();
    if (!folderId) return alert('Please enter a Google Drive folder ID');
    setStatus('Uploading to GDrive...', true);
    try {
        const result = await apiPost('/api/gdrive/upload', { folder_id: folderId });
        setStatus(`Uploaded ${result.uploaded.length} files`);
        alert(`✅ Uploaded ${result.uploaded.length} file(s) to Google Drive`);
    } catch (err) {
        setStatus('Upload failed');
        alert(`Upload error: ${err.message}`);
    }
}

async function uploadClipsToGDrive() {
    const folderId = document.getElementById('gdrive-clips-folder').value.trim();
    if (!folderId) return alert('Please enter a Google Drive folder ID');
    setStatus('Uploading clips to GDrive...', true);
    try {
        const result = await apiPost('/api/gdrive/upload', { folder_id: folderId });
        setStatus(`Uploaded ${result.uploaded.length} clips`);
        alert(`✅ Uploaded ${result.uploaded.length} clip(s) to Google Drive`);
    } catch (err) {
        setStatus('Upload failed');
        alert(`Upload error: ${err.message}`);
    }
}

// ── BLOCK 3: CLIP PROCESSOR ──────────────────────────────────
function validateJSON() {
    const jsonText = document.getElementById('clip-json').value.trim();
    const statusEl = document.getElementById('json-status');

    if (!jsonText) {
        statusEl.className = 'json-status invalid';
        statusEl.textContent = '❌ JSON field is empty';
        statusEl.style.display = 'block';
        return false;
    }

    try {
        const data = JSON.parse(jsonText);
        if (!Array.isArray(data)) throw new Error('Must be a JSON array');

        let totalSegs = 0;
        for (const clip of data) {
            if (!clip.segments_to_keep) throw new Error(`Clip ${clip.clip_number}: missing segments_to_keep`);
            totalSegs += clip.segments_to_keep.length;
        }

        statusEl.className = 'json-status valid';
        statusEl.textContent = `✅ Valid — ${data.length} clips, ${totalSegs} total segments`;
        statusEl.style.display = 'block';
        return true;
    } catch (err) {
        statusEl.className = 'json-status invalid';
        statusEl.textContent = `❌ ${err.message}`;
        statusEl.style.display = 'block';
        return false;
    }
}

async function startProcessing() {
    if (!currentProjectId) return alert("Select a project first!");
    if (!validateJSON()) return;
    const jsonText = document.getElementById('clip-json').value.trim();

    clearLog('clipprocessor');
    setLoading('btn-process', true);
    setStatus('Processing clips...', true);

    try {
        const result = await apiPost('/api/process-clips', { json_data: jsonText });
        setStatus(`Processed ${result.clips.length} clips`);
        notifyUser('Processing Complete', `Successfully processed ${result.clips.length} clips.`);
        refreshGallery();
        loadStatus();
    } catch (err) {
        setStatus('Processing failed');
        appendLog(document.getElementById('log-clipprocessor'), `❌ Error: ${err.message}`);
        notifyUser('Processing Failed', err.message);
    } finally {
        setLoading('btn-process', false);
    }
}

// ── STOP ─────────────────────────────────────────────────────
async function stopBlock(blockId) {
    if (!currentProjectId) return;
    try {
        await apiPost(`/api/stop/${blockId}`);
    } catch (err) {
        console.error('Stop error:', err);
    }
}

// ── GALLERY ──────────────────────────────────────────────────
async function refreshGallery() {
    if (!currentProjectId) return;
    const grid = document.getElementById('gallery-grid');
    try {
        const result = await apiGet('/api/clips');
        if (!result.clips || result.clips.length === 0) {
            grid.innerHTML = '<p class="empty-state">No clips yet. Process some clips first!</p>';
            return;
        }

        result.clips.sort((a, b) => {
            const scoreA = (a.metadata && a.metadata.virality_score) ? parseInt(a.metadata.virality_score) : 0;
            const scoreB = (b.metadata && b.metadata.virality_score) ? parseInt(b.metadata.virality_score) : 0;
            return scoreB - scoreA;
        });

        grid.innerHTML = result.clips.map(clip => {
            const meta = clip.metadata || {};
            return `
            <div class="gallery-card">
                <video src="${clip.url}&v=${Date.now()}" controls preload="metadata" playsinline></video>
                <div class="gallery-card-info">
                    <h4>${escapeHtml(clip.filename)}</h4>
                    ${meta.virality_score ? `<div class="score">Virality Score: <strong>${escapeHtml(meta.virality_score.toString())}/100</strong></div>` : ''}
                    <p class="meta">${clip.size_mb} MB${meta.estimated_total_seconds ? ` • ${meta.estimated_total_seconds}s` : ''}</p>
                    
                    ${meta.why_chosen ? `<div class="meta-section"><strong>Why Chosen:</strong> ${escapeHtml(meta.why_chosen)}</div>` : ''}
                    ${meta.why_viral ? `<div class="meta-section"><strong>Why Viral:</strong> ${escapeHtml(meta.why_viral)}</div>` : ''}
                    ${meta.audio_tag ? `<div class="meta-section"><strong>Audio Tag:</strong> ${escapeHtml(meta.audio_tag)}</div>` : ''}
                    
                    ${meta.tiktok_caption || meta.instagram_reels || meta.youtube_shorts ? `
                    <details class="social-details">
                        <summary>📱 Social Media Kit</summary>
                        <div class="social-content">
                            ${meta.tiktok_caption ? `<strong>TikTok:</strong><br>${escapeHtml(meta.tiktok_caption)}<br><br>` : ''}
                            ${meta.instagram_reels ? `<strong>Instagram:</strong><br>${escapeHtml(meta.instagram_reels)}<br><br>` : ''}
                            ${meta.youtube_shorts ? `<strong>YouTube:</strong><br>${escapeHtml(meta.youtube_shorts)}` : ''}
                        </div>
                    </details>
                    ` : ''}
                </div>
            </div>
        `}).join('');
    } catch (err) {
        grid.innerHTML = `<p class="empty-state">Error loading clips: ${err.message}</p>`;
    }
}

// ── DANGER ZONE ──────────────────────────────────────────────
async function clearAllData() {
    if (!currentProjectId) return;
    if (!confirm('⚠️ This will delete ALL downloaded videos, processed clips, transcripts, and temp files for this project. Are you sure?')) {
        return;
    }
    try {
        const result = await apiPost('/api/clear-all');
        alert(`✅ Deleted ${result.deleted} files`);
        ['downloader', 'transcriber', 'clipprocessor'].forEach(clearLog);
        document.getElementById('transcript-preview').style.display = 'none';
        refreshGallery();
        setStatus('Idle');
    } catch (err) {
        alert(`Error: ${err.message}`);
    }
}

// ── SETTINGS ─────────────────────────────────────────────────
async function loadSettings() {
    try {
        const result = await apiGet('/api/settings');
        if (result.cookies) document.getElementById('download-cookies').value = result.cookies;
        if (result.gdrive_folder_transcripts) document.getElementById('gdrive-folder-id').value = result.gdrive_folder_transcripts;
        if (result.gdrive_folder_clips) document.getElementById('gdrive-clips-folder').value = result.gdrive_folder_clips;
    } catch (err) {
        console.warn('Failed to load settings:', err);
    }
}

async function saveSettings() {
    const cookies = document.getElementById('download-cookies').value.trim();
    const gdrive_folder_transcripts = document.getElementById('gdrive-folder-id').value.trim();
    const gdrive_folder_clips = document.getElementById('gdrive-clips-folder').value.trim();
    
    try {
        await apiPost('/api/settings', {
            cookies: cookies || null,
            gdrive_folder_transcripts: gdrive_folder_transcripts || null,
            gdrive_folder_clips: gdrive_folder_clips || null
        });
    } catch (err) {}
}

function toggleEdit(inputId, btnId) {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(btnId);
    if (input.disabled) {
        input.disabled = false;
        input.focus();
        btn.textContent = 'Save';
    } else {
        input.disabled = true;
        btn.textContent = 'Edit';
        saveSettings();
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;")
        .replace(/`/g, "&#96;");
}

// ── STATUS SYNC ──────────────────────────────────────────────
async function loadStatus() {
    if (!currentProjectId) return;
    try {
        const status = await apiGet('/api/status');
        if (status.current_video) {
            setStatus(`Loaded: ${status.current_video}`);
        } else {
            setStatus('Idle');
        }
        
        // Sync button loading states
        setLoading('btn-download', status.active_processes.includes('downloader'));
        setLoading('btn-transcribe', status.active_processes.includes('transcriber'));
        setLoading('btn-process', status.active_processes.includes('clipprocessor'));
    } catch(e) {
        setStatus('Server offline', true);
    }
}

// ── Initialize ───────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    try {
        if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission().catch(e => console.warn(e));
        }
    } catch (e) {}

    loadProjects().then(() => {
        if (currentProjectId) {
            ['downloader', 'transcriber', 'clipprocessor'].forEach(connectSSE);
            refreshGallery();
            loadSettings();
            loadStatus();
        }
    });

    // Auto-sync status every 5 seconds
    setInterval(loadStatus, 5000);
});
