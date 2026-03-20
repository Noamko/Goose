/* ================================================================
   Goose Dashboard — Frontend Application
   ================================================================ */

'use strict';

// ---------------------------------------------------------------------------
// API Client
// ---------------------------------------------------------------------------

const API = {
  async _fetch(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch('/api' + path, opts);
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || r.statusText);
    }
    return r.json();
  },
  get: (path) => API._fetch('GET', path),
  post: (path, body) => API._fetch('POST', path, body),
  put: (path, body) => API._fetch('PUT', path, body),
  delete: (path) => API._fetch('DELETE', path),
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  templates: [],
  runs: [],
  tools: [],
  activeTemplateId: null,
  activeRunId: null,
  chatOpen: false,
  chatMessages: [],          // [{role, content, action?}] — in-memory history
  pendingInputCallId: null,  // call_id of the current ask_user pause
};

// run_id -> WebSocket
const openSockets = {};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return ''; }
}

function statusClass(status) { return 'status-' + (status || 'pending'); }

function statusLabel(status) {
  const map = {
    pending: 'Pending', running: 'Running',
    waiting_for_user: 'Waiting', completed: 'Done',
    failed: 'Failed', cancelled: 'Cancelled',
  };
  return map[status] || status;
}

// ---------------------------------------------------------------------------
// Sidebar rendering
// ---------------------------------------------------------------------------

function renderTemplatesList() {
  const el = document.getElementById('templates-list');
  const q = (document.getElementById('agent-search')?.value || '').toLowerCase();
  const filtered = state.templates.filter(t =>
    !q || t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q)
  );
  if (!filtered.length) {
    el.innerHTML = `<div class="empty-hint">${q ? 'No matches.' : 'No agents yet. Create one →'}</div>`;
    return;
  }
  el.innerHTML = filtered.map(t => `
    <div class="sidebar-item ${state.activeTemplateId === t.id ? 'active' : ''}"
         onclick="App.showTemplate('${t.id}')">
      <div class="sidebar-item-name">${t.pinned ? '★ ' : ''}${esc(t.name)}</div>
      <div class="sidebar-item-meta">${esc(t.description || 'No description')}</div>
      <div class="sidebar-item-actions" onclick="event.stopPropagation()">
        <button class="sidebar-action-btn pin ${t.pinned ? 'pinned' : ''}"
          title="${t.pinned ? 'Unpin' : 'Pin'}"
          onclick="App.togglePin('${t.id}', ${!t.pinned})">★</button>
        <button class="sidebar-action-btn run" title="Quick run"
          onclick="App.startRunModal('${t.id}')">▶</button>
        <button class="sidebar-action-btn del" title="Delete"
          onclick="App.deleteTemplate('${t.id}')">✕</button>
      </div>
    </div>
  `).join('');
}

function renderRunsList() {
  const el = document.getElementById('runs-list');
  const q = (document.getElementById('run-search')?.value || '').toLowerCase();
  const filtered = state.runs.filter(r =>
    !q || (r.user_goal || '').toLowerCase().includes(q) || (r.template_name || '').toLowerCase().includes(q)
  );
  if (!filtered.length) {
    el.innerHTML = `<div class="empty-hint">${q ? 'No matches.' : 'No runs yet.'}</div>`;
    return;
  }
  el.innerHTML = filtered.slice(0, 30).map(r => `
    <div class="sidebar-item ${state.activeRunId === r.id ? 'active' : ''}"
         onclick="App.showRun('${r.id}')">
      <div class="sidebar-item-name">${esc(r.template_name || 'Ad-hoc')}</div>
      <div class="sidebar-item-meta">
        <span class="status-badge ${statusClass(r.status)}">${statusLabel(r.status)}</span>
        <span>${esc((r.user_goal || '').slice(0, 30))}${r.user_goal?.length > 30 ? '…' : ''}</span>
      </div>
    </div>
  `).join('');
}

// ---------------------------------------------------------------------------
// Template detail view
// ---------------------------------------------------------------------------

