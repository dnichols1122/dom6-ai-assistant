// Dominions 6 Assistant — frontend application
// Vanilla ES2022, no build step required.

// ─────────────────────────────────────────────
// API helpers
// ─────────────────────────────────────────────

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail ?? r.statusText);
    }
    return r.json();
  },
  /** Returns a raw Response for SSE/streaming endpoints. */
  async postRaw(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  },
};

function fmtBytes(n) {
  if (n == null) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GiB';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MiB';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KiB';
  return n + ' B';
}

// ─────────────────────────────────────────────
// Tab management
// ─────────────────────────────────────────────

let activeTab = 'overview';

function switchTab(name) {
  document.querySelectorAll('.tab-panel').forEach(el => el.classList.add('hidden'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById(`tab-${name}`)?.classList.remove('hidden');
  document.querySelector(`.tab-btn[data-tab="${name}"]`)?.classList.add('active');
  activeTab = name;
  onTabActivated(name);
}

function onTabActivated(name) {
  if (name === 'overview') refreshOverview();
  if (name === 'journal')  { loadJournal(); loadCorrelations(); }
  if (name === 'settings') loadSettings();
  if (name === 'trndiff')  { tdLoadSaves(); tdLoadSessions(); }
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ─────────────────────────────────────────────
// Process status (header + overview)
// ─────────────────────────────────────────────

async function pollStatus() {
  try {
    const data = await api.get('/api/process/status');
    updateStatusUI(data);
  } catch {
    setStatusUnknown();
  }
}

function updateStatusUI(data) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');

  if (data.running) {
    dot.className  = 'w-2 h-2 rounded-full bg-green-400 status-pulse';
    text.textContent = `dom6 · PID ${data.pid}`;
    text.className   = 'text-green-400 text-sm';
  } else {
    dot.className  = 'w-2 h-2 rounded-full bg-red-600';
    text.textContent = 'dom6 not running';
    text.className   = 'text-red-400 text-sm';
  }

  // overview panel
  const ovStatus = document.getElementById('ov-status');
  if (ovStatus) {
    ovStatus.textContent = data.running ? 'Running' : 'Not running';
    ovStatus.className   = data.running ? 'font-mono text-green-400' : 'font-mono text-red-400';
    document.getElementById('ov-pid').textContent  = data.pid ?? '—';
    document.getElementById('ov-name').textContent = data.name ?? '—';
    document.getElementById('ov-rss').textContent  =
      data.memory ? fmtBytes(data.memory.rss_bytes) : '—';
  }
}

function setStatusUnknown() {
  const dot = document.getElementById('status-dot');
  dot.className = 'w-2 h-2 rounded-full bg-gray-600';
  document.getElementById('status-text').textContent = 'Unknown';
}

// ─────────────────────────────────────────────
// Overview tab
// ─────────────────────────────────────────────

async function loadGameState() {
  try {
    const gs = await api.get('/api/gamestate');
    const lockedEl   = document.getElementById('gs-locked');
    const unlockedEl = document.getElementById('gs-unlocked');
    const addrEl     = document.getElementById('gs-address');
    if (!gs.locked) {
      lockedEl.classList.add('hidden');
      unlockedEl.classList.remove('hidden');
      addrEl.textContent = '';
      return;
    }
    unlockedEl.classList.add('hidden');
    lockedEl.classList.remove('hidden');
    addrEl.textContent = gs.address;
    document.getElementById('gs-gold').textContent      = gs.gold?.toLocaleString() ?? '—';
    document.getElementById('gs-nation-id').textContent = gs.nation_id ?? '—';
    if (gs.gems) {
      document.getElementById('gs-gem-fire').textContent   = gs.gems.fire;
      document.getElementById('gs-gem-water').textContent  = gs.gems.water;
      document.getElementById('gs-gem-nature').textContent = gs.gems.nature;
      document.getElementById('gs-gem-air').textContent    = gs.gems.air;
      document.getElementById('gs-gem-astral').textContent = gs.gems.astral;
      document.getElementById('gs-gem-earth').textContent = gs.gems.earth ?? '—';
      document.getElementById('gs-gem-death').textContent = gs.gems.death ?? '—';
      document.getElementById('gs-gem-glamour').textContent = gs.gems.glamour ?? '—';
      document.getElementById('gs-gem-blood').textContent = gs.gems.blood ?? '—';
    }
  } catch (e) {
    // silently ignore — game state is optional
  }
}

async function lockGameState(address) {
  try {
    await api.post('/api/gamestate/lock', { address });
    await loadGameState();
    switchTab('overview');
  } catch (e) {
    alert(`Failed to lock address: ${e.message}`);
  }
}

async function refreshOverview() {
  pollStatus();

  // Key memory regions
  const regionsEl = document.getElementById('ov-regions');
  try {
    const { regions } = await api.get('/api/memory/maps?rw_only=true');
    // Show top interesting regions (heap + large anon + dom6 binary data section)
    const interesting = regions
      .filter(r => r.size_bytes > 1024 * 1024)   // > 1 MiB
      .sort((a, b) => b.size_bytes - a.size_bytes)
      .slice(0, 6);
    if (interesting.length === 0) {
      regionsEl.textContent = 'No regions loaded (is dom6 running?)';
    } else {
      regionsEl.innerHTML = interesting.map(r => `
        <div class="flex items-center gap-3 py-1 border-b border-gray-800 last:border-0">
          <span class="mono text-xs text-gray-500 w-36">${r.start}</span>
          <span class="text-xs text-amber-300 w-20">${fmtBytes(r.size_bytes)}</span>
          <span class="text-xs font-mono text-gray-600 w-10">${r.perms}</span>
          <span class="text-xs text-gray-500 truncate">${r.pathname || '<anon>'}</span>
        </div>`).join('');
    }
  } catch (e) {
    regionsEl.textContent = `Error: ${e.message}`;
  }

  loadGameState();

  // Scan state
  const scanEl = document.getElementById('ov-scan');
  try {
    const s = await api.get('/api/memory/scan/state');
    if (s.value == null) {
      scanEl.textContent = 'No scan running.';
    } else {
      scanEl.innerHTML = `
        <span class="text-gray-300">Value <span class="text-amber-400 mono">${s.value}</span>
        (${s.val_type}) · <span class="text-amber-400">${s.count.toLocaleString()}</span> addresses</span>`;
    }
  } catch {
    scanEl.textContent = '—';
  }
}

// ─────────────────────────────────────────────
// Memory tab — maps
// ─────────────────────────────────────────────

async function loadMaps() {
  const filter = document.getElementById('maps-filter').value;
  const rwOnly = document.getElementById('maps-rw-only').checked;
  const tbody  = document.getElementById('maps-tbody');
  const countEl= document.getElementById('maps-count');

  tbody.innerHTML = '<tr><td colspan="5" class="text-gray-500 py-4 text-center">Loading…</td></tr>';
  try {
    const params = new URLSearchParams({ filter, rw_only: rwOnly });
    const { regions, count } = await api.get(`/api/memory/maps?${params}`);
    countEl.textContent = `${count} regions`;
    if (count === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-gray-500 py-4 text-center">No regions match.</td></tr>';
      return;
    }
    tbody.innerHTML = regions.map(r => `
      <tr class="cursor-pointer" onclick="fillHexAddr('${r.start}')">
        <td class="text-cyan-400">${r.start}</td>
        <td class="text-gray-500">${r.end}</td>
        <td class="text-right text-amber-300">${fmtBytes(r.size_bytes)}</td>
        <td class="text-gray-400">${r.perms}</td>
        <td class="text-gray-500 max-w-xs truncate">${r.pathname || ''}</td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-red-400 py-2">${e.message}</td></tr>`;
  }
}

function fillHexAddr(addr) {
  document.getElementById('hex-address').value = addr;
  switchTab('memory');
  // Small delay so the tab switch renders before we dump
  setTimeout(runHexdump, 50);
}

document.getElementById('maps-refresh-btn').addEventListener('click', loadMaps);
document.getElementById('maps-filter').addEventListener('keydown', e => { if (e.key === 'Enter') loadMaps(); });
document.getElementById('maps-rw-only').addEventListener('change', loadMaps);

// ─────────────────────────────────────────────
// Memory tab — scanner
// ─────────────────────────────────────────────

let lastScanCount = 0;

let _scanProgressInterval = null;

async function pollScanProgress(statusEl) {
  try {
    const p = await api.get('/api/memory/scan/progress');
    if (p.running) {
      const mb = (p.scanned_bytes / 1024 / 1024).toFixed(0);
      const total = (p.total_bytes / 1024 / 1024).toFixed(0);
      statusEl.innerHTML =
        `Scanning… ${mb} / ${total} MiB &nbsp;` +
        `<span class="text-amber-400">${p.percent}%</span>`;
    }
  } catch {}
}

async function runScan() {
  const value = parseInt(document.getElementById('scan-value').value, 10);
  if (isNaN(value)) { alert('Enter a valid integer value to scan for.'); return; }
  const val_type = document.getElementById('scan-type').value;

  const btn = document.getElementById('scan-btn');
  const statusEl = document.getElementById('scan-status');
  btn.disabled = true;
  statusEl.textContent = 'Scanning…';
  clearScanResults();

  // Poll progress every 500ms while the scan runs
  _scanProgressInterval = setInterval(() => pollScanProgress(statusEl), 500);

  try {
    const data = await api.post('/api/memory/scan', { value, val_type });
    lastScanCount = data.count;
    displayScanResults(data);
    document.getElementById('narrow-btn').disabled = false;
    statusEl.textContent = '';
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.className = 'text-xs text-red-400';
  } finally {
    clearInterval(_scanProgressInterval);
    _scanProgressInterval = null;
    btn.disabled = false;
  }
}

async function runNarrow() {
  const value = parseInt(document.getElementById('narrow-value').value, 10);
  if (isNaN(value)) { alert('Enter the new value to narrow by.'); return; }

  const btn = document.getElementById('narrow-btn');
  const statusEl = document.getElementById('narrow-status');
  btn.disabled = true;
  statusEl.textContent = 'Narrowing…';
  clearScanResults();

  try {
    const data = await api.post('/api/memory/narrow', { value });
    lastScanCount = data.count;
    displayScanResults(data);
    if (data.count > 0) btn.disabled = false;
    statusEl.textContent = '';
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
  } finally {
    if (lastScanCount > 0) btn.disabled = false;
  }
}

function displayScanResults(data) {
  const el = document.getElementById('scan-results');
  const scanStatus = document.getElementById('scan-status');

  const badge = data.count === 0 ? 'text-red-400'
              : data.count <= 10 ? 'text-green-400'
              : data.count <= 100 ? 'text-amber-400'
              : 'text-gray-400';

  scanStatus.innerHTML =
    `<span class="${badge}">${data.count.toLocaleString()} matches</span>` +
    (data.truncated ? ` <span class="text-gray-600">(showing first ${data.addresses.length})</span>` : '');
  scanStatus.className = 'text-xs';

  if (data.count === 0) {
    el.innerHTML = '<span class="text-gray-600">No matches found.</span>';
    el.classList.remove('hidden');
    return;
  }
  el.innerHTML = data.addresses.map(a =>
    `<div class="flex items-center gap-2 py-0.5">
      <span class="text-cyan-400 w-44">${a}</span>
      <button class="text-gray-600 hover:text-amber-400 text-xs" onclick="fillHexAddr('${a}')">hexdump</button>
      <button class="text-gray-600 hover:text-green-400 text-xs" onclick="lockGameState('${a}')">lock as game state</button>
    </div>`
  ).join('');
  el.classList.remove('hidden');
}

function clearScanResults() {
  const el = document.getElementById('scan-results');
  el.innerHTML = '';
  el.classList.add('hidden');
}

document.getElementById('scan-btn').addEventListener('click', runScan);
document.getElementById('narrow-btn').addEventListener('click', runNarrow);
document.getElementById('scan-value').addEventListener('keydown', e => { if (e.key === 'Enter') runScan(); });

// ─────────────────────────────────────────────
// Memory tab — hex dump
// ─────────────────────────────────────────────

async function runHexdump() {
  const address = document.getElementById('hex-address').value.trim();
  const size    = parseInt(document.getElementById('hex-size').value, 10);
  const output  = document.getElementById('hex-output');
  const errEl   = document.getElementById('hex-error');

  if (!address) { alert('Enter an address.'); return; }

  output.classList.add('hidden');
  errEl.classList.add('hidden');

  try {
    const params = new URLSearchParams({ address, size });
    const data = await api.get(`/api/memory/hexdump?${params}`);
    output.textContent = data.dump;
    output.classList.remove('hidden');
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

document.getElementById('hex-btn').addEventListener('click', runHexdump);
document.getElementById('hex-address').addEventListener('keydown', e => { if (e.key === 'Enter') runHexdump(); });

// ─────────────────────────────────────────────
// AI Chat tab
// ─────────────────────────────────────────────

let chatHistory = [];  // { role, content }[]

function appendChatBubble(role, content) {
  const container = document.getElementById('chat-messages');
  const isUser = role === 'user';
  const div = document.createElement('div');
  div.className = `flex ${isUser ? 'justify-end' : 'justify-start'}`;
  div.innerHTML = `
    <div class="max-w-2xl px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed
      ${isUser ? 'chat-bubble-user text-blue-100' : 'chat-bubble-assistant text-gray-200'}">
      ${escHtml(content)}
    </div>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div.querySelector('div');
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;

  input.value = '';
  input.disabled = true;
  document.getElementById('chat-send-btn').disabled = true;

  appendChatBubble('user', message);
  chatHistory.push({ role: 'user', content: message });

  // Placeholder for streaming response
  const container = document.getElementById('chat-messages');
  const wrapper = document.createElement('div');
  wrapper.className = 'flex justify-start';
  wrapper.innerHTML = `<div class="max-w-2xl px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed chat-bubble-assistant text-gray-200"></div>`;
  container.appendChild(wrapper);
  container.scrollTop = container.scrollHeight;
  const bubbleEl = wrapper.querySelector('div');

  const backend = document.getElementById('chat-backend').value || null;
  const systemPrompt = document.getElementById('chat-sysprompt').value;

  let fullResponse = '';
  try {
    const response = await api.postRaw('/api/chat', {
      message,
      backend: backend || undefined,
      system_prompt: systemPrompt,
      history: chatHistory.slice(0, -1),   // exclude the just-added user message
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(err.detail ?? response.statusText);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') continue;
        try {
          const parsed = JSON.parse(raw);
          if (parsed.delta) {
            fullResponse += parsed.delta;
            bubbleEl.textContent = fullResponse;
            container.scrollTop = container.scrollHeight;
          }
          if (parsed.error) {
            bubbleEl.textContent = `Error: ${parsed.error}`;
            bubbleEl.classList.add('text-red-400');
          }
        } catch {}
      }
    }
  } catch (e) {
    bubbleEl.textContent = `Connection error: ${e.message}`;
    bubbleEl.classList.add('text-red-400');
  }

  if (fullResponse) {
    chatHistory.push({ role: 'assistant', content: fullResponse });
  }

  input.disabled = false;
  document.getElementById('chat-send-btn').disabled = false;
  input.focus();
}

document.getElementById('chat-send-btn').addEventListener('click', sendChat);
document.getElementById('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
document.getElementById('chat-clear-btn').addEventListener('click', () => {
  chatHistory = [];
  document.getElementById('chat-messages').innerHTML =
    '<p class="text-gray-600 text-sm text-center">Chat cleared.</p>';
});
document.getElementById('chat-sysprompt-toggle').addEventListener('click', () => {
  document.getElementById('chat-sysprompt-panel').classList.toggle('hidden');
});

// ─────────────────────────────────────────────
// Settings tab
// ─────────────────────────────────────────────

let settingsData = null;

async function loadSettings() {
  try {
    settingsData = await api.get('/api/settings');
    populateSettings(settingsData);
  } catch (e) {
    document.getElementById('settings-save-status').textContent = `Load error: ${e.message}`;
  }

  // Backend availability badges
  try {
    const { backends } = await api.get('/api/settings/backends');
    for (const b of backends) {
      const badgeEl = document.getElementById(`${b.name}-badge`);
      if (!badgeEl) continue;
      if (b.installed) {
        badgeEl.textContent = 'installed';
        badgeEl.classList.add('key-set');
      } else {
        badgeEl.textContent = 'not installed';
        badgeEl.classList.add('key-unset');
      }
    }
  } catch {}
}

function populateSettings(data) {
  // Active backend radios
  const radiosEl = document.getElementById('backend-radios');
  const names = ['ollama', 'anthropic', 'openai', 'gemini'];
  radiosEl.innerHTML = names.map(n => `
    <label class="flex items-center gap-2 cursor-pointer">
      <input type="radio" name="active-backend" value="${n}"
        ${data.active_backend === n ? 'checked' : ''}
        class="accent-amber-400">
      <span class="text-sm text-gray-300">${n}</span>
    </label>`).join('');

  // Fill backend config fields
  for (const [name, info] of Object.entries(data.backends)) {
    const modelEl = document.getElementById(`cfg-${name}-model`);
    if (modelEl) modelEl.value = info.model ?? '';

    for (const [k, v] of Object.entries(info.extra)) {
      const el = document.getElementById(`cfg-${name}-${k}`);
      if (el) el.value = v ?? '';
    }

    // Key set badge on settings page
    const keyEnv = info.extra?.api_key_env;
    // (shown in the backend card header via the API badge endpoint above)
  }
}

async function saveSettings() {
  const btn = document.getElementById('settings-save-btn');
  const statusEl = document.getElementById('settings-save-status');
  btn.disabled = true;
  statusEl.textContent = 'Saving…';

  const activeBackend = document.querySelector('input[name="active-backend"]:checked')?.value;
  const names = ['ollama', 'anthropic', 'openai', 'gemini'];

  const backendsPayload = {};
  for (const name of names) {
    const fields = {};
    document.querySelectorAll(`[id^="cfg-${name}-"]`).forEach(el => {
      const key = el.id.replace(`cfg-${name}-`, '');
      if (el.value.trim()) fields[key] = el.value.trim();
    });
    if (Object.keys(fields).length) backendsPayload[name] = fields;
  }

  try {
    await api.post('/api/settings', {
      active_backend: activeBackend,
      backends: backendsPayload,
    });
    statusEl.textContent = 'Saved!';
    statusEl.className = 'text-sm text-green-400';
    setTimeout(() => { statusEl.textContent = ''; statusEl.className = 'text-sm text-gray-400'; }, 3000);
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.className = 'text-sm text-red-400';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('settings-save-btn').addEventListener('click', saveSettings);

// ─────────────────────────────────────────────
// Journal tab
// ─────────────────────────────────────────────

let journalProvinces = [];   // cached list from last load

async function loadJournal() {
  // Populate turn # field with latest recorded turn + 1 if empty
  try {
    const turns = await api.get('/api/journal/turns');
    if (turns.length > 0 && !document.getElementById('j-turn').value) {
      const latest = turns[0];
      document.getElementById('j-turn').value            = latest.turn_number;
      document.getElementById('j-treasury-start').value  = latest.treasury_start ?? '';
      document.getElementById('j-treasury-end').value    = latest.treasury_end ?? '';
      document.getElementById('j-income').value          = latest.total_income ?? '';
      document.getElementById('j-upkeep').value    = latest.upkeep ?? '';
      if (latest.gems) {
        document.getElementById('j-gem-fire').value   = latest.gems.fire   ?? '';
        document.getElementById('j-gem-air').value    = latest.gems.air    ?? '';
        document.getElementById('j-gem-astral').value = latest.gems.astral ?? '';
        document.getElementById('j-gem-water').value  = latest.gems.water  ?? '';
        document.getElementById('j-gem-nature').value = latest.gems.nature ?? '';
        document.getElementById('j-gem-earth').value  = latest.gems.earth  ?? '';
        document.getElementById('j-gem-death').value  = latest.gems.death  ?? '';
        document.getElementById('j-gem-blood').value  = latest.gems.blood  ?? '';
      }
    }
  } catch { /* first run, no turns yet */ }
  await renderProvinces();
}

async function saveTurn() {
  const btn = document.getElementById('j-save-turn-btn');
  const status = document.getElementById('j-turn-status');
  const turn_number = parseInt(document.getElementById('j-turn').value);
  if (!turn_number) { status.textContent = 'Enter a turn number.'; return; }
  btn.disabled = true;
  try {
    await api.post('/api/journal/turns', {
      turn_number,
      treasury_start: intOrNull('j-treasury-start'),
      treasury_end:   intOrNull('j-treasury-end'),
      total_income:   intOrNull('j-income'),
      upkeep:         intOrNull('j-upkeep'),
      gems: {
        fire:   intOrNull('j-gem-fire'),
        water:  intOrNull('j-gem-water'),
        nature: intOrNull('j-gem-nature'),
        air:    intOrNull('j-gem-air'),
        astral: intOrNull('j-gem-astral'),
        earth:  intOrNull('j-gem-earth'),
        death:  intOrNull('j-gem-death'),
        blood:  intOrNull('j-gem-blood'),
      },
    });
    status.textContent = `Turn ${turn_number} saved.`;
    setTimeout(() => { status.textContent = ''; }, 3000);
    loadCorrelations();  // refresh — snapshot may have been captured
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function intOrNull(id) {
  const v = document.getElementById(id)?.value;
  return (v === '' || v == null) ? null : parseInt(v);
}

async function renderProvinces() {
  const container = document.getElementById('j-provinces');
  try {
    journalProvinces = await api.get('/api/journal/provinces');
  } catch (e) {
    container.innerHTML = `<div class="text-red-400 text-xs">Error: ${e.message}</div>`;
    return;
  }
  if (journalProvinces.length === 0) {
    container.innerHTML = '<div class="text-sm text-gray-600">No provinces recorded yet. Click "+ Add Province" to start.</div>';
    return;
  }
  container.innerHTML = '';
  const tmpl = document.getElementById('prov-snap-template');
  for (const prov of journalProvinces) {
    const card = tmpl.content.cloneNode(true).querySelector('.prov-card');
    card.dataset.provId = prov.id;

    // Header
    card.querySelector('.prov-name').textContent =
      (prov.is_capital ? '★ ' : '') + prov.name;

    // Latest snapshot summary
    const s = prov.latest;
    card.querySelector('.prov-summary').textContent = s
      ? `Turn ${s.turn_number} · Pop ${s.population?.toLocaleString() ?? '?'} · Income ${s.income ?? '?'} · Gold · Defense ${s.defense ?? '?'}`
      : 'No data recorded yet';

    // Sites
    card.querySelector('.prov-sites').textContent = prov.sites.length
      ? 'Sites: ' + prov.sites.map(x => x.name).join(', ')
      : '';

    // Toggle form
    const form = card.querySelector('.prov-form');
    card.querySelector('.prov-toggle-btn').addEventListener('click', () => {
      const hidden = form.classList.toggle('hidden');
      if (!hidden) prefillSnapForm(form, prov);
    });
    card.querySelector('.prov-form-cancel-btn').addEventListener('click', () => {
      form.classList.add('hidden');
    });

    // Save snapshot
    card.querySelector('.prov-snap-save-btn').addEventListener('click', () =>
      saveSnapshot(prov.id, form));

    // Delete province
    card.querySelector('.prov-delete-btn').addEventListener('click', async () => {
      if (!confirm(`Delete province "${prov.name}"? This removes all its history.`)) return;
      try {
        await fetch(`/api/journal/provinces/${prov.id}`, { method: 'DELETE' });
        await renderProvinces();
      } catch (e) { alert(`Delete failed: ${e.message}`); }
    });

    container.appendChild(card);
  }
}

function prefillSnapForm(form, prov) {
  const s = prov.latest;
  const turnField = form.querySelector('[name="turn_number"]');
  // Default to current journal turn
  turnField.value = document.getElementById('j-turn').value || (s?.turn_number ?? '');
  if (!s) return;
  for (const [k, v] of Object.entries(s)) {
    const el = form.querySelector(`[name="${k}"]`);
    if (el && v != null) el.value = v;
  }
  // Sites as textarea
  form.querySelector('[name="sites"]').value =
    prov.sites.map(x => x.name).join('\n');
}

async function saveSnapshot(provId, form) {
  const status = form.querySelector('.prov-snap-status');
  const btn    = form.querySelector('.prov-snap-save-btn');
  const turn_number = parseInt(form.querySelector('[name="turn_number"]').value);
  if (!turn_number) { status.textContent = 'Turn # required.'; return; }

  const fields = [
    'terrain','population','income','resources','recruitment_points',
    'recruitment_per_turn','supplies','supply_usage','defense','unrest',
    'dominion_strength','scale_order','scale_productivity','scale_heat',
    'scale_growth','scale_luck','scale_magic',
  ];
  const snap = { turn_number };
  for (const f of fields) {
    const el = form.querySelector(`[name="${f}"]`);
    if (!el) continue;
    snap[f] = el.value === '' ? null
            : f === 'terrain' ? el.value
            : parseInt(el.value);
  }

  // Sites: split textarea by newline, trim blanks
  const sitesRaw = form.querySelector('[name="sites"]').value;
  const sites = sitesRaw.split('\n').map(s => s.trim()).filter(Boolean)
    .map(name => ({ name, site_type: null }));

  btn.disabled = true;
  status.textContent = '';
  try {
    await api.post(`/api/journal/provinces/${provId}/snapshot`, snap);
    await fetch(`/api/journal/provinces/${provId}/sites`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sites }),
    });
    status.textContent = 'Saved.';
    setTimeout(() => { status.textContent = ''; }, 2000);
    await renderProvinces();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

// Add province form
document.getElementById('j-add-prov-btn').addEventListener('click', () => {
  document.getElementById('j-add-prov-form').classList.toggle('hidden');
});
document.getElementById('j-prov-cancel-btn').addEventListener('click', () => {
  document.getElementById('j-add-prov-form').classList.add('hidden');
});
document.getElementById('j-prov-save-btn').addEventListener('click', async () => {
  const name = document.getElementById('j-prov-name').value.trim();
  if (!name) return;
  const is_capital = document.getElementById('j-prov-capital').checked;
  const province_number = intOrNull('j-prov-number');
  try {
    await api.post('/api/journal/provinces', { name, is_capital, province_number });
    document.getElementById('j-add-prov-form').classList.add('hidden');
    document.getElementById('j-prov-name').value = '';
    document.getElementById('j-prov-number').value = '';
    document.getElementById('j-prov-capital').checked = false;
    await renderProvinces();
  } catch (e) { alert(`Error: ${e.message}`); }
});
document.getElementById('j-save-turn-btn').addEventListener('click', saveTurn);

// ─────────────────────────────────────────────
// Correlation panel
// ─────────────────────────────────────────────

const FIELD_LABELS = {
  total_income: 'Total Income',
  upkeep:       'Upkeep',
};

async function loadCorrelations() {
  const gemsEl   = document.getElementById('corr-gems');
  const fieldsEl = document.getElementById('corr-fields');
  try {
    const data = await api.get('/api/correlate');
    renderGemMapping(gemsEl, data.gem_mapping);
    renderFieldCorrelations(fieldsEl, data.fields);
  } catch (e) {
    gemsEl.textContent = `Error: ${e.message}`;
  }
}

function renderGemMapping(el, gm) {
  if (gm.status === 'no_data') {
    el.innerHTML = '<span class="text-gray-600">No snapshots yet — save a turn with the gold address locked.</span>';
    return;
  }
  if (gm.status === 'no_journal_gems') {
    el.innerHTML = '<span class="text-gray-600">Enter Earth/Death/Blood values in the turn form to enable mapping.</span>';
    return;
  }
  const badge = {
    resolved:     '<span class="text-green-400">✓ Resolved</span>',
    ambiguous:    '<span class="text-amber-400">⚠ Ambiguous</span>',
    inconsistent: '<span class="text-red-400">✗ Inconsistent — check journal entries</span>',
  }[gm.status] ?? gm.status;

  let html = `${badge} (${gm.turns_checked} turn${gm.turns_checked !== 1 ? 's' : ''} checked)`;
  for (const m of gm.consistent_mappings) {
    const parts = Object.entries(m).map(([slot, gem]) =>
      `${slot} = <span class="text-amber-300">${gem}</span>`
    ).join(', ');
    html += `<div class="mt-0.5 pl-2 text-gray-300">${parts}</div>`;
  }
  el.innerHTML = html;
}

function renderFieldCorrelations(el, fields) {
  el.innerHTML = '';
  for (const [name, f] of Object.entries(fields)) {
    const label = FIELD_LABELS[name] ?? name;
    const statusText = f.resolved
      ? `<span class="text-green-400">✓ ${f.locked_address}</span>`
      : f.candidates === null
        ? '<span class="text-gray-600">not scanned</span>'
        : `<span class="text-amber-400">${f.candidates} candidates</span>`;

    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 text-xs';
    row.innerHTML = `
      <span class="text-gray-400 w-24">${label}</span>
      <span id="corr-f-${name}" class="font-mono flex-1">${statusText}</span>
      <button class="btn-sm py-0 text-xs corr-correct-btn" data-field="${name}"
        title="Enter the actual in-game value to narrow candidates">Correct</button>
      <button class="btn-sm py-0 text-xs corr-reset-btn" data-field="${name}">Reset</button>
    `;
    row.querySelector('.corr-correct-btn').addEventListener('click', () => correctField(name));
    row.querySelector('.corr-reset-btn').addEventListener('click', () => resetField(name));
    el.appendChild(row);
  }
}

async function correctField(fieldName) {
  const label = FIELD_LABELS[fieldName] ?? fieldName;
  const raw = prompt(`Actual in-game value for "${label}" right now:`);
  if (raw === null || raw.trim() === '') return;
  const value = parseInt(raw);
  if (isNaN(value)) { alert('Please enter a whole number.'); return; }
  try {
    await api.post(`/api/correlate/fields/${fieldName}/correct`, { value });
    await loadCorrelations();
  } catch (e) { alert(`Error: ${e.message}`); }
}

async function resetField(fieldName) {
  if (!confirm(`Reset correlation for "${FIELD_LABELS[fieldName] ?? fieldName}"? Candidates will be cleared.`)) return;
  try {
    const r = await fetch(`/api/correlate/fields/${fieldName}`, { method: 'DELETE' });
    if (!r.ok && r.status !== 204) throw new Error(r.statusText);
    await loadCorrelations();
  } catch (e) { alert(`Error: ${e.message}`); }
}

document.getElementById('corr-refresh-btn').addEventListener('click', loadCorrelations);
document.getElementById('corr-scan-btn').addEventListener('click', async () => {
  const btn = document.getElementById('corr-scan-btn');
  const status = document.getElementById('corr-status');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  status.textContent = 'Scanning memory — this may take ~20 s…';
  try {
    const results = await api.post('/api/correlate/fields/scan', {});
    status.textContent = 'Scan complete.';
    setTimeout(() => { status.textContent = ''; }, 4000);
    await loadCorrelations();
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan Fields';
  }
});

// Pre-fill form fields from live memory when turn number loses focus
document.getElementById('j-turn').addEventListener('change', async () => {
  try {
    const live = await api.get('/api/correlate/live');
    if (live.total_income != null) {
      const el = document.getElementById('j-income');
      if (!el.value) el.value = live.total_income;
    }
    if (live.upkeep != null) {
      const el = document.getElementById('j-upkeep');
      if (!el.value) el.value = live.upkeep;
    }
  } catch { /* silently ignore if no addresses resolved yet */ }
});

// ─────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────

// Expose functions used in dynamically-generated onclick handlers to window.
// Required because this script runs as type="module" (module scope ≠ global scope).
window.fillHexAddr  = fillHexAddr;
window.lockGameState = lockGameState;

// ─────────────────────────────────────────────
// TRN Diff tab
// ─────────────────────────────────────────────

let tdActiveSession = '';
let tdSnapshots = [];   // [{label, timestamp, fields}, …]

const tdFieldIds = ['gold','income','upkeep','turn','fire','air','water','earth','astral','death','nature','glamour','blood'];

function tdFieldValues() {
  const out = {};
  for (const f of tdFieldIds) {
    const v = document.getElementById(`td-f-${f}`)?.value;
    if (v !== '' && v != null) out[f] = parseFloat(v);
  }
  return out;
}

function tdClearFields() {
  for (const f of tdFieldIds) {
    const el = document.getElementById(`td-f-${f}`);
    if (el) el.value = '';
  }
}

async function tdLoadSaves() {
  try {
    const data = await api.get('/api/trndiff/saves');
    const sel = document.getElementById('td-save-select');
    sel.innerHTML = '<option value="">— select save file —</option>';
    for (const s of data.saves) {
      const opt = document.createElement('option');
      opt.value = s.path;
      opt.textContent = s.label;
      sel.appendChild(opt);
    }
  } catch (e) {
    console.warn('tdLoadSaves:', e);
  }
}

async function tdLoadSessions() {
  try {
    const data = await api.get('/api/trndiff/sessions');
    const sel = document.getElementById('td-session-select');
    const prev = sel.value;
    sel.innerHTML = '<option value="">— select or create —</option>';
    for (const s of data.sessions) {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = `${s.name}  (${s.snapshot_count} snaps)`;
      sel.appendChild(opt);
    }
    if (prev && [...sel.options].some(o => o.value === prev)) {
      sel.value = prev;
    }
  } catch (e) {
    console.warn('tdLoadSessions:', e);
  }
}

async function tdSelectSession(name) {
  tdActiveSession = name;
  const label = document.getElementById('td-active-session-label');
  if (!name) {
    tdSnapshots = [];
    document.getElementById('td-snapshot-list').classList.add('hidden');
    label.classList.add('hidden');
    return;
  }
  label.textContent = `Active session: ${name}`;
  label.classList.remove('hidden');
  try {
    const data = await api.get(`/api/trndiff/sessions/${encodeURIComponent(name)}/snapshots`);
    tdSnapshots = data.snapshots;
    tdRenderSnapshotList();
    tdPopulateDiffSelects();
  } catch (e) {
    console.warn('tdSelectSession:', e);
  }
}

function tdRenderSnapshotList() {
  const wrap = document.getElementById('td-snapshot-list');
  const items = document.getElementById('td-snapshot-items');
  if (!tdSnapshots.length) {
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  items.innerHTML = tdSnapshots.map(s => {
    const fields = Object.entries(s.fields).map(([k,v]) => `${k}=${v}`).join('  ') || '(no fields)';
    const ts = new Date(s.timestamp).toLocaleTimeString();
    return `<div class="py-0.5"><span class="text-amber-300">${s.label}</span>  <span class="text-gray-600">${ts}</span>  ${fields}</div>`;
  }).join('');
}

function tdPopulateDiffSelects() {
  for (const id of ['td-diff-a', 'td-diff-b']) {
    const sel = document.getElementById(id);
    const prev = sel.value;
    sel.innerHTML = '<option value="">— pick —</option>';
    for (const s of tdSnapshots) {
      const opt = document.createElement('option');
      opt.value = s.label;
      opt.textContent = s.label;
      sel.appendChild(opt);
    }
    if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  }
}

// Session create
document.getElementById('td-create-session-btn').addEventListener('click', async () => {
  const name = document.getElementById('td-session-name').value.trim();
  const status = document.getElementById('td-session-status');
  if (!name) { status.textContent = 'Enter a session name.'; return; }
  try {
    await api.post('/api/trndiff/sessions', { name });
    status.textContent = `Session '${name}' created.`;
    document.getElementById('td-session-name').value = '';
    await tdLoadSessions();
    document.getElementById('td-session-select').value = name;
    await tdSelectSession(name);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// Session select change
document.getElementById('td-session-select').addEventListener('change', async e => {
  await tdSelectSession(e.target.value);
});

// Snapshot button
document.getElementById('td-snap-btn').addEventListener('click', async () => {
  const status = document.getElementById('td-snap-status');
  const trnPath = document.getElementById('td-save-select').value;
  const label   = document.getElementById('td-snap-label').value.trim();
  if (!tdActiveSession) { status.textContent = 'Create or select a session first (top section).'; return; }
  if (!trnPath) { status.textContent = 'Select a save file.'; return; }
  if (!label)   { status.textContent = 'Enter a label for this snapshot.'; return; }

  status.textContent = 'Saving…';
  try {
    await api.post(
      `/api/trndiff/sessions/${encodeURIComponent(tdActiveSession)}/snapshot`,
      { trn_path: trnPath, label, fields: tdFieldValues() }
    );
    status.textContent = `Snapshot '${label}' saved.`;
    document.getElementById('td-snap-label').value = '';
    tdClearFields();
    await tdSelectSession(tdActiveSession);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});

// Diff button
document.getElementById('td-diff-btn').addEventListener('click', async () => {
  const labelA  = document.getElementById('td-diff-a').value;
  const labelB  = document.getElementById('td-diff-b').value;
  const minLen  = parseInt(document.getElementById('td-diff-minlen').value) || 1;
  const summary = document.getElementById('td-diff-summary');
  const wrap    = document.getElementById('td-diff-table-wrap');
  const tbody   = document.getElementById('td-diff-tbody');

  if (!tdActiveSession) { summary.textContent = 'Select a session first.'; summary.classList.remove('hidden'); return; }
  if (!labelA || !labelB) { summary.textContent = 'Pick two snapshots from the dropdowns.'; summary.classList.remove('hidden'); return; }
  if (labelA === labelB)  { summary.textContent = 'Pick two different snapshots.'; summary.classList.remove('hidden'); return; }

  summary.textContent = 'Diffing…'; summary.classList.remove('hidden');
  wrap.classList.add('hidden');

  try {
    const data = await api.post(
      `/api/trndiff/sessions/${encodeURIComponent(tdActiveSession)}/diff`,
      { label_a: labelA, label_b: labelB, min_len: minLen }
    );
    summary.textContent =
      `${data.label_a} vs ${data.label_b} — sizes: ${data.size_a.toLocaleString()} → ${data.size_b.toLocaleString()} bytes — ` +
      `${data.total_regions} changed regions (showing ${data.shown})`;

    tbody.innerHTML = data.regions.map(r => {
      const delta = r.delta_i32 != null ? (r.delta_i32 > 0 ? `+${r.delta_i32}` : String(r.delta_i32)) : '';
      const deltaClass = r.delta_i32 > 0 ? 'text-green-400' : r.delta_i32 < 0 ? 'text-red-400' : '';
      return `<tr>
        <td class="font-mono">${r.offset_hex}</td>
        <td class="text-right">${r.length}</td>
        <td class="font-mono text-red-300">${r.old_hex}</td>
        <td class="font-mono text-green-300">${r.new_hex}</td>
        <td class="text-right font-mono">${r.old_i32 ?? ''}</td>
        <td class="text-right font-mono">${r.new_i32 ?? ''}</td>
        <td class="text-right font-mono ${deltaClass}">${delta}</td>
      </tr>`;
    }).join('');

    wrap.classList.remove('hidden');
  } catch (e) {
    summary.textContent = `Error: ${e.message}`;
  }
});

// Analyze button
document.getElementById('td-analyze-btn').addEventListener('click', async () => {
  const results = document.getElementById('td-analyze-results');
  if (!tdActiveSession) {
    results.innerHTML = '<div class="text-xs text-red-400">Select a session first.</div>';
    return;
  }
  results.innerHTML = '<div class="text-xs text-gray-500">Analyzing…</div>';

  try {
    const data = await api.get(
      `/api/trndiff/sessions/${encodeURIComponent(tdActiveSession)}/analyze`
    );
    const fields = data.fields;
    if (!Object.keys(fields).length) {
      results.innerHTML = '<div class="text-xs text-gray-500">No correlations found. Take more snapshots with different field values.</div>';
      return;
    }

    results.innerHTML = Object.entries(fields).map(([fieldName, candidates]) => {
      const rows = candidates.map(c => {
        const exactBadge = c.exact_match
          ? '<span class="ml-1 text-green-400 font-bold">✓ exact</span>' : '';
        const rowClass = c.exact_match ? 'bg-green-950' : '';
        const samples = c.samples.slice(0,3).map(s => {
          const exactMark = s.exact ? ' ✓' : '';
          return `${s.field_old}→${s.field_new} / ${s.bytes_old}→${s.bytes_new} (Δ${s.delta > 0 ? '+' : ''}${s.delta}${exactMark})`;
        }).join('<br>');
        return `<tr class="${rowClass}">
          <td class="font-mono text-amber-300">${c.offset_hex}${exactBadge}</td>
          <td class="text-right">${c.length}</td>
          <td class="text-right">${c.observations}</td>
          <td class="text-xs text-gray-400">${samples}</td>
        </tr>`;
      }).join('');

      return `<div>
        <div class="text-xs text-green-400 font-semibold mb-1">${fieldName} — ${candidates.length} candidate(s)</div>
        <table class="maps-table w-full text-xs mb-2">
          <thead><tr><th>Offset</th><th class="text-right">Len</th><th class="text-right">Obs.</th><th>Samples (field old→new / bytes old→new)</th></tr></thead>
          <tbody class="mono text-gray-300">${rows}</tbody>
        </table>
      </div>`;
    }).join('');
  } catch (e) {
    results.innerHTML = `<div class="text-xs text-red-400">Error: ${e.message}</div>`;
  }
});

switchTab('overview');
pollStatus();
setInterval(pollStatus, 5000);   // refresh process status every 5s
