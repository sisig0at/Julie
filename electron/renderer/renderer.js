const backend = {
  async url(force) {
    if (!force && this._url) return this._url;
    if (window.jarvis) {
      const u = await window.jarvis.getBackendUrl();
      if (u) this._url = u;
    } else {
      this._url = window.location.origin;
    }
    return this._url || null;
  },
  // fetch wrapper: on network failure invalidate the cached URL, re-query the
  // real backend URL and retry once (covers the backend announcing its port
  // after the renderer already cached a stale/fallback URL)
  async _req(path, opts, retry) {
    const base = await this.url();
    if (!base) throw new Error('Backend not ready');
    try {
      const res = await fetch(`${base}${path}`, opts);
      if (!res.ok) throw new Error(`Backend error ${res.status}`);
      return res;
    } catch (e) {
      if (retry) {
        this._url = null;
        const base2 = await this.url();
        if (base2 && base2 !== base) return this._req(path, opts, false);
      }
      throw e;
    }
  },
  async chat(text) {
    const res = await this._req('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, tts: true })
    }, true);
    return res.json();
  },
  async status() {
    const res = await this._req('/api/status', {}, true);
    return res.json();
  },
  async clear() {
    await this._req('/api/clear', { method: 'POST' }, true);
  },
  async listen(lang) {
    // lang = recognition language from the UI toggle ("en-US" / "ru-RU");
    // omitted -> backend falls back to the language saved in config
    const q = lang ? `?lang=${encodeURIComponent(lang)}` : '';
    const res = await this._req(`/api/listen${q}`, { method: 'POST' }, true);
    return res.json();
  },
  async stopListen() {
    await this._req('/api/listen/stop', { method: 'POST' }, true).catch(() => {});
  },
  async health() {
    const res = await this._req('/api/health', {}, true);
    return res.json();
  },
  async config() {
    const res = await this._req('/api/config', {}, true);
    return res.json();
  },
  async saveConfig(payload) {
    const res = await this._req('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }, true);
    return res.json();
  },
  async models(provider) {
    const res = await this._req(`/api/models?provider=${encodeURIComponent(provider)}`, {}, true);
    return res.json();
  }
};

const $ = (id) => document.getElementById(id);
const chatLog = $('chat-log');
const statusLabel = $('status-label');
const coreOrb = $('core-orb');
const coreDot = $('core-dot');
const clockEl = $('clock');
const input = $('text-input');
const sendBtn = $('btn-send');
const micBtn = $('btn-mic');
const langBtn = $('btn-lang');
const clearBtn = $('btn-clear');
const banner = $('task-banner');
const bannerIcon = $('banner-icon');
const bannerTitle = $('banner-title');
const bannerText = $('banner-text');
const backendText = $('backend-text');

const audio = new Audio();

let bannerTimer = null;
let lastTaskKey = '';
let audioEnded = false;

/* ---------------- task banner ---------------- */

function showBanner(kind, title, text) {
  banner.className = kind; // '', 'done', 'error'
  bannerIcon.textContent = kind === 'done' ? '\u2713' : kind === 'error' ? '\u26A0' : '\u2699';
  bannerTitle.textContent = title;
  bannerText.textContent = text;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(hideBanner, 5000);
}

function hideBanner() {
  clearTimeout(bannerTimer);
  banner.classList.add('hidden');
}

function clearBannerNow() {
  hideBanner();
}

function formatDetail(detail) {
  if (!detail) return '';
  try {
    const obj = JSON.parse(detail);
    return Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join(' · ');
  } catch {
    return detail;
  }
}