function renderTemplateDetail(t) {
  document.getElementById('td-name').textContent = t.name;
  document.getElementById('td-description').textContent = t.description || '';
  document.getElementById('td-system-prompt').textContent = t.system_prompt;
  document.getElementById('td-model-badge').textContent = t.model || 'gpt-4o';

  const pinBtn = document.getElementById('td-btn-pin');
  pinBtn.textContent = t.pinned ? '★ Unpin' : '☆ Pin';

  const toolsEl = document.getElementById('td-tools');
  const tools = t.allowed_tools || [];
  toolsEl.innerHTML = ['ask_user (always)', ...tools].map(name =>
    `<span class="tag tag-tool">${esc(name)}</span>`
  ).join('');

  const secretsEl = document.getElementById('td-secrets');
  const keys = t.secret_keys || [];
  if (keys.length === 0) {
    secretsEl.innerHTML = '<span class="text-secondary text-sm">None stored</span>';
  } else {
    secretsEl.innerHTML = keys.map(k =>
      `<span class="tag tag-secret">&#128273; ${esc(k)}</span>`
    ).join('');
  }
}

async function renderTemplateSchedules(tid) {
  const el = document.getElementById('td-schedules');
  let schedules = [];
  try { schedules = await API.get(`/templates/${tid}/schedules`); } catch {}
  if (!schedules.length) {
    el.innerHTML = '<div class="text-secondary text-sm">No schedules.</div>';
    return;
  }
  el.innerHTML = schedules.map(s => {
    const interval = formatInterval(s.interval_minutes);
    const next = s.next_run ? new Date(s.next_run).toLocaleString() : '—';
    return `
      <div class="schedule-item ${s.enabled ? '' : 'disabled'}" id="sched-${s.id}">
        <span class="schedule-goal" title="${esc(s.goal)}">${esc(s.goal)}</span>
        <span class="schedule-interval">${interval}</span>
        <span class="schedule-next">next: ${next}</span>
        <button class="schedule-toggle ${s.enabled ? 'active' : ''}"
          onclick="App.toggleSchedule('${s.id}', ${!s.enabled}, '${tid}')">
          ${s.enabled ? 'ON' : 'OFF'}
        </button>
        <button class="sidebar-action-btn del" onclick="App.deleteSchedule('${s.id}', '${tid}')">✕</button>
      </div>
    `;
  }).join('');
}

