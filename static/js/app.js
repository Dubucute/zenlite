/**
 * ZenLite Dashboard — Frontend Logic
 */

// ── State ───────────────────────────────────────────────────────────────────
let allModels = [];
let allProviders = [];
let lastLogSeq = 0;
let logPaused = false;

// ── Initialization ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    loadStatus();
    loadProviders();
    loadModels();
    loadLogs();

    // Log viewer controls
    document.getElementById('logClearBtn').addEventListener('click', () => {
        const view = document.getElementById('logView');
        view.querySelectorAll('.log-line').forEach(n => n.remove());
        lastLogSeq = 0;
        showLogEmpty();
    });
    document.getElementById('logAutoscroll').addEventListener('change', (e) => {
        if (e.target.checked) scrollLogsToBottom();
    });

    // Auto-refresh stats every 5 seconds, logs every 2 seconds
    setInterval(loadStatus, 5000);
    setInterval(loadLogs, 2000);
});

// ── API Calls ───────────────────────────────────────────────────────────────

async function loadStatus() {
    try {
        const [statusRes, proxyRes] = await Promise.all([
            fetch('/dashboard/status'),
            fetch('/dashboard/proxy-stats'),
        ]);
        const status = await statusRes.json();
        const proxy = await proxyRes.json();

        // Update status dot
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        dot.className = 'status-dot online';
        text.textContent = 'Online';

        // Update stats
        document.getElementById('uptime').textContent = formatUptime(status.uptime_seconds);
        document.getElementById('totalRequests').textContent = proxy.total_requests || 0;
        document.getElementById('directHits').textContent = proxy.direct_successes || 0;
        document.getElementById('proxyHits').textContent = proxy.proxy_successes || 0;
        document.getElementById('rateLimitRetries').textContent = proxy.rate_limited_retries || 0;
        document.getElementById('retries').textContent = proxy.retries || 0;
        document.getElementById('proxyFailures').textContent = proxy.proxy_failures || 0;
        document.getElementById('failures').textContent = proxy.failures || 0;
        document.getElementById('proxyPool').textContent = proxy.pool_size || 0;
        document.getElementById('freeProxies').textContent = proxy.free_proxies_count || 0;
    } catch (e) {
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        dot.className = 'status-dot offline';
        text.textContent = 'Offline';
    }
}

async function loadProviders() {
    try {
        const res = await fetch('/dashboard/providers');
        const data = await res.json();
        allProviders = data.providers;
        renderProviders(data.providers);
    } catch (e) {
        document.getElementById('providersList').textContent = 'Failed to load providers';
    }
}

async function loadModels() {
    try {
        const res = await fetch('/v1/models');
        const data = await res.json();
        allModels = data.data || [];
        renderModels(allModels);
        populateModelSelect(allModels);
    } catch (e) {
        document.getElementById('modelsList').textContent = 'Failed to load models';
    }
}

// ── Render Functions ────────────────────────────────────────────────────────

function renderProviders(providers) {
    const container = document.getElementById('providersList');
    container.innerHTML = providers.map(p => `
        <div class="provider-card">
            <div class="provider-name">${escapeHtml(p.name)}</div>
            <span class="provider-auth ${p.auth_type === 'none' ? 'no-auth' : 'api-key'}">
                ${p.auth_type === 'none' ? '🔓 No Auth Required' : '🔑 API Key Required'}
            </span>
            <div class="provider-desc">${escapeHtml(p.description)}</div>
            <div class="provider-models">
                ${p.models.map(m => `<span class="model-tag">${escapeHtml(m)}</span>`).join('')}
            </div>
        </div>
    `).join('');
}

function renderModels(models) {
    const container = document.getElementById('modelsList');
    container.innerHTML = models.map(m => `
        <div class="model-item">
            <div class="model-item-name">${escapeHtml(m.id)}</div>
            <div class="model-item-provider">${escapeHtml(m.owned_by)}</div>
        </div>
    `).join('');
}

function populateModelSelect(models) {
    const select = document.getElementById('modelSelect');
    select.innerHTML = models.map(m =>
        `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)} (${escapeHtml(m.owned_by)})</option>`
    ).join('');
}