function handleStatus(data) {
  const state = data.state || 'idle';
  const brain = data.brain ? `${data.brain} ` : '';   // "[local] " / "[cloud] "

  // current task running
  if (state === 'executing' && data.task) {
    const key = `x|${data.ts}`;
    if (key !== lastTaskKey) {
      lastTaskKey = key;
      showBanner('', 'EXECUTING', `${brain}${data.task.replace(/_/g, ' ').toUpperCase()} · ${formatDetail(data.detail)}`);
      coreOrb.classList.add('busy');
      statusLabel.textContent = `EXECUTING ${data.brain || ''}`.trim();
    }
  } else if (state === 'thinking') {
    statusLabel.textContent = `THINKING ${data.brain || ''}`.trim();
    coreOrb.classList.add('busy');
  } else if (state === 'error') {
    statusLabel.textContent = 'ERROR';
    coreOrb.classList.add('error');
    if (data.task) showBanner('error', 'ERROR', `${brain}${data.task.replace(/_/g, ' ')}`);
  } else if (state === 'idle') {
    coreOrb.classList.remove('busy', 'error');

    // finished task: show COMPLETE banner for 5s (or until backtick)
    if (data.last_task && data.last_ts) {
      const key = `done|${data.last_ts}`;
      if (key !== lastTaskKey) {
        lastTaskKey = key;
        const title = data.last_task.replace(/_/g, ' ').toUpperCase();
        showBanner('done', 'COMPLETE', `${brain}${title}${data.last_detail ? ' · ' + data.last_detail : ''}`);
      }
    }
    statusLabel.textContent = 'STANDBY';
  }
}

/* ---------------- clock ---------------- */

function tickClock() {
  const now = new Date();
  clockEl.textContent = now.toTimeString().slice(0, 8);
}

setInterval(tickClock, 1000);
tickClock();

/* ---------------- backend polling ---------------- */

let backendOnline = false;

async function pollStatus() {
  try {
    const data = await backend.status();
    if (!backendOnline) {
      backendOnline = true;
      backendText.textContent = 'ONLINE';
      backendText.classList.add('online');
      appendMessage('sys', 'UPLINK ESTABLISHED. ALL SYSTEMS GO.');
      loadSttLang();   // pick up the saved recognition language (EN/RU)
    }
    handleStatus(data);
    checkFirstRun();
  } catch {
    if (backendOnline) {
      backendOnline = false;
      backendText.textContent = 'OFFLINE';
      backendText.classList.remove('online');
      coreOrb.classList.remove('busy', 'error');
      statusLabel.textContent = 'OFFLINE';
      appendMessage('error', 'BACKEND OFFLINE');
    }
  }
}

setInterval(pollStatus, 800);
pollStatus();

/* ---------------- chat ---------------- */

function appendMessage(role, text) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function playAudio(url) {
  audioEnded = false;
  audio.src = url;
  audio.play().catch((e) => console.warn('playback:', e));
  audio.onended = () => { audioEnded = true; };
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  appendMessage('user', text);
  statusLabel.textContent = 'PROCESSING';
  try {
    const { reply, audio: audioFile, brain } = await backend.chat(text);
    appendMessage('jarvis', brain ? `${brain} ${reply}` : reply);
    if (audioFile) {
      const base = await backend.url();
      playAudio(`${base}/audio/${audioFile}`);
    }
  } catch (e) {
    appendMessage('error', `ERROR: ${e.message}`);
    statusLabel.textContent = 'ERROR';
  }
}

sendBtn.addEventListener('click', sendMessage);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendMessage();
});

let isListening = false;

/* ---------------- speech recognition language (EN / RU) ----------------
   Google Web Speech transcribes in exactly one locale per request, so the
   chosen language is sent as ?lang= on every /api/listen call and persisted
   in config (stt_language) to survive a restart. Default stays en-US. */
let sttLang = 'en-US';

function renderLang() {
  langBtn.textContent = sttLang.startsWith('ru') ? 'RU' : 'EN';
  langBtn.title = `Speech recognition language: ${sttLang}`;
}

async function loadSttLang() {
  try {
    const cfg = await backend.config();
    if (cfg && cfg.stt_language) sttLang = cfg.stt_language;
  } catch { /* keep the default (en-US) */ }
  renderLang();
}