function formatInterval(minutes) {
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${minutes / 60}h`;
  return `${minutes / 1440}d`;
}

// ---------------------------------------------------------------------------
// Run view
// ---------------------------------------------------------------------------

function appendLogEntry(logEl, event) {
  const ts = fmtTime(event.timestamp);
  let html = '';
  const t = event.type;

  if (t === 'agent_log') {
    html = `
      <div class="log-entry log-type-agent_log">
        <span class="log-ts">${ts}</span>
        <span class="log-content">${esc(event.content)}</span>
      </div>`;

  } else if (t === 'tool_call_start') {
    const argsStr = JSON.stringify(event.arguments || {}, null, 0);
    const preview = argsStr.length > 120 ? argsStr.slice(0, 120) + '…' : argsStr;
    html = `
      <div class="log-entry log-type-tool_call_start">
        <span class="log-ts">${ts}</span>
        <span class="log-content">
          <span class="log-tool-badge badge-tool">TOOL</span>
          ${esc(event.tool_name)} <span style="color:var(--text3)">${esc(preview)}</span>
        </span>
      </div>`;

  } else if (t === 'tool_call_result') {
    const cls = event.status === 'error' ? 'status-error' : 'status-success';
    const badge = event.status === 'error' ? 'badge-error' : 'badge-result';
    const label = event.status === 'error' ? 'ERROR' : 'RESULT';
    html = `
      <div class="log-entry log-type-tool_call_result ${cls}">
        <span class="log-ts">${ts}</span>
        <span class="log-content">
          <span class="log-tool-badge ${badge}">${label}</span>
          ${esc(event.tool_name)}: ${esc((event.result || '').slice(0, 300))}
        </span>
      </div>`;

  } else if (t === 'user_input_required') {
    html = `
      <div class="log-entry log-type-user_input_required">
        <span class="log-ts">${ts}</span>
        <span class="log-content">
          <span class="log-tool-badge badge-ask">ASK</span>
          ${esc(event.question)}
        </span>
      </div>`;

  } else if (t === 'run_complete') {
    html = `
      <hr class="log-separator" />
      <div class="log-entry log-type-run_complete">
        <span class="log-ts">${ts}</span>
        <span class="log-content">
          <span class="log-tool-badge badge-done">DONE</span>
          ${esc(event.content || 'Task completed.')}
        </span>
      </div>`;

  } else if (t === 'error') {
    html = `
      <div class="log-entry log-type-error">
        <span class="log-ts">${ts}</span>
        <span class="log-content">⚠ ${esc(event.content || event.message || 'Error')}</span>
      </div>`;

  } else if (t === 'status_change') {
    // Don't render status_change as a log line — just update the badge
  }

  if (html) {
    logEl.insertAdjacentHTML('beforeend', html);
    logEl.scrollTop = logEl.scrollHeight;
  }
}

function setRunStatus(status) {
  const badge = document.getElementById('rv-status');
  badge.className = `status-badge ${statusClass(status)}`;
  badge.textContent = statusLabel(status);

  const cancelBtn = document.getElementById('rv-btn-cancel');
  const followupWidget = document.getElementById('rv-followup-widget');

  if (status === 'running' || status === 'waiting_for_user') {
    cancelBtn.classList.remove('hidden');
    followupWidget.classList.add('hidden');
  } else {
    cancelBtn.classList.add('hidden');
    followupWidget.classList.remove('hidden');
    document.getElementById('rv-followup-input').focus();
  }
}

function updateTokenBadge(tokenUsage) {
  const el = document.getElementById('rv-tokens');
  if (!tokenUsage || !el) return;
  try {
    const u = typeof tokenUsage === 'string' ? JSON.parse(tokenUsage) : tokenUsage;
    if (u && u.total) {
      el.textContent = `${u.total.toLocaleString()} tokens`;
      el.classList.remove('hidden');
    }
  } catch {}
}

async function sendFollowup() {
  const input = document.getElementById('rv-followup-input');
  const message = input.value.trim();
  if (!message || !state.activeRunId) return;

  input.value = '';
  document.getElementById('rv-followup-widget').classList.add('hidden');

  const logEl = document.getElementById('rv-log');
  const ts = fmtTime(new Date().toISOString());
  logEl.insertAdjacentHTML('beforeend', `
    <hr class="log-separator" />
    <div class="log-entry">
      <span class="log-ts">${ts}</span>
      <span class="log-content" style="color:var(--text2)">You: ${esc(message)}</span>
    </div>
  `);
  logEl.scrollTop = logEl.scrollHeight;

  try {
    await API.post(`/runs/${state.activeRunId}/continue`, { message });
  } catch (e) {
    alert('Error: ' + e.message);
    document.getElementById('rv-followup-widget').classList.remove('hidden');
  }
}

function showInputWidget(event) {
  state.pendingInputCallId = event.call_id;
  const widget = document.getElementById('rv-input-widget');
  document.getElementById('rv-question').textContent = event.question;
  const inputEl = document.getElementById('rv-input-field');
  inputEl.type = event.input_type === 'password' ? 'password' : 'text';
  inputEl.value = '';
  document.getElementById('rv-save-check').checked = false;
  document.getElementById('rv-save-key').style.display = 'none';
  document.getElementById('rv-save-key').value = '';
  widget.classList.remove('hidden');
  inputEl.focus();
}

function hideInputWidget() {
  document.getElementById('rv-input-widget').classList.add('hidden');
  state.pendingInputCallId = null;
}

function sendUserInput() {
  const callId = state.pendingInputCallId;
  if (!callId || !state.activeRunId) return;

  const value = document.getElementById('rv-input-field').value;
  const saveCheck = document.getElementById('rv-save-check').checked;
  const saveKey = document.getElementById('rv-save-key').value.trim() || `secret_${callId.slice(0, 8)}`;

  const ws = openSockets[state.activeRunId];
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    alert('WebSocket not connected. Please refresh.');
    return;
  }

  ws.send(JSON.stringify({
    type: 'user_input',
    call_id: callId,
    value: value,
    save_to_template: saveCheck,
    save_key: saveKey,
  }));

  // Log the response in the UI
  const logEl = document.getElementById('rv-log');
  const ts = fmtTime(new Date().toISOString());
  const displayValue = document.getElementById('rv-input-field').type === 'password'
    ? '••••••••'
    : esc(value.slice(0, 60)) + (value.length > 60 ? '…' : '');
  logEl.insertAdjacentHTML('beforeend', `
    <div class="log-entry">
      <span class="log-ts">${ts}</span>
      <span class="log-content" style="color:var(--text2)">You: ${displayValue}</span>
    </div>
  `);
  logEl.scrollTop = logEl.scrollHeight;

  hideInputWidget();
}

// ---------------------------------------------------------------------------
// WebSocket management
// ---------------------------------------------------------------------------

function connectRunSocket(runId) {
  // Close existing socket for this run if any
  if (openSockets[runId]) {
    try { openSockets[runId].close(); } catch (_) {}
    delete openSockets[runId];
  }

  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${location.host}/ws/${runId}`);
  openSockets[runId] = ws;

  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }

    // Only process events for the currently viewed run
    if (runId !== state.activeRunId) return;

    const logEl = document.getElementById('rv-log');

    if (msg.type === 'history') {
      // Replay past events
      logEl.innerHTML = '';
      for (const ev of (msg.events || [])) {
        appendLogEntry(logEl, ev);
        if (ev.type === 'status_change') setRunStatus(ev.status);
      }
      // Determine if we're waiting for input (last event was user_input_required and no response yet)
      const lastInputReq = [...(msg.events || [])].reverse().find(ev => ev.type === 'user_input_required');
      const lastStatus = [...(msg.events || [])].reverse().find(ev => ev.type === 'status_change');
      if (lastStatus && lastStatus.status === 'waiting_for_user' && lastInputReq) {
        showInputWidget(lastInputReq);
      }
      return;
    }

    appendLogEntry(logEl, msg);

    if (msg.type === 'status_change') {
      setRunStatus(msg.status);
      loadRuns();
    }

    if (msg.type === 'widget_update') {
      // If dashboard is open, refresh it
      if (!document.getElementById('dashboard-view').classList.contains('hidden')) {
        Dashboard.load();
      }
    }

    if (msg.type === 'user_input_required') {
      showInputWidget(msg);
    }

    if (msg.type === 'run_complete' || msg.type === 'status_change') {
      if (['completed', 'failed', 'cancelled'].includes(msg.status || msg.type === 'run_complete' ? 'completed' : '')) {
        hideInputWidget();
      }
    }
  };

  ws.onclose = () => {
    delete openSockets[runId];
    // Attempt reconnect if run is active
    if (runId === state.activeRunId) {
      setTimeout(() => {
        // Only reconnect if still viewing this run and it might still be active
        if (runId === state.activeRunId) connectRunSocket(runId);
      }, 3000);
    }
  };

  ws.onerror = () => {};
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

