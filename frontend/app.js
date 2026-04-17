/* ═══════════════════════════════════════════════════════════
   DailyPodClips — Frontend JavaScript
   SSE log streaming, API calls, gallery management
   ═══════════════════════════════════════════════════════════ */

// ── SSE Log Connections ──────────────────────────────────────
const sseConnections = {};

/**
 * Connect to the SSE endpoint for a block and stream logs into its console.
 */
function connectSSE(blockId) {
    // Close existing connection if any
    if (sseConnections[blockId]) {
        sseConnections[blockId].close();
    }

    const logEl = document.getElementById(`log-${blockId}`);
    const evtSource = new EventSource(`/api/logs/${blockId}`);

    evtSource.addEventListener('log', (e) => {
        appendLog(logEl, e.data);
    });

    evtSource.addEventListener('ping', () => {
        // keepalive — do nothing
    });

    evtSource.onerror = () => {
        // Auto-reconnect is built into EventSource
        console.warn(`SSE reconnecting for ${blockId}...`);
    };

    sseConnections[blockId] = evtSource;
}

/**
 * Append a log line to a console element with auto-scroll and color coding.
 */
function appendLog(logEl, text) {
    const line = document.createElement('span');
    line.className = 'log-line';

    // Color code based on content
    if (text.startsWith('❌') || text.startsWith('⚠')) {
        line.classList.add(text.startsWith('❌') ? 'error' : 'warn');
    } else if (text.startsWith('✅') || text.startsWith('🎉')) {
        line.classList.add('success');
    }

    line.textContent = text + '\n';
    logEl.appendChild(line);

    // Auto-scroll to bottom
    logEl.scrollTop = logEl.scrollHeight;
}

/**
 * Clear a log console.
 */
function clearLog(blockId) {
    const logEl = document.getElementById(`log-${blockId}`);
    if (logEl) logEl.innerHTML = '';
}

// ── API Helpers ──────────────────────────────────────────────
async function apiPost(url, body = {}) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
        throw new Error(data.detail || `Request failed (${res.status})`);
    }
    return data;
}

async function apiGet(url) {
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) {
        throw new Error(data.detail || `Request failed (${res.status})`);
    }
    return data;
}

/**
 * Set a button to loading state.
 */
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

/**
 * Update the header status indicator.
 */
function setStatus(text, busy = false) {
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    dot.className = busy ? 'status-dot busy' : 'status-dot';
    txt.textContent = text;
}

/**
 * Send a browser notification if permission is granted.
 */
function notifyUser(title, body) {
    try {
        if ("Notification" in window && Notification.permission === "granted") {
            new Notification(title, { body, icon: '/static/favicon.png' });
        }
    } catch (err) {
        console.warn("Notification error:", err);
    }
}

// ── BLOCK 1: DOWNLOADER ──────────────────────────────────────
async function startDownload() {
    try {
        if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission().catch(e => console.warn(e));
        }
    } catch (e) {
        console.warn(e);
    }
    
    const url = document.getElementById('download-url').value.trim();
    if (!url) {
        alert('Please enter a video URL');
        return;
    }

    const cookies = document.getElementById('download-cookies').value.trim();

    clearLog('downloader');
    setLoading('btn-download', true);
    setStatus('Downloading...', true);

    try {
        const result = await apiPost('/api/download', { url, cookies: cookies || null });
        setStatus(`Downloaded: ${result.filename}`);
        notifyUser('Download Complete', result.filename);
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
    clearLog('transcriber');
    setLoading('btn-transcribe', true);
    setStatus('Transcribing...', true);

    try {
        const result = await apiPost('/api/transcribe');
        setStatus(`Transcribed: ${result.filename}`);

        // Show transcript preview
        const previewEl = document.getElementById('transcript-preview');
        const contentEl = document.getElementById('transcript-content');
        previewEl.style.display = 'block';
        contentEl.textContent = result.transcript;
        notifyUser('Transcription Complete', 'Transcript ready for processing.');
    } catch (err) {
        setStatus('Transcription failed');
        appendLog(document.getElementById('log-transcriber'), `❌ Error: ${err.message}`);
        notifyUser('Transcription Failed', err.message);
    } finally {
        setLoading('btn-transcribe', false);
    }
}

// ── BLOCK 2: GDRIVE AUTH ─────────────────────────────────────
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
    const code = document.getElementById('gdrive-auth-code').value.trim();
    if (!code) {
        alert('Please paste the authorization code');
        return;
    }

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
    if (!folderId) {
        alert('Please enter a Google Drive folder ID');
        return;
    }

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
    if (!folderId) {
        alert('Please enter a Google Drive folder ID');
        return;
    }

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
        alert('JSON field is empty. Please paste the AI output first.');
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
        alert(`JSON Validation Error:\n${err.message}`);
        return false;
    }
}

async function startProcessing() {
    try {
        if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission().catch(e => console.warn(e));
        }
    } catch (e) {
        console.warn(e);
    }
    
    if (!validateJSON()) return;
    
    const jsonText = document.getElementById('clip-json').value.trim();

    clearLog('clipprocessor');
    setLoading('btn-process', true);
    setStatus('Processing clips...', true);

    try {
        const result = await apiPost('/api/process-clips', { json_data: jsonText });
        setStatus(`Processed ${result.clips.length} clips`);
        notifyUser('Processing Complete', `Successfully processed ${result.clips.length} clips.`);
        // Auto-refresh gallery
        await refreshGallery();
    } catch (err) {
        setStatus('Processing failed');
        appendLog(document.getElementById('log-clipprocessor'), `❌ Error: ${err.message}`);
        notifyUser('Processing Failed', err.message);
    } finally {
        setLoading('btn-process', false);
    }
}

// ── BLOCK: STOP ──────────────────────────────────────────────
async function stopBlock(blockId) {
    try {
        await apiPost(`/api/stop/${blockId}`);
    } catch (err) {
        console.error('Stop error:', err);
    }
}

// ── BLOCK 4: GALLERY ─────────────────────────────────────────
async function refreshGallery() {
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
                <video src="${clip.url}?v=${Date.now()}" controls preload="metadata" playsinline></video>
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
    if (!confirm('⚠️ This will delete ALL downloaded videos, processed clips, transcripts, and temp files. Are you sure?')) {
        return;
    }

    try {
        const result = await apiPost('/api/clear-all');
        alert(`✅ Deleted ${result.deleted} files`);
        // Clear all log consoles
        ['downloader', 'transcriber', 'clipprocessor'].forEach(clearLog);
        // Clear transcript preview
        document.getElementById('transcript-preview').style.display = 'none';
        // Refresh gallery
        await refreshGallery();
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
        console.log('Settings saved automatically.');
    } catch (err) {
        console.error('Failed to save settings:', err);
    }
}

// ── Utils ────────────────────────────────────────────────────
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
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

// ── Initialize ───────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Request notification permission safely
    try {
        if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission().catch(e => console.warn(e));
        }
    } catch (e) {
        console.warn("Notifications init error:", e);
    }

    // Connect SSE for all blocks
    ['downloader', 'transcriber', 'clipprocessor'].forEach(connectSSE);

    // Load gallery on startup
    refreshGallery();

    // Load persistent settings
    loadSettings();

    // Check server status
    apiGet('/api/status').then(status => {
        if (status.current_video) {
            setStatus(`Loaded: ${status.current_video}`);
        }
        if (status.gdrive_authenticated) {
            console.log('GDrive: authenticated');
        }
    }).catch(() => {
        setStatus('Server offline', true);
    });
});