langBtn.addEventListener('click', async () => {
  sttLang = sttLang.startsWith('ru') ? 'en-US' : 'ru-RU';
  renderLang();
  try {
    await backend.saveConfig({ stt_language: sttLang });
    appendMessage('sys', `VOICE INPUT LANGUAGE: ${sttLang.startsWith('ru') ? 'RUSSIAN' : 'ENGLISH'}`);
  } catch (e) {
    appendMessage('error', `LANGUAGE SAVE FAILED: ${e.message}`);
  }
});

renderLang();

micBtn.addEventListener('click', async () => {
  if (!isListening) {
    // START listening
    isListening = true;
    micBtn.classList.add('recording');
    micBtn.textContent = '\u25CF\u25CF\u25CF';
    statusLabel.textContent = 'LISTENING...';
    appendMessage('sys', 'LISTENING... CLICK AGAIN TO STOP');

    try {
      const { text, error } = await backend.listen(sttLang);
      if (text) {
        appendMessage('user', text);
        input.value = '';
        statusLabel.textContent = 'PROCESSING';
        const { reply, audio: audioFile, brain } = await backend.chat(text);
        appendMessage('jarvis', brain ? `${brain} ${reply}` : reply);
        if (audioFile) {
          const base = await backend.url();
          playAudio(`${base}/audio/${audioFile}`);
        }
      } else if (error) {
        appendMessage('error', `MIC ERROR: ${error}`);
      } else {
        appendMessage('sys', 'NO SPEECH DETECTED');
      }
    } catch (e) {
      appendMessage('error', `MIC ERROR: ${e.message}`);
    } finally {
      isListening = false;
      micBtn.classList.remove('recording');
      micBtn.textContent = '\uD83C\uDFA4';
      statusLabel.textContent = 'STANDBY';
    }
  } else {
    // STOP listening early
    await backend.stopListen();
  }
});

clearBtn.addEventListener('click', async () => {
  try {
    await backend.clear();
    chatLog.innerHTML = '';
    appendMessage('sys', 'MEMORY PURGED');
  } catch (e) {
    appendMessage('error', `ERROR: ${e.message}`);
  }
});

$('btn-minimize').addEventListener('click', () => window.jarvis && window.jarvis.minimize());
$('btn-close').addEventListener('click', () => window.jarvis && window.jarvis.close());

/* ---------------- backtick clears task banner (global shortcut) ---------------- */

window.addEventListener('keydown', (e) => {
  if (e.key === '`') {
    clearBannerNow();
    statusLabel.textContent = 'STANDBY';
    input.focus();
  }
});

if (window.jarvis) {
  window.jarvis.onTaskClear(() => {
    clearBannerNow();
    statusLabel.textContent = 'STANDBY';
    input.focus();
  });
}

/* ---------------- settings / first-run setup modal ---------------- */

const settingsOverlay = $('settings-overlay');
const settingsHint = $('settings-hint');
const cfgProvider = $('cfg-provider');
const cfgBrainMode = $('cfg-brain-mode');
const cfgKey = $('cfg-key');
const cfgKeyRow = $('cfg-key-row');
const cfgKeyToggle = $('cfg-key-toggle');
const cfgModel = $('cfg-model');
const cfgModelOr = $('cfg-model-or');
const cfgModelOllama = $('cfg-model-ollama');
const cfgStatus = $('cfg-status');
const modelRowGroq = $('cfg-model-row-groq');
const modelRowOr = $('cfg-model-row-openrouter');
const modelRowOllama = $('cfg-model-row-ollama');
const gearBtn = $('btn-settings');
const ollamaOption = cfgProvider.querySelector('option[value="ollama"]');

let hintBase = '';
let manualProvider = 'groq';     // PROVIDER value while BRAIN = manual
let cloudProviderSel = 'groq';   // PROVIDER value while BRAIN = dual (cloud brain)

function openSettings(firstRun) {
  hintBase = firstRun
    ? 'No API key detected. Provide a provider and API key to activate JARVIS. Your key is stored locally and never bundled with the app.'
    : 'Update your provider, API key, or model. Your key is stored locally and never bundled with the app.';
  settingsHint.textContent = hintBase;
  cfgStatus.textContent = '';
  cfgStatus.className = 'modal-status';
  loadSettingsForm();
  settingsOverlay.classList.remove('hidden');
}