const Dashboard = {
  async show() {
    state.activeTemplateId = null;
    state.activeRunId = null;
    hideAll();
    document.getElementById('dashboard-view').classList.remove('hidden');
    await Dashboard.load();
  },

  async load() {
    let widgets = [];
    try { widgets = await API.get('/widgets'); } catch {}
    const grid = document.getElementById('widget-grid');
    if (!widgets.length) {
      grid.innerHTML = '<div class="dashboard-empty">No widgets yet. Ask an agent to use <code>set_dashboard_widget</code> to display results here.</div>';
      return;
    }
    grid.innerHTML = widgets.map(w => Dashboard._renderCard(w)).join('');
  },

  _renderCard(w) {
    const updated = w.updated_at ? new Date(w.updated_at).toLocaleString() : '';
    const source = w.template_name ? `by ${esc(w.template_name)}` : '';
    return `
      <div class="widget-card">
        <div class="widget-card-header">
          <span class="widget-card-title">${esc(w.title)}</span>
          <span class="widget-card-meta">${source}</span>
          <button class="widget-card-delete" onclick="Dashboard.remove('${w.id}')" title="Remove widget">✕</button>
        </div>
        ${Dashboard._renderBody(w)}
      </div>
    `;
  },

  _renderBody(w) {
    const d = w.data || {};
    switch (w.widget_type) {
      case 'metric':
        return `<div class="widget-metric">
          <div class="widget-metric-value">${esc(String(d.value ?? '—'))}</div>
          <div class="widget-metric-label">${esc(d.label ?? '')}</div>
          ${d.sublabel ? `<div class="widget-metric-sublabel">${esc(d.sublabel)}</div>` : ''}
        </div>`;

      case 'list':
        return `<div class="widget-list">${(d.items || []).map(item =>
          `<div class="widget-list-item">${esc(String(item))}</div>`
        ).join('')}</div>`;

      case 'table': {
        const cols = d.columns || [];
        const rows = d.rows || [];
        return `<div class="widget-table-wrap"><table class="widget-table">
          <thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>
          <tbody>${rows.map(row =>
            `<tr>${(Array.isArray(row) ? row : [row]).map(cell => `<td>${esc(String(cell))}</td>`).join('')}</tr>`
          ).join('')}</tbody>
        </table></div>`;
      }

      case 'status':
        return `<div class="widget-status-list">${(d.items || []).map(item => {
          const s = item.status || 'up';
          return `<div class="widget-status-item">
            <span class="widget-status-dot dot-${s}"></span>
            <span class="widget-status-name">${esc(item.name)}</span>
            <span class="status-label-${s}">${s.toUpperCase()}</span>
          </div>`;
        }).join('')}</div>`;

      default: // text
        return `<div class="widget-text">${esc(String(d.content ?? ''))}</div>`;
    }
  },

  async remove(id) {
    try {
      await API.delete(`/widgets/${id}`);
      await Dashboard.load();
    } catch (e) {
      alert('Error: ' + e.message);
    }
  },
};