// ── Quick Test ──────────────────────────────────────────────────────────────

async function sendTest() {
    const provider = document.getElementById('providerSelect').value;
    const apiKey = document.getElementById('apiKeyInput').value.trim();
    const model = document.getElementById('modelSelect').value;
    const message = document.getElementById('messageInput').value.trim();
    const stream = document.getElementById('streamToggle').checked;
    const sendBtn = document.getElementById('sendBtn');
    const responseEl = document.getElementById('responseText');

    if (!message) {
        responseEl.textContent = 'Please enter a message.';
        return;
    }

    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending...';
    responseEl.textContent = '';

    const headers = { 'Content-Type': 'application/json' };
    if (apiKey) {
        headers['Authorization'] = `Bearer ${apiKey}`;
    }

    const body = {
        model: model,
        messages: [{ role: 'user', content: message }],
        stream: stream,
        provider: provider,
    };

    try {
        if (stream) {
            await sendStreamTest(headers, body, responseEl);
        } else {
            const res = await fetch('/v1/chat/completions', {
                method: 'POST',
                headers,
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (data.choices && data.choices.length > 0) {
                responseEl.textContent = data.choices[0].message?.content || JSON.stringify(data, null, 2);
            } else {
                responseEl.textContent = JSON.stringify(data, null, 2);
            }
        }
    } catch (e) {
        responseEl.textContent = `Error: ${e.message}`;
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send Request';
    }
}

async function sendStreamTest(headers, body, responseEl) {
    const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullResponse = '';

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || !trimmed.startsWith('data: ')) continue;
            const data = trimmed.slice(6);
            if (data === '[DONE]') continue;

            try {
                const chunk = JSON.parse(data);
                const delta = chunk.choices?.[0]?.delta?.content;
                if (delta) {
                    fullResponse += delta;
                    responseEl.textContent = fullResponse;
                    responseEl.scrollTop = responseEl.scrollHeight;
                }
            } catch (e) {
                // Skip unparseable chunks
            }
        }
    }

    if (!fullResponse) {
        responseEl.textContent = '(empty response)';
    }
}

// ── Live Logs ───────────────────────────────────────────────────────────────

async function loadLogs() {
    try {
        const res = await fetch(`/dashboard/logs?limit=200&after=${lastLogSeq}`);
        const data = await res.json();
        const logs = data.logs || [];
        if (!logs.length) return;

        const view = document.getElementById('logView');
        const autoscroll = document.getElementById('logAutoscroll').checked;
        const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;

        logs.forEach(log => {
            lastLogSeq = Math.max(lastLogSeq, log.seq);
            view.appendChild(renderLogLine(log));
        });

        const empty = document.getElementById('logEmpty');
        if (empty) empty.style.display = 'none';

        // Prune old log lines so the DOM doesn't grow forever (keep #logEmpty)
        const lines = view.querySelectorAll('.log-line');
        for (let i = 0; i < lines.length - 400; i++) lines[i].remove();

        if (autoscroll || atBottom) scrollLogsToBottom();
    } catch (e) {
        // Server unreachable — try again next tick
    }
}

function renderLogLine(log) {
    const line = document.createElement('div');
    line.className = `log-line log-${(log.level || 'info').toLowerCase()}`;

    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = log.time || '';

    const level = document.createElement('span');
    level.className = 'log-level';
    level.textContent = (log.level || 'INFO').toUpperCase();

    const logger = document.createElement('span');
    logger.className = 'log-logger';
    logger.textContent = (log.logger || '').replace('zenlite', 'zl');

    const msg = document.createElement('span');
    msg.className = 'log-msg';
    msg.textContent = log.message || '';

    line.append(time, level, logger, msg);
    return line;
}

function showLogEmpty() {
    const el = document.getElementById('logEmpty');
    if (el) el.style.display = 'block';
}

function scrollLogsToBottom() {
    const view = document.getElementById('logView');
    view.scrollTop = view.scrollHeight;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