function closeSettings() {
  settingsOverlay.classList.add('hidden');
}

async function loadSettingsForm() {
  try {
    const cfg = await backend.config();
    manualProvider = ['groq', 'openrouter', 'ollama'].includes(cfg.provider) ? cfg.provider : 'groq';
    cloudProviderSel = ['groq', 'openrouter'].includes(cfg.cloud_provider) ? cfg.cloud_provider : 'groq';
    cfgBrainMode.value = cfg.brain_mode === 'dual' ? 'dual' : 'manual';
    cfgKey.value = '';
    await refreshForm(cfg);
  } catch {
    cfgStatus.textContent = 'CONFIG UNAVAILABLE';
    cfgStatus.className = 'modal-status error';
  }
}

function setCfgStatus(text, isError) {
  cfgStatus.textContent = text;
  cfgStatus.className = isError ? 'modal-status error' : 'modal-status';
}

async function fillModelList(target, list, provider, saved) {
  list.innerHTML = '';
  target.value = saved || '';
  try {
    const res = await backend.models(provider);
    for (const m of res.models) {
      const opt = document.createElement('option');
      opt.value = m.id;
      list.appendChild(opt);
    }
    if (provider === 'ollama' && res.reachable === false) {
      setCfgStatus(res.error || 'OLLAMA NOT REACHABLE ON localhost:11434', true);
    }
  } catch (e) {
    /* datalist stays empty; typing a model id still works */
    if (provider === 'ollama') {
      setCfgStatus('OLLAMA NOT REACHABLE ON localhost:11434 - RUN `ollama serve`', true);
    }
  }
}

/* Show/hide the rows for the current BRAIN mode + provider and reload the
   model datalists. cfg = freshly loaded config, null = user just toggled. */
async function refreshForm(cfg) {
  const dual = cfgBrainMode.value === 'dual';
  if (dual && cloudProviderSel === 'ollama') cloudProviderSel = 'groq'; // dual: local brain is always Ollama
  const provider = dual ? cloudProviderSel : manualProvider;
  const isOllama = !dual && provider === 'ollama';

  setCfgStatus('', false);

  // In dual mode PROVIDER selects the CLOUD brain, so Ollama can't be picked
  // there (the local brain is always Ollama and gets its own MODEL row).
  ollamaOption.disabled = dual;
  cfgProvider.value = provider;

  // API key: required by the cloud brain, never needed by the local one
  cfgKeyRow.classList.toggle('hidden', isOllama);

  const showGroq = dual ? cloudProviderSel === 'groq' : provider === 'groq';
  const showOr = dual ? cloudProviderSel === 'openrouter' : provider === 'openrouter';
  const showOllama = dual || isOllama;   // dual: both model rows active at once
  modelRowGroq.classList.toggle('hidden', !showGroq);
  modelRowOr.classList.toggle('hidden', !showOr);
  modelRowOllama.classList.toggle('hidden', !showOllama);

  settingsHint.textContent = dual
    ? `Dual brain: short commands go to local Ollama, harder prompts to ${cloudProviderSel === 'openrouter' ? 'OpenRouter' : 'Groq'}. Both model fields below are active.`
    : isOllama
      ? 'Ollama runs on your machine, so no API key is needed. The Ollama service must be running on http://localhost:11434.'
      : hintBase;

  const jobs = [];
  if (showGroq) {
    jobs.push(fillModelList(cfgModel, $('cfg-model-list'), 'groq',
      cfg ? cfg.model : cfgModel.value));
  }
  if (showOr) {
    jobs.push(fillModelList(cfgModelOr, $('cfg-model-or-list'), 'openrouter',
      cfg ? cfg.openrouter_model : cfgModelOr.value));
  }
  if (showOllama) {
    jobs.push(fillModelList(cfgModelOllama, $('cfg-model-ollama-list'), 'ollama',
      cfg ? cfg.ollama_model : cfgModelOllama.value));
  }
  await Promise.all(jobs);
}