// ---------------------------------------------------------------------------
// Chat (Goose meta-agent)
// ---------------------------------------------------------------------------

const Chat = {
  show() {
    state.activeTemplateId = null;
    state.activeRunId = null;
    state.chatOpen = true;
    renderTemplatesList();
    renderRunsList();
    hideAll();
    document.getElementById('chat-view').classList.remove('hidden');
    state.chatOpen = true;
    Chat._render();
    document.getElementById('chat-input').focus();
  },

  clear() {
    state.chatMessages = [];
    Chat._render();
  },

  _render() {
    const container = document.getElementById('chat-messages');
    container.innerHTML = '';
    for (const msg of state.chatMessages) {
      container.appendChild(Chat._buildBubble(msg));
    }
    container.scrollTop = container.scrollHeight;
  },

  _buildBubble(msg) {
    const div = document.createElement('div');
    div.className = `chat-msg ${msg.role}`;

    const avatar = document.createElement('div');
    avatar.className = 'chat-avatar';
    avatar.textContent = msg.role === 'assistant' ? 'G' : 'U';

    const wrap = document.createElement('div');
    wrap.className = 'chat-bubble-wrap';

    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.textContent = msg.content;
    wrap.appendChild(bubble);

    if (msg.action && msg.action.type === 'agent_created') {
      const card = document.createElement('div');
      card.className = 'chat-action-card';
      card.innerHTML = `&#10003; Agent <strong>${esc(msg.action.name)}</strong> created &mdash; click to view`;
      card.onclick = () => App.showTemplate(msg.action.id);
      wrap.appendChild(card);
    }

    div.appendChild(avatar);
    div.appendChild(wrap);
    return div;
  },

  _showThinking() {
    const container = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = 'chat-msg assistant';
    div.id = 'chat-thinking';
    const avatar = document.createElement('div');
    avatar.className = 'chat-avatar';
    avatar.textContent = 'G';
    const wrap = document.createElement('div');
    wrap.className = 'chat-bubble-wrap';
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.innerHTML = '<div class="chat-thinking"><span></span><span></span><span></span></div>';
    wrap.appendChild(bubble);
    div.appendChild(avatar);
    div.appendChild(wrap);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  },

  _hideThinking() {
    const el = document.getElementById('chat-thinking');
    if (el) el.remove();
  },

  async send() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    const sendBtn = document.getElementById('chat-send-btn');
    sendBtn.disabled = true;
    input.value = '';
    input.style.height = 'auto';

    state.chatMessages.push({ role: 'user', content: text });
    Chat._render();
    Chat._showThinking();

    // Only send role+content to the API
    const apiMessages = state.chatMessages.map(m => ({ role: m.role, content: m.content }));

    try {
      const result = await API.post('/chat', { messages: apiMessages });
      Chat._hideThinking();
      const replyMsg = { role: 'assistant', content: result.reply, action: result.action || null };
      state.chatMessages.push(replyMsg);
      Chat._render();

      if (result.action && result.action.type === 'agent_created') {
        await loadTemplates();
        renderTemplatesList();
      }
    } catch (e) {
      Chat._hideThinking();
      state.chatMessages.push({ role: 'assistant', content: `Sorry, something went wrong: ${e.message}` });
      Chat._render();
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  },
};

// ---------------------------------------------------------------------------
// Main App object
// ---------------------------------------------------------------------------

const App = {
  // Show template detail
  showTemplate(id) {
    state.activeTemplateId = id;
    state.activeRunId = null;
    renderTemplatesList();

    const t = state.templates.find(x => x.id === id);
    if (!t) return;

    hideAll();
    document.getElementById('template-detail').classList.remove('hidden');
    renderTemplateDetail(t);

    // Wire buttons
    document.getElementById('td-btn-run').onclick = () => App.startRunModal(id);
    document.getElementById('td-btn-edit').onclick = () => App.editTemplateModal(id);
    document.getElementById('td-btn-delete').onclick = () => App.deleteTemplate(id);
    document.getElementById('td-btn-pin').onclick = () => App.togglePin(id, !t.pinned);
    document.getElementById('td-btn-export').onclick = () => App.exportTemplate(id);
    document.getElementById('td-sched-add-btn').onclick = () => App.addSchedule(id);
    renderTemplateSchedules(id);
  },

  // Show run
  async showRun(id) {
    state.activeRunId = id;
    state.activeTemplateId = null;
    renderRunsList();

    const run = state.runs.find(r => r.id === id);
    if (!run) return;

    hideAll();
    document.getElementById('run-view').classList.remove('hidden');

    document.getElementById('rv-template-name').textContent = run.template_name || 'Ad-hoc';
    document.getElementById('rv-goal').textContent = run.user_goal || '';
    document.getElementById('rv-log').innerHTML = '';
    document.getElementById('rv-tokens').classList.add('hidden');
    hideInputWidget();
    document.getElementById('rv-followup-widget').classList.add('hidden');
    setRunStatus(run.status);
    updateTokenBadge(run.token_usage);

    // Wire cancel button
    document.getElementById('rv-btn-cancel').onclick = () => App.cancelRun(id);

    // Connect WebSocket (will replay history)
    connectRunSocket(id);
  },

  // New template modal
  newTemplateModal() {
    document.getElementById('modal-template-title').textContent = 'New Agent';
    document.getElementById('mt-name').value = '';
    document.getElementById('mt-description').value = '';
    document.getElementById('mt-system-prompt').value = '';
    document.getElementById('mt-model').value = 'gpt-4o';
    document.getElementById('mt-secrets-list').innerHTML = '';
    renderToolCheckboxes(null);
    document.getElementById('mt-btn-save').onclick = () => App.saveTemplate(null);
    openModal('modal-template');
  },

  // Edit template modal
  async editTemplateModal(id) {
    let t = state.templates.find(x => x.id === id);
    if (!t) {
      try { t = await API.get(`/templates/${id}`); } catch { return; }
    }
    // Fetch full detail including secret_keys
    try {
      const full = await API.get(`/templates/${id}`);
      Object.assign(t, full);
    } catch {}

    document.getElementById('modal-template-title').textContent = 'Edit Agent';
    document.getElementById('mt-name').value = t.name || '';
    document.getElementById('mt-description').value = t.description || '';
    document.getElementById('mt-system-prompt').value = t.system_prompt || '';
    document.getElementById('mt-model').value = t.model || 'gpt-4o';
    document.getElementById('mt-secrets-list').innerHTML = '';
    renderToolCheckboxes(t.allowed_tools || []);

    // Show existing secret keys as read-only rows
    for (const key of (t.secret_keys || [])) {
      addSecretRow(key, '', true);
    }

    document.getElementById('mt-btn-save').onclick = () => App.saveTemplate(id);
    openModal('modal-template');
  },

  // Save template (create or update)
  async saveTemplate(id) {
    const name = document.getElementById('mt-name').value.trim();
    const description = document.getElementById('mt-description').value.trim();
    const system_prompt = document.getElementById('mt-system-prompt').value.trim();
    if (!name || !system_prompt) {
      alert('Name and System Prompt are required.');
      return;
    }

    const model = document.getElementById('mt-model').value || 'gpt-4o';
    const allowed_tools = getCheckedTools();
    const secrets = getSecretsFromForm();

    try {
      if (id) {
        await API.put(`/templates/${id}`, { name, description, system_prompt, model, allowed_tools, secrets });
      } else {
        await API.post('/templates', { name, description, system_prompt, model, allowed_tools, secrets });
      }
      App.closeModal();
      await loadTemplates();
      renderTemplatesList();
    } catch (e) {
      alert('Error saving: ' + e.message);
    }
  },

  async deleteTemplate(id) {
    if (!confirm('Delete this agent? All its runs and stored credentials will also be removed.')) return;
    try {
      await API.delete(`/templates/${id}`);
      state.activeTemplateId = null;
      hideAll();
      showWelcome();
      await loadTemplates();
      renderTemplatesList();
    } catch (e) {
      alert('Error: ' + e.message);
    }
  },

  // Start run modal
  startRunModal(templateId) {
    const t = state.templates.find(x => x.id === templateId);
    document.getElementById('modal-run-title').textContent = `Run: ${t ? t.name : 'Agent'}`;
    document.getElementById('mr-goal').value = '';
    document.getElementById('mr-btn-start').onclick = () => App.startRun(templateId);
    openModal('modal-run');
  },

  // Ad-hoc run modal
  adhocRunModal() {
    document.getElementById('modal-run-title').textContent = 'Ad-hoc Run';
    document.getElementById('mr-goal').value = '';
    document.getElementById('mr-btn-start').onclick = () => App.startRun(null);
    openModal('modal-run');
  },

  async startRun(templateId) {
    const goal = document.getElementById('mr-goal').value.trim();
    if (!goal) { alert('Please describe what the agent should do.'); return; }

    try {
      const { run_id } = await API.post('/runs', { template_id: templateId || undefined, user_goal: goal });
      App.closeModal();
      await loadRuns();
      renderRunsList();
      App.showRun(run_id);
    } catch (e) {
      alert('Error starting run: ' + e.message);
    }
  },

  async cancelRun(id) {
    if (!confirm('Cancel this run?')) return;
    try {
      await API.delete(`/runs/${id}`);
      if (openSockets[id]) {
        try { openSockets[id].close(); } catch (_) {}
      }
      await loadRuns();
      renderRunsList();
    } catch (e) {
      alert('Error: ' + e.message);
    }
  },

  async togglePin(id, pinned) {
    try {
      await API.post(`/templates/${id}/pin`, { pinned });
      await loadTemplates();
      renderTemplatesList();
      const t = state.templates.find(x => x.id === id);
      if (t) renderTemplateDetail(t);
    } catch (e) { alert('Error: ' + e.message); }
  },

  exportTemplate(id) {
    const t = state.templates.find(x => x.id === id);
    if (!t) return;
    const exportData = {
      name: t.name,
      description: t.description,
      system_prompt: t.system_prompt,
      allowed_tools: t.allowed_tools,
      model: t.model || 'gpt-4o',
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${t.name.replace(/\s+/g, '_')}.json`;
    a.click();
    URL.revokeObjectURL(url);
  },

  importTemplateModal() {
    document.getElementById('import-json').value = '';
    openModal('modal-import');
  },

  async importTemplateSave() {
    const raw = document.getElementById('import-json').value.trim();
    if (!raw) return;
    let data;
    try { data = JSON.parse(raw); } catch { alert('Invalid JSON.'); return; }
    if (!data.name || !data.system_prompt) {
      alert('JSON must include name and system_prompt.');
      return;
    }
    try {
      await API.post('/templates', {
        name: data.name,
        description: data.description || '',
        system_prompt: data.system_prompt,
        allowed_tools: data.allowed_tools || [],
        model: data.model || 'gpt-4o',
      });
      App.closeModal();
      await loadTemplates();
      renderTemplatesList();
    } catch (e) { alert('Import error: ' + e.message); }
  },

  async addSchedule(tid) {
    const goal = document.getElementById('td-sched-goal').value.trim();
    const interval = parseInt(document.getElementById('td-sched-interval').value);
    if (!goal) { alert('Enter a goal for the schedule.'); return; }
    try {
      await API.post(`/templates/${tid}/schedules`, { goal, interval_minutes: interval });
      document.getElementById('td-sched-goal').value = '';
      await renderTemplateSchedules(tid);
    } catch (e) { alert('Error: ' + e.message); }
  },

  async toggleSchedule(sid, enabled, tid) {
    try {
      await API.post(`/schedules/${sid}/toggle`, { enabled });
      await renderTemplateSchedules(tid);
    } catch (e) { alert('Error: ' + e.message); }
  },

  async deleteSchedule(sid, tid) {
    try {
      await API.delete(`/schedules/${sid}`);
      await renderTemplateSchedules(tid);
    } catch (e) { alert('Error: ' + e.message); }
  },

  closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
    document.querySelectorAll('.modal').forEach(m => m.classList.add('hidden'));
  },
};

// ---------------------------------------------------------------------------
// Tool checkboxes
// ---------------------------------------------------------------------------

function renderToolCheckboxes(enabledTools) {
  const container = document.getElementById('mt-tools');
  container.innerHTML = '';
  for (const tool of state.tools) {
    if (tool.name === 'ask_user') continue; // always enabled, skip
    const checked = enabledTools && enabledTools.includes(tool.name);
    const div = document.createElement('label');
    div.className = 'checkbox-label';
    div.innerHTML = `
      <input type="checkbox" name="tool_${tool.name}" value="${esc(tool.name)}" ${checked ? 'checked' : ''} />
      <span title="${esc(tool.description)}">${esc(tool.name)}</span>
    `;
    container.appendChild(div);
  }
}

function getCheckedTools() {
  return Array.from(
    document.querySelectorAll('#mt-tools input[type=checkbox]:checked')
  ).map(cb => cb.value);
}

// ---------------------------------------------------------------------------
// Secrets form helpers
// ---------------------------------------------------------------------------

function addSecretRow(key = '', value = '', isExisting = false) {
  const list = document.getElementById('mt-secrets-list');
  const row = document.createElement('div');
  row.className = 'secret-row';
  if (isExisting) {
    row.innerHTML = `
      <input class="input-field secret-key" value="${esc(key)}" placeholder="key" readonly style="opacity:0.6"/>
      <input class="input-field secret-val" type="password" placeholder="••••• (stored)" />
      <button class="btn-icon" title="Remove" onclick="this.closest('.secret-row').remove()">✕</button>
    `;
  } else {
    row.innerHTML = `
      <input class="input-field secret-key" placeholder="key (e.g. imap_password)" />
      <input class="input-field secret-val" type="password" placeholder="value" />
      <button class="btn-icon" title="Remove" onclick="this.closest('.secret-row').remove()">✕</button>
    `;
  }
  list.appendChild(row);
}

function getSecretsFromForm() {
  const secrets = {};
  for (const row of document.querySelectorAll('#mt-secrets-list .secret-row')) {
    const key = row.querySelector('.secret-key').value.trim();
    const val = row.querySelector('.secret-val').value;
    if (key && val) secrets[key] = val;
  }
  return secrets;
}

// ---------------------------------------------------------------------------
// Modal helpers
// ---------------------------------------------------------------------------

function openModal(id) {
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.getElementById(id).classList.remove('hidden');
}

function hideAll() {
  document.getElementById('welcome-screen').classList.add('hidden');
  document.getElementById('template-detail').classList.add('hidden');
  document.getElementById('run-view').classList.add('hidden');
  document.getElementById('chat-view').classList.add('hidden');
  document.getElementById('dashboard-view').classList.add('hidden');
  state.chatOpen = false;
}

function showWelcome() {
  hideAll();
  document.getElementById('welcome-screen').classList.remove('hidden');
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadTemplates() {
  try {
    state.templates = await API.get('/templates');
  } catch { state.templates = []; }
}

async function loadRuns() {
  try {
    state.runs = await API.get('/runs');
  } catch { state.runs = []; }
}

async function loadTools() {
  try {
    state.tools = await API.get('/tools');
  } catch { state.tools = []; }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  await Promise.all([loadTemplates(), loadRuns(), loadTools()]);
  renderTemplatesList();
  renderRunsList();

  // Top bar buttons
  document.getElementById('btn-chat').onclick = () => Chat.show();
  document.getElementById('btn-dashboard').onclick = () => Dashboard.show();
  document.getElementById('btn-import-agent').onclick = () => App.importTemplateModal();
  document.getElementById('btn-new-template').onclick = () => App.newTemplateModal();
  document.getElementById('btn-adhoc-run').onclick = () => App.adhocRunModal();
  document.getElementById('btn-welcome-new').onclick = () => App.newTemplateModal();
  document.getElementById('btn-welcome-adhoc').onclick = () => App.adhocRunModal();

  // Import save button
  document.getElementById('import-btn-save').onclick = () => App.importTemplateSave();

  // Dashboard refresh
  document.getElementById('dashboard-refresh-btn').onclick = () => Dashboard.load();

  // Sidebar search filters
  document.getElementById('agent-search').addEventListener('input', renderTemplatesList);
  document.getElementById('run-search').addEventListener('input', renderRunsList);

  // Add secret button in modal
  document.getElementById('mt-btn-add-secret').onclick = () => addSecretRow();

  // Send button in input widget
  document.getElementById('rv-btn-send').onclick = sendUserInput;

  // Follow-up widget
  document.getElementById('rv-followup-btn').onclick = sendFollowup;
  document.getElementById('rv-followup-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendFollowup(); }
  });

  // Enter key in input field
  document.getElementById('rv-input-field').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendUserInput(); }
  });

  // Save check toggles save key field
  document.getElementById('rv-save-check').addEventListener('change', (e) => {
    const keyField = document.getElementById('rv-save-key');
    keyField.style.display = e.target.checked ? 'block' : 'none';
  });

  // Chat send button + Enter key
  document.getElementById('chat-send-btn').onclick = () => Chat.send();
  document.getElementById('chat-clear-btn').onclick = () => Chat.clear();
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); Chat.send(); }
  });
  // Auto-resize chat textarea
  document.getElementById('chat-input').addEventListener('input', (e) => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px';
  });

  // Close modal on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') App.closeModal();
  });

  // Poll runs every 10 seconds
  setInterval(async () => {
    await loadRuns();
    renderRunsList();
  }, 10000);
}

document.addEventListener('DOMContentLoaded', init);