cfgProvider.addEventListener('change', () => {
  if (cfgBrainMode.value === 'dual') {
    if (cfgProvider.value !== 'ollama') cloudProviderSel = cfgProvider.value; // local brain is always Ollama
  } else {
    manualProvider = cfgProvider.value;
  }
  refreshForm(null);
});

cfgBrainMode.addEventListener('change', () => refreshForm(null));

cfgKeyToggle.addEventListener('click', () => {
  const show = cfgKey.type === 'password';
  cfgKey.type = show ? 'text' : 'password';
});

gearBtn.addEventListener('click', () => openSettings(false));
$('btn-settings-close').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', (e) => {
  if (e.target === settingsOverlay) closeSettings();
});

$('btn-settings-save').addEventListener('click', async () => {
  const dual = cfgBrainMode.value === 'dual';
  if (dual && cloudProviderSel === 'ollama') cloudProviderSel = 'groq';
  const provider = dual ? cloudProviderSel : manualProvider;
  const isOllama = !dual && provider === 'ollama';
  const key = cfgKey.value.trim();

  if (!dual && !isOllama && !key) {
    setCfgStatus('ENTER AN API KEY', true);
    return;
  }
  if ((isOllama || dual) && !cfgModelOllama.value.trim()) {
    setCfgStatus('ENTER AN OLLAMA MODEL (e.g. llama3.2)', true);
    return;
  }
  const payload = {
    model: cfgModel.value.trim(),
    openrouter_model: cfgModelOr.value.trim(),
    ollama_model: cfgModelOllama.value.trim(),
    brain_mode: dual ? 'dual' : 'manual'
  };
  if (dual) {
    // dual routes on top of the manual provider: only the cloud brain is saved,
    // so switching back to manual keeps the provider the user had picked there
    payload.cloud_provider = cloudProviderSel;
  } else {
    payload.provider = provider;
  }
  // keep the stored key untouched when Ollama (no key) is selected, and in dual
  // mode where the key may already be stored (field starts empty)
  if (!isOllama && key) payload.api_key = key;

  setCfgStatus('SAVING...', false);
  try {
    await backend.saveConfig(payload);
    setCfgStatus('SAVED', false);
    cfgKey.value = '';
    appendMessage('sys', dual
      ? `CONFIG UPDATED. DUAL BRAIN: LOCAL OLLAMA + ${cloudProviderSel === 'openrouter' ? 'OPENROUTER' : 'GROQ'}.`
      : isOllama
        ? 'CONFIG UPDATED. RUNNING ON LOCAL OLLAMA.'
        : 'CONFIG UPDATED. JARVIS ACTIVATED.');
    setTimeout(closeSettings, 700);
  } catch (e) {
    setCfgStatus('SAVE FAILED: ' + e.message, true);
  }
});

/* first-run: auto-open setup when the backend says no key is configured */
let firstRunChecked = false;
async function checkFirstRun() {
  if (firstRunChecked || !backendOnline) return;
  firstRunChecked = true;
  try {
    const h = await backend.health();
    if (!h.configured) {
      openSettings(true);
      appendMessage('sys', 'CONFIGURATION REQUIRED - SET YOUR API KEY');
    }
  } catch { /* backend not ready yet; poll will retry */ }
}

/* ---------------- auto-update status (footer) ---------------- */

const updateState = $('update-state');
const updateText = $('update-text');

if (window.jarvis) {
  window.jarvis.onUpdateAvailable((v) => {
    updateState.classList.remove('hidden');
    updateText.textContent = `v${v} AVAILABLE`;
  });
  window.jarvis.onUpdateProgress((p) => {
    updateState.classList.remove('hidden');
    updateText.textContent = `DOWNLOADING ${p}%`;
  });
  window.jarvis.onUpdateDownloaded((v) => {
    updateState.classList.remove('hidden');
    updateText.textContent = 'READY - CLOSE TO INSTALL';
  });
  window.jarvis.onBackendUrl((u) => {
    if (u === backend._url) return;
    backend._url = u;
    pollStatus();
  });
}
