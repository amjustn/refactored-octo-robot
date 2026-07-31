// AI Berkshire Web - Application Logic
let currentSkill = null;
let allSkills = [];
let currentReport = '';
let ws = null;
let _researchAbort = null;   // AbortController for batch mode
let _historyData = [];       // Cached history for search
let _wsReconnectAttempts = 0;
let _wsShouldReconnect = false;
const _WS_MAX_RECONNECT = 3;
const _WS_RECONNECT_DELAYS = [2000, 5000, 10000]; // ms

let currentTaskId = null;    // task_id reported by server (for real cancel)
let _userCancelled = false;  // distinguish user cancel from timeout abort
let _progressTimer = null;   // elapsed-time ticker
let _progressStartTs = 0;
let _renderScheduled = false; // streaming render throttle flag
let _favorites = new Set();  // starred skills
let _compareSelection = new Set(); // history compare checkboxes

// Category icons
const CAT_ICONS = {
  '深度研究': 'K',
  '财报分析': 'E',
  '行业筛选': 'I',
  '持仓管理': 'P',
  '思维工具': 'T',
  '日报': 'D'
};

// ==================== Helpers ====================
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return sec + '秒';
  var m = Math.floor(sec / 60), s = sec % 60;
  return m + '分' + (s ? s + '秒' : '');
}

function fmtTokens(t) {
  if (!t || !t.total_tokens) return '';
  var n = t.total_tokens;
  var s = n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n);
  return '约 ' + s + ' tokens';
}

function skillDisplayName(name) {
  var s = allSkills.find(function(x) { return x.name === name; });
  return s ? (s.display_name || s.name) : name;
}

// ==================== Toast ====================
function showToast(msg, type) {
  type = type || 'info';
  var c = document.getElementById('toast-container');
  var el = document.createElement('div');
  el.className = 'toast-msg ' + type;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(function() {
    el.classList.add('out');
    setTimeout(function() { el.remove(); }, 300);
  }, 3000);
}

// ==================== Init ====================

function _promptForToken(callback) {
  var secret = prompt('请输入访问令牌（服务器 BERKSHIRE_API_TOKEN）：');
  if (!secret) { document.body.innerHTML = '<div style="padding:40px;text-align:center;color:#c4a77d">需要令牌才能访问</div>'; return; }
  loginWithToken(secret).then(function(data) {
    if (data && data.note === 'Auth disabled') {
      localStorage.setItem('ai_berkshire_jwt', 'disabled');
    }
    callback();
  }).catch(function(e) {
    alert('令牌无效：' + e.message);
    _promptForToken(callback);
  });
}
document.addEventListener('DOMContentLoaded', function() {
  // Restore favorites
  try {
    _favorites = new Set(JSON.parse(localStorage.getItem('ai_berkshire_favs') || '[]'));
  } catch(e) { _favorites = new Set(); }

  // Restore last report from sessionStorage
  try {
    var savedReport = sessionStorage.getItem('ai_berkshire_report');
    var savedMeta = JSON.parse(sessionStorage.getItem('ai_berkshire_report_meta') || '{}');
    if (savedReport) {
      currentReport = savedReport;
      document.getElementById('empty-state').style.display = 'none';
      document.getElementById('result-area').style.display = 'block';
      renderReport(savedReport);
      // Banner: make clear this is a restored report, not a fresh one
      var banner = document.getElementById('restore-banner');
      var when = savedMeta.ts ? new Date(savedMeta.ts).toLocaleString() : '未知时间';
      var skillText = savedMeta.skill ? skillDisplayName(savedMeta.skill) : '未知技能';
      document.getElementById('restore-banner-text').textContent =
        '这是上次会话恢复的报告（' + skillText + ' · ' + when + '），并非刚刚生成';
      banner.style.display = 'flex';
    }
  } catch(e) {}

  // Enter key to submit
  document.getElementById('arg-field').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') startResearch();
  });

  // Input char count & hint
  document.getElementById('arg-field').addEventListener('input', function(e) {
    var val = e.target.value;
    var cc = document.getElementById('charCount');
    if (val.length > 0) {
      cc.textContent = val.length + '字';
      var isCode = /^\d{6}$/.test(val);
      document.getElementById('input-hint').textContent = isCode ? '检测到股票代码格式' : (currentSkill ? currentSkill.input_hint || '' : '');
    } else {
      cc.textContent = '';
      document.getElementById('input-hint').textContent = currentSkill ? currentSkill.input_hint || '' : '';
    }
  });

  renderArgHistory();

  // Fetch server model info (supported model list, default model)
  fetch('/api/llm/default').then(function(r) { return r.json(); })
    .then(function(d) { _initLlmDefaults(d); })
    .catch(function() { _initLlmDefaults(null); });
  loadSkills();
});

function dismissRestoreBanner() {
  document.getElementById('restore-banner').style.display = 'none';
}

// ==================== Sidebar (mobile) ====================
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

// ==================== Skills ====================
async function loadSkills() {
  try {
    var res = await fetch('/api/skills', { headers: getAuthHeaders() });
    if (res.status === 401) {
      // Token expired or server restarted with a new secret — re-login
      localStorage.removeItem('ai_berkshire_jwt');
      location.reload();
      return;
    }
    var data = await res.json();
    allSkills = data.skills;
    document.getElementById('nav-loading').style.display = 'none';
    renderSkillNav(allSkills);
    if (!prefillFromURL()) {
      var saved = localStorage.getItem('ai_berkshire_skill');
      if (saved) selectSkill(saved);
      else if (allSkills.length > 0) selectSkill(allSkills[0].name);
    }
  } catch (e) {
    document.getElementById('nav-loading').innerHTML =
      '<p style="color:var(--accent-red);padding:12px 20px;font-size:0.8rem">技能加载失败 <button onclick="location.reload()" style="cursor:pointer;background:#30363d;color:#e6edf3;border:1px solid #484f58;border-radius:4px;padding:2px 10px;margin-left:8px">重试</button></p>';
    console.error('Failed to load skills:', e);
    setTimeout(function() {
      var params = new URLSearchParams(window.location.search);
      var skill = params.get('skill') || '';
      var args = params.get('arguments') || params.get('company') || '';
      if (skill && args) {
        document.getElementById('arg-field').value = args;
        document.getElementById('skill-name').textContent = skill;
        if (params.get('auto') !== '0' && params.get('noauto') !== '1') {
          setTimeout(function() {
            try { startResearch(); } catch(e2) { console.error('auto-start failed:', e2); }
          }, 1000);
        }
      }
    }, 500);
  }
}

// Build nav sections dynamically from API categories — new categories
// (e.g. 日报) appear automatically without touching the HTML.
function renderSkillNav(skills) {
  var nav = document.getElementById('nav-categories');
  var cats = [];
  var catMap = {};
  skills.forEach(function(s) {
    if (!catMap[s.category]) {
      catMap[s.category] = [];
      cats.push(s.category);
    }
    catMap[s.category].push(s);
  });

  var html = '';

  // Favorites pinned on top
  var favs = skills.filter(function(s) { return _favorites.has(s.name); });
  if (favs.length) {
    html += buildSection('⭐ 收藏', 'favorites', favs);
  }
  cats.forEach(function(cat) {
    html += buildSection(cat, cat, catMap[cat]);
  });
  nav.innerHTML = html;

  // Restore collapsed states
  try {
    var state = JSON.parse(localStorage.getItem('ai_berkshire_toggle') || '{}');
    nav.querySelectorAll('.nav-section').forEach(function(section) {
      var items = section.querySelector('.nav-items');
      var header = section.querySelector('.nav-header');
      var cat = items.id ? items.id.replace('cat-', '') : '';
      if (state[cat]) {
        header.classList.add('collapsed');
        items.classList.add('collapsed');
      }
    });
  } catch(e) {}

  filterSkills();
}

function buildSection(label, key, skills) {
  var items = skills.map(function(s) {
    var badge = s.is_multi_agent ? '<span class="multi-badge">' + s.agent_count + 'A</span>' : '';
    var star = '<span class="fav-star' + (_favorites.has(s.name) ? ' on' : '') +
      '" data-star="' + escHtml(s.name) + '" onclick="toggleFav(event,\'' + escHtml(s.name) + '\')">★</span>';
    return '<div class="nav-item" data-skill="' + escHtml(s.name) + '" onclick="selectSkill(\'' + escHtml(s.name) + '\')" title="' + escHtml(s.description || '') + '">' +
      '<span class="nav-item-name">' + escHtml(s.display_name || s.name) + badge + '</span>' + star + '</div>';
  }).join('');
  return '<div class="nav-section">' +
    '<div class="nav-header" onclick="toggleSection(this)"><span>' + escHtml(label) + '</span><span class="arrow">V</span></div>' +
    '<div class="nav-items" id="cat-' + escHtml(key) + '" data-cat="' + escHtml(label) + '">' + items + '</div></div>';
}

function toggleFav(e, name) {
  e.stopPropagation();
  if (_favorites.has(name)) _favorites.delete(name);
  else _favorites.add(name);
  localStorage.setItem('ai_berkshire_favs', JSON.stringify(Array.from(_favorites)));
  // Re-render nav but keep the active skill highlighted
  var active = currentSkill ? currentSkill.name : null;
  renderSkillNav(allSkills);
  if (active) {
    document.querySelectorAll('.nav-item[data-skill="' + active + '"]')
      .forEach(function(el) { el.classList.add('active'); });
  }
}

function filterSkills() {
  var q = (document.getElementById('skill-search').value || '').trim().toLowerCase();
  document.querySelectorAll('#nav-categories .nav-section').forEach(function(section) {
    var visible = 0;
    section.querySelectorAll('.nav-item').forEach(function(item) {
      var name = item.textContent.toLowerCase();
      var skill = allSkills.find(function(s) { return s.name === item.dataset.skill; });
      var hay = name + ' ' + (skill ? (skill.name + ' ' + (skill.description || '')).toLowerCase() : '');
      var show = !q || hay.indexOf(q) >= 0;
      item.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    section.style.display = visible ? '' : 'none';
  });
}

function selectSkill(name) {
  var skill = allSkills.find(function(s) { return s.name === name; });
  if (!skill) return;

  currentSkill = skill;
  localStorage.setItem('ai_berkshire_skill', name);

  document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.nav-item[data-skill="' + name + '"]')
    .forEach(function(el) { el.classList.add('active'); });

  document.getElementById('skill-name').textContent = skill.name;
  document.getElementById('skill-label').textContent = skill.display_name || '';
  document.getElementById('skill-icon').textContent = CAT_ICONS[skill.category] || '?';
  document.getElementById('skill-desc').textContent = skill.description || '';
  document.getElementById('input-hint').textContent = skill.input_hint || '';
  document.getElementById('arg-field').placeholder = skill.input_hint || '输入分析目标...';

  var btn = document.getElementById('btn-research');
  btn.textContent = skill.is_multi_agent ? '启动 ' + skill.agent_count + ' Agent 并行研究' : '开始研究';

  // On mobile, close the drawer after picking a skill
  if (window.innerWidth <= 860) document.body.classList.remove('sidebar-open');
}

// ==================== Sidebar Toggle ====================
function toggleSection(header) {
  header.classList.toggle('collapsed');
  var items = header.nextElementSibling;
  items.classList.toggle('collapsed');
  var cat = items.id ? items.id.replace('cat-', '') : '';
  if (cat) {
    var state = JSON.parse(localStorage.getItem('ai_berkshire_toggle') || '{}');
    state[cat] = header.classList.contains('collapsed');
    localStorage.setItem('ai_berkshire_toggle', JSON.stringify(state));
  }
}

function quickStart(skillName, args) {
  selectSkill(skillName);
  document.getElementById('arg-field').value = args;
  startResearch();
}

// ==================== LLM Config (per-user model selection) ====================
// Config lives only in the visitor's own browser (localStorage) and is sent
// with each research request — the server never persists anyone's API key.

// Per-provider model catalog.  Values pulled from /api/llm/default on page
// load so the server-owner can update the list without touching frontend code.
var LLM_PROVIDER_PRESETS = {
  deepseek: {
    label: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    models: []   // populated from /api/llm/default
  },
  kimi: {
    label: 'Kimi（Moonshot）',
    base_url: 'https://api.moonshot.cn/v1',
    models: ['kimi-k3', 'kimi-k3-0905-preview', 'kimi-k2-0905-preview']
  },
  zhipu: {
    label: '智谱（GLM）',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    models: ['glm-5.2', 'glm-4.7', 'glm-4.7-flash', 'glm-4.5', 'glm-4.5-air']
  },
  hunyuan: {
    label: '腾讯混元',
    base_url: 'https://api.hunyuan.cloud.tencent.com/v1',
    models: ['hunyuan-turbo', 'hunyuan-pro', 'hunyuan-lite']
  },
  openai: {
    label: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'o3-mini']
  },
  openrouter: {
    label: 'OpenRouter',
    base_url: 'https://openrouter.ai/api/v1',
    models: ['deepseek/deepseek-chat', 'anthropic/claude-sonnet-4', 'google/gemini-2.5-pro']
  },
  ollama: {
    label: 'Ollama（本地）',
    base_url: 'http://localhost:11434/v1',
    models: ['qwen2.5:14b', 'qwen2.5:7b', 'llama3.1:8b', 'deepseek-r1:8b']
  }
};
var _serverDefaultModel = '';

// Called once on page load to pull the server's supported model list.
function _initLlmDefaults(serverInfo) {
  if (serverInfo && serverInfo.supported_models && serverInfo.supported_models.length) {
    LLM_PROVIDER_PRESETS.deepseek.models = serverInfo.supported_models;
  }
  _serverDefaultModel = (serverInfo && serverInfo.model) || '';
  updatePoweredBy();
}

function updatePoweredBy() {
  var el = document.getElementById('powered-by');
  var cfg = _loadLlmConfig();
  if (cfg && cfg.model) {
    el.textContent = '模型：' + cfg.model + '（自定义）';
  } else {
    el.textContent = '模型：' + (_serverDefaultModel || 'deepseek-v4-flash') + '（服务器默认）';
  }
  updateLlmNotice();
}

// Red reminder for visitors who haven't configured their own provider/key.
// Shown until any provider config is saved; click opens the settings modal.
function updateLlmNotice() {
  var notice = document.getElementById('llm-notice');
  if (!notice) return;
  var cfg = _loadLlmConfig();
  var configured = !!(cfg && (cfg.api_key || cfg.base_url || cfg.model));
  notice.style.display = configured ? 'none' : 'block';
}

// ── localStorage helpers ──────────────────────────────────
function _loadLlmConfig() {
  try {
    return JSON.parse(localStorage.getItem('ai_berkshire_llm') || 'null');
  } catch(e) { return null; }
}

function _saveLlmConfig(cfg) {
  if (cfg) localStorage.setItem('ai_berkshire_llm', JSON.stringify(cfg));
  else localStorage.removeItem('ai_berkshire_llm');
}

// Build the per-request config object sent to the server.
// Returns null when user wants server defaults.
function getLlmConfig() {
  var cfg = _loadLlmConfig();
  if (!cfg) return null;
  var out = {};
  if (cfg.model) out.model = cfg.model;
  if (cfg.base_url) out.base_url = cfg.base_url;
  if (cfg.api_key) out.api_key = cfg.api_key;
  return Object.keys(out).length ? out : null;
}

// ── Auth headers ──────────────────────────────────────────
function getAuthHeaders() {
  var jwt = localStorage.getItem('ai_berkshire_jwt') || '';
  if (!jwt) return {};
  return { 'Authorization': 'Bearer ' + jwt };
}

function getWsToken() {
  return localStorage.getItem('ai_berkshire_jwt') || '';
}

async function loginWithToken(secret) {
  var res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: secret })
  });
  if (!res.ok) {
    var err = await res.json().catch(function() { return {}; });
    throw new Error(err.detail || '登录失败');
  }
  var data = await res.json();
  if (data.access_token) {
    localStorage.setItem('ai_berkshire_jwt', data.access_token);
  }
  return data;
}

// ── Modal ─────────────────────────────────────────────────
function openLlmModal() {
  var cfg = _loadLlmConfig();
  var preset = (cfg && cfg._preset) || '';

  // Restore form
  document.getElementById('llm-preset').value = preset;
  document.getElementById('llm-model').value = (cfg && cfg.model) || '';
  document.getElementById('llm-base-url').value = (cfg && cfg.base_url) || '';
  // Keep the stored key — wiping it here silently drops the key on re-save
  document.getElementById('llm-api-key').value = (cfg && cfg.api_key) || '';

  // Trigger UI refresh
  onProviderChange();
  // If we have a stored preset, select the matching model in dropdown
  if (preset && cfg && cfg.model) {
    var sel = document.getElementById('llm-model-select');
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === cfg.model) {
        sel.value = cfg.model;
        break;
      }
    }
  }

  document.getElementById('llm-test-result').textContent = '';
  document.getElementById('llm-modal').style.display = 'flex';
}

function closeLlmModal() {
  document.getElementById('llm-modal').style.display = 'none';
}

// When provider dropdown changes, rebuild the model dropdown and show/hide fields.
function onProviderChange() {
  var preset = document.getElementById('llm-preset').value;
  var modelSelect = document.getElementById('llm-model-select');
  var modelInput = document.getElementById('llm-model');
  var customFields = document.getElementById('llm-custom-fields');

  if (!preset) {
    // Server default — hide everything
    modelSelect.style.display = 'none';
    modelInput.style.display = 'none';
    customFields.style.display = 'none';
    return;
  }

  var info = LLM_PROVIDER_PRESETS[preset];
  if (info && info.models && info.models.length) {
    // Known provider with a model list — show dropdown
    modelSelect.innerHTML = '<option value="">请选择模型</option>';
    info.models.forEach(function(m) {
      modelSelect.innerHTML += '<option value="' + m + '">' + m + '</option>';
    });
    modelSelect.style.display = 'block';
    modelInput.style.display = 'none';
  } else {
    // Custom or provider without a known list — show free-form input
    modelSelect.style.display = 'none';
    modelInput.style.display = 'block';
  }

  if (preset === 'custom') {
    customFields.style.display = 'block';
  } else {
    // Pre-fill base_url from preset
    document.getElementById('llm-base-url').value = (info && info.base_url) || '';
    customFields.style.display = 'block';  // always show so user can override key
  }
}

// When a model is picked from the dropdown, sync it to the hidden input for save.
function onModelSelectChange() {
  document.getElementById('llm-model').value = document.getElementById('llm-model-select').value;
}

// ── Save / Reset / Test ───────────────────────────────────
function saveLlmConfig() {
  var preset = document.getElementById('llm-preset').value;
  var model = document.getElementById('llm-model').value.trim();
  var baseUrl = document.getElementById('llm-base-url').value.trim();
  var apiKey = document.getElementById('llm-api-key').value.trim();

  if (!preset) {
    // User chose "server default" — clear all config
    _saveLlmConfig(null);
    updatePoweredBy();
    closeLlmModal();
    showToast('已恢复服务器默认模型', 'info');
    return;
  }

  if (!model && preset !== 'custom') {
    showToast('请选择模型', 'error');
    return;
  }

  // Non-DeepSeek cloud providers reject the server's DeepSeek key — the
  // visitor MUST supply their own key, otherwise every request will 401.
  if (!apiKey && preset !== 'custom' && preset !== 'deepseek') {
    showToast('使用「' + (LLM_PROVIDER_PRESETS[preset] ? LLM_PROVIDER_PRESETS[preset].label : preset) +
      '」需要填写你自己的 API Key（不填会拿服务器 DeepSeek Key 去请求，必然 401）', 'error');
    return;
  }

  var cfg = { _preset: preset };
  if (model) cfg.model = model;
  if (baseUrl) cfg.base_url = baseUrl;
  if (apiKey) cfg.api_key = apiKey;

  _saveLlmConfig(cfg);
  updatePoweredBy();
  closeLlmModal();
  showToast('模型配置已保存', 'info');
}

function resetLlmConfig() {
  document.getElementById('llm-preset').value = '';
  document.getElementById('llm-model').value = '';
  document.getElementById('llm-model-select').innerHTML = '<option value="">请选择模型</option>';
  document.getElementById('llm-base-url').value = '';
  document.getElementById('llm-api-key').value = '';
  document.getElementById('llm-test-result').textContent = '';
  onProviderChange();
  _saveLlmConfig(null);
  updatePoweredBy();
  showToast('已清除所有自定义配置', 'info');
}

async function testLlmConfig() {
  var resultEl = document.getElementById('llm-test-result');
  var btn = document.getElementById('btn-llm-test');

  var cfg = {};
  var model = document.getElementById('llm-model').value.trim();
  var baseUrl = document.getElementById('llm-base-url').value.trim();
  var apiKey = document.getElementById('llm-api-key').value.trim();
  if (model) cfg.model = model;
  if (baseUrl) cfg.base_url = baseUrl;
  if (apiKey) cfg.api_key = apiKey;

  if (!cfg.model && !cfg.base_url && !cfg.api_key) {
    resultEl.textContent = '请先填写配置';
    resultEl.className = 'llm-test-result err';
    return;
  }

  resultEl.textContent = '测试中...';
  resultEl.className = 'llm-test-result';
  btn.disabled = true;

  try {
    var url = '/api/llm/test';
    var res = await fetch(url, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
      body: JSON.stringify({ llm_config: cfg })
    });
    if (res.status === 401 || res.status === 403) {
      // JWT expired (24h TTL) — say so instead of a misleading "连接失败"
      localStorage.removeItem('ai_berkshire_jwt');
      resultEl.textContent = '✗ 登录已过期（令牌24小时有效），即将跳转重新登录...';
      resultEl.className = 'llm-test-result err';
      setTimeout(function() { location.reload(); }, 1500);
      return;
    }
    if (!res.ok) {
      var errText = await res.text().catch(function() { return ''; });
      resultEl.textContent = '✗ 服务器返回 ' + res.status + '：' + errText.slice(0, 200);
      resultEl.className = 'llm-test-result err';
      return;
    }
    var data = await res.json();
    if (data.ok) {
      resultEl.textContent = '✓ 连接成功！模型 ' + (data.model || '?') + ' 回复: ' + (data.reply || '');
      resultEl.className = 'llm-test-result ok';
    } else {
      resultEl.textContent = '✗ ' + (data.error || '连接失败');
      resultEl.className = 'llm-test-result err';
    }
  } catch(e) {
    resultEl.textContent = '✗ 网络错误: ' + e.message;
    resultEl.className = 'llm-test-result err';
  } finally {
    btn.disabled = false;
  }
}

// ==================== Input Validation & History ====================
// Per-skill format checks. Return an error message, or null when OK.
var ARG_VALIDATORS = {
  'portfolio-review': function(a) {
    return a.indexOf('%') >= 0 ? null : '持仓清单需要包含仓位百分比，如：腾讯30%,美团20%,现金30%';
  },
  'earnings-review': function(a) {
    return (/\s/.test(a.trim()) || a.indexOf('最新') >= 0) ? null : '需要同时提供公司名和季度，如：腾讯 2025Q4 / PDD 最新';
  },
  'earnings-team': function(a) {
    return (/\s/.test(a.trim()) || a.indexOf('最新') >= 0) ? null : '需要同时提供公司名和季度，如：腾讯 2025Q4';
  },
  'management-deep-dive': function(a) {
    return /\s/.test(a.trim()) ? null : '需要同时提供管理层姓名和公司名，如：王兴 美团';
  }
};

function validateArgs(skill, args) {
  if (!args) {
    // daily-briefing explicitly supports empty input (今日日报)
    if (skill.name === 'daily-briefing') return null;
    return '请输入分析目标，如：' + (skill.input_hint || '公司名');
  }
  var v = ARG_VALIDATORS[skill.name];
  return v ? v(args) : null;
}

function saveArgHistory(args) {
  if (!args) return;
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_arg_history') || '[]');
    list = list.filter(function(x) { return x !== args; });
    list.unshift(args);
    localStorage.setItem('ai_berkshire_arg_history', JSON.stringify(list.slice(0, 20)));
    renderArgHistory();
  } catch(e) {}
}

function renderArgHistory() {
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_arg_history') || '[]');
    document.getElementById('arg-history').innerHTML = list.map(function(a) {
      return '<option value="' + escHtml(a) + '"></option>';
    }).join('');
  } catch(e) {}
}

// ==================== Progress Timer ====================
function startProgressTimer() {
  stopProgressTimer();
  _progressStartTs = Date.now();
  var el = document.getElementById('progress-timer');
  el.textContent = '0:00';
  _progressTimer = setInterval(function() {
    var sec = Math.floor((Date.now() - _progressStartTs) / 1000);
    var m = Math.floor(sec / 60), s = sec % 60;
    el.textContent = m + ':' + String(s).padStart(2, '0');
  }, 1000);
}

function stopProgressTimer() {
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
}

// ==================== Research ====================
async function startResearch() {
  var args = document.getElementById('arg-field').value.trim();
  if (!currentSkill) { showToast('请先选择技能', 'warning'); return; }

  var err = validateArgs(currentSkill, args);
  if (err) {
    showToast(err, 'warning');
    document.getElementById('input-hint').textContent = err;
    return;
  }

  var streamMode = document.getElementById('stream-mode').checked;
  var btn = document.getElementById('btn-research');
  var cancelBtn = document.getElementById('btn-cancel');
  btn.disabled = true;
  btn.textContent = '研究中...';
  cancelBtn.style.display = 'inline-block';
  cancelBtn.disabled = false;

  saveArgHistory(args);

  // Reset state
  _userCancelled = false;
  currentTaskId = null;
  _wsShouldReconnect = false;
  if (_researchAbort) { _researchAbort.abort(); _researchAbort = null; }
  if (ws) { ws.close(); ws = null; }

  // Hide other areas
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('report-content').innerHTML = '';
  document.getElementById('report-meta').style.display = 'none';
  document.getElementById('restore-banner').style.display = 'none';
  hideToc();
  currentReport = '';

  // Show progress
  var progressArea = document.getElementById('progress-area');
  progressArea.style.display = 'block';
  document.getElementById('progress-title').textContent =
    currentSkill.display_name + ': ' + (args || '今日日报');
  document.getElementById('progress-status').textContent = '运行中';
  document.getElementById('progress-status').className = 'status-badge running';
  document.getElementById('progressCount').textContent = '';
  startProgressTimer();

  // Build agent rows (mirror backend naming)
  var agentNames;
  if (currentSkill.series_mode && currentSkill.series_topics && currentSkill.series_topics.length) {
    agentNames = currentSkill.series_topics.map(function(t, i) { return 'article-' + (i + 1); });
  } else if (currentSkill.is_multi_agent && currentSkill.agents) {
    agentNames = currentSkill.agents;
  } else {
    agentNames = ['default'];
  }
  var agentDiv = document.getElementById('agent-progress');
  agentDiv.innerHTML = agentNames.map(function(name, i) {
    var topic = '';
    if (currentSkill.series_mode && currentSkill.series_topics && currentSkill.series_topics[i]) {
      topic = ' <span style="color:var(--text-secondary);font-size:0.7rem">' + escHtml(currentSkill.series_topics[i]) + '</span>';
    }
    return '<div class="agent-row" id="agent-' + i + '">' +
      '<div class="agent-dot pending"></div>' +
      '<span>' + escHtml(name) + topic + '</span>' +
      '<span style="color:var(--text-secondary);font-size:0.7rem">等待中</span></div>';
  }).join('');

  // Stream mode now works for ALL skills via WebSocket:
  // single-agent streams chunks; multi-agent/series stream per-agent progress.
  if (streamMode) {
    await streamResearch(args);
  } else {
    await batchResearch(args);
  }
}

function cancelResearch() {
  _userCancelled = true;
  _wsShouldReconnect = false;
  // Ask the server to actually stop the LLM calls
  if (currentTaskId) {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    fetch('/api/cancel/' + currentTaskId, { method: 'POST', headers: headers })
      .catch(function() {});
  }
  if (_researchAbort) {
    _researchAbort.abort();
    showToast('已取消研究', 'warning');
  }
  if (ws) {
    ws.close();
    ws = null;
    showToast('已取消研究', 'warning');
  }
  finishResearch(false, '已取消');
  document.getElementById('btn-cancel').style.display = 'none';
}

async function streamResearch(args) {
  _wsShouldReconnect = true;
  _wsReconnectAttempts = 0;
  _streamResearchImpl(args);
}

function _streamResearchImpl(args) {
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var url = proto + '//' + location.host + '/ws/research/' + currentSkill.name;

  // Timeout guard: if no data within 90s, fall back to batch mode
  var _wsTimeout = setTimeout(function() {
    if (!currentReport && _wsShouldReconnect) {
      showToast('WebSocket响应超时，自动切换到普通模式', 'warning');
      _wsShouldReconnect = false;
      if (ws) { ws.close(); ws = null; }
      batchResearch(args);
    }
  }, 90000);

  try {
    ws = new WebSocket(url);
    ws.onopen = function() {
      _wsReconnectAttempts = 0;
      var msg = { arguments: args };
      msg.token = getWsToken();
      var llmConfig = getLlmConfig();
      if (llmConfig) msg.llm_config = llmConfig;
      ws.send(JSON.stringify(msg));
    };

    ws.onmessage = function(event) {
      clearTimeout(_wsTimeout);
      var data = JSON.parse(event.data);
      if (data.type === 'started') {
        currentTaskId = data.task_id;
      } else if (data.type === 'chunk') {
        currentReport += data.content;
        document.getElementById('progress-area').style.display = 'none';
        document.getElementById('result-area').style.display = 'block';
        scheduleRender();
      } else if (data.type === 'progress') {
        updateAgentProgress(data.agent, data.status, data.message);
      } else if (data.type === 'complete') {
        _wsShouldReconnect = false;
        if (data.report) currentReport = data.report;
        finishResearch(true, null, { duration: data.duration_seconds, tokens: data.tokens });
      } else if (data.type === 'error') {
        _wsShouldReconnect = false;
        finishResearch(false, data.message);
      }
    };

    ws.onerror = function() {
      // Will trigger onclose, handle reconnect there
    };
    ws.onclose = function(event) {
      if (_wsShouldReconnect && _wsReconnectAttempts < _WS_MAX_RECONNECT) {
        var delay = _WS_RECONNECT_DELAYS[_wsReconnectAttempts] || 10000;
        _wsReconnectAttempts++;
        showToast('连接断开，' + Math.round(delay/1000) + '秒后重试 (' + _wsReconnectAttempts + '/' + _WS_MAX_RECONNECT + ')', 'warning');
        setTimeout(function() {
          if (_wsShouldReconnect) _streamResearchImpl(args);
        }, delay);
      } else if (_wsShouldReconnect) {
        // Max retries reached — fall back to batch mode
        if (currentReport) {
          finishResearch(true);
        } else {
          showToast('WebSocket连接失败，自动切换到普通模式', 'warning');
          _wsShouldReconnect = false;
          if (ws) { ws.close(); ws = null; }
          batchResearch(args);
          return;
        }
      } else if (currentReport && !document.getElementById('result-area').style.display) {
        finishResearch(true);
      }
    };
  } catch (e) {
    finishResearch(false, e.message);
  }
}

// Throttled streaming render: re-parse the whole report at most every 150ms
// instead of on every token, which keeps long reports responsive.
function scheduleRender() {
  if (_renderScheduled) return;
  _renderScheduled = true;
  setTimeout(function() {
    _renderScheduled = false;
    renderReport(currentReport);
    var resultArea = document.getElementById('result-area');
    resultArea.scrollTop = resultArea.scrollHeight;
  }, 150);
}

async function batchResearch(args) {
  _researchAbort = new AbortController();
  // 30-minute budget: multi-agent / series runs legitimately take a long time.
  // On timeout we no longer report failure — the server keeps running and the
  // report will land in 历史报告.
  var timeout = setTimeout(function() { if(_researchAbort) _researchAbort.abort(); }, 1800000);
  try {
    var headers = { 'Content-Type': 'application/json' };
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/research', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({
        skill_name: currentSkill.name,
        arguments: args,
        stream: false,
        llm_config: getLlmConfig() || undefined
      }),
      signal: _researchAbort.signal
    });
    clearTimeout(timeout);
    var data = await res.json();
    if (data.task_id) currentTaskId = data.task_id;

    if (data.agent_results) {
      var total = data.agent_results.length;
      var done = data.agent_results.filter(function(r) { return r.status === 'completed' || r.status === 'success'; }).length;
      document.getElementById('progressCount').textContent = '完成 ' + done + '/' + total + ' Agent';
      data.agent_results.forEach(function(r, i) {
        updateAgentProgress(r.agent_name || r.name || ('agent-' + i), r.status, r.status === 'completed' ? '完成' : (r.error || ''));
      });
    }

    if (data.status === 'completed') {
      currentReport = data.report;
      updateAllAgents('completed');
      finishResearch(true, null, { duration: data.duration_seconds, tokens: data.tokens });
    } else {
      finishResearch(false, data.error || 'Unknown error');
    }
  } catch (e) {
    clearTimeout(timeout);
    if (e.name === 'AbortError') {
      if (_userCancelled) {
        finishResearch(false, '已取消');
      } else {
        showToast('等待超时：任务仍在后台运行，完成后可在「历史报告」中查看', 'warning');
        finishResearch(false, '等待超时，任务仍在后台运行，完成后请查看历史报告');
      }
    } else {
      finishResearch(false, e.message);
    }
  } finally {
    _researchAbort = null;
  }
}

function agentRowNames() {
  if (currentSkill && currentSkill.series_mode && currentSkill.series_topics && currentSkill.series_topics.length) {
    return currentSkill.series_topics.map(function(t, i) { return 'article-' + (i + 1); });
  }
  return (currentSkill && currentSkill.agents) || ['default'];
}

function updateAgentProgress(name, status, message) {
  var agentNames = agentRowNames();
  var idx = agentNames.indexOf(name);
  if (idx < 0) {
    // 'system' and other meta progress — surface in the count line
    if (message) document.getElementById('progressCount').textContent = message;
    return;
  }

  var row = document.getElementById('agent-' + idx);
  if (!row) return;

  var dot = row.querySelector('.agent-dot');
  var statusText = row.querySelector('span:last-child');

  dot.className = 'agent-dot ' + status;
  if (status === 'completed') {
    statusText.textContent = '完成';
    statusText.style.color = 'var(--accent-green)';
  } else if (status === 'running') {
    statusText.textContent = message || '分析中...';
    statusText.style.color = 'var(--accent)';
  } else if (status === 'failed') {
    statusText.textContent = message || '失败';
    statusText.style.color = 'var(--accent-red)';
  }

  var allRows = document.querySelectorAll('#agent-progress .agent-row');
  var done = 0;
  allRows.forEach(function(r) {
    if (r.querySelector('.agent-dot.completed')) done++;
  });
  document.getElementById('progressCount').textContent = '完成 ' + done + '/' + allRows.length + ' Agent';
}

function updateAllAgents(status) {
  agentRowNames().forEach(function(name, i) {
    var row = document.getElementById('agent-' + i);
    if (!row) return;
    row.querySelector('.agent-dot').className = 'agent-dot ' + status;
    row.querySelector('span:last-child').textContent = status === 'completed' ? '完成' : '';
  });
}

function finishResearch(success, errorMsg, meta) {
  stopProgressTimer();
  var btn = document.getElementById('btn-research');
  var cancelBtn = document.getElementById('btn-cancel');
  btn.disabled = false;
  btn.textContent = currentSkill && currentSkill.is_multi_agent
    ? '启动 ' + currentSkill.agent_count + ' Agent 并行研究'
    : '开始研究';
  cancelBtn.style.display = 'none';

  var statusEl = document.getElementById('progress-status');
  if (success) {
    statusEl.textContent = '完成';
    statusEl.className = 'status-badge completed';
  } else {
    statusEl.textContent = '失败';
    statusEl.className = 'status-badge failed';
    if (errorMsg) {
      // Keep whatever partial content was already generated — it is often
      // still useful — and append the error instead of wiping the report.
      if (currentReport) {
        currentReport += '\n\n---\n\n> ⚠ **中断**：' + errorMsg + '（以上为已完成的部分内容）\n';
      } else {
        currentReport = '# 错误\n\n' + errorMsg;
      }
      showToast(errorMsg, 'error');
    }
  }

  if (currentReport && currentReport.indexOf('# 错误') !== 0) {
    document.getElementById('progress-area').style.display = 'none';
    document.getElementById('result-area').style.display = 'block';
    renderReport(currentReport);
    buildToc();

    // Duration / token cost line
    if (meta && (meta.duration || (meta.tokens && meta.tokens.total_tokens))) {
      var parts = [];
      if (meta.duration) parts.push('耗时 ' + fmtDuration(meta.duration));
      var tk = fmtTokens(meta.tokens);
      if (tk) parts.push('消耗 ' + tk);
      var metaEl = document.getElementById('report-meta');
      metaEl.textContent = parts.join(' · ');
      metaEl.style.display = 'block';
    }

    try {
      sessionStorage.setItem('ai_berkshire_report', currentReport);
      sessionStorage.setItem('ai_berkshire_report_meta', JSON.stringify({
        skill: currentSkill ? currentSkill.name : '',
        ts: Date.now()
      }));
    } catch(e) {}
  }

  _wsShouldReconnect = false;
  if (ws) { ws.close(); ws = null; }
  localStorage.setItem('ai_berkshire_skill', currentSkill ? currentSkill.name : '');
}

// ==================== Rendering / TOC ====================
function renderReport(md) {
  document.getElementById('report-content').innerHTML =
    DOMPurify.sanitize(marked.parse(md)) +
    '<div class="ai-disclaimer">该数据由AI在数据的基础上进行分析，仅供参考</div>';
}

function buildToc() {
  var toc = document.getElementById('report-toc');
  var headings = document.querySelectorAll('#report-content h1, #report-content h2, #report-content h3');
  if (!headings.length) { hideToc(); return; }
  var html = '<div class="toc-title">目录</div><ul>';
  headings.forEach(function(h, i) {
    var id = 'toc-h-' + i;
    h.id = id;
    var level = h.tagName.toLowerCase();
    html += '<li class="toc-' + level + '"><a href="javascript:void(0)" data-target="' + id + '" onclick="tocJump(\'' + id + '\')">' +
      escHtml(h.textContent) + '</a></li>';
  });
  html += '</ul>';
  toc.innerHTML = html;
}

function tocJump(id) {
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function toggleToc() {
  var toc = document.getElementById('report-toc');
  if (toc.style.display === 'none' || !toc.innerHTML) {
    if (!toc.innerHTML) buildToc();
    if (!toc.innerHTML) { showToast('报告暂无章节标题', 'warning'); return; }
    toc.style.display = 'block';
  } else {
    toc.style.display = 'none';
  }
}

function hideToc() {
  var toc = document.getElementById('report-toc');
  toc.style.display = 'none';
  toc.innerHTML = '';
}

// Scroll-spy: highlight current section in TOC
document.addEventListener('scroll', function(e) {
  var toc = document.getElementById('report-toc');
  if (!toc || toc.style.display === 'none') return;
  if (e.target && e.target.id !== 'result-area') return;
  var headings = document.querySelectorAll('#report-content h1, #report-content h2, #report-content h3');
  var current = null;
  headings.forEach(function(h) {
    if (h.getBoundingClientRect().top < 140) current = h.id;
  });
  toc.querySelectorAll('a').forEach(function(a) {
    a.classList.toggle('current', a.dataset.target === current);
  });
}, true);

// ==================== Report Actions ====================
function copyReport() {
  if (!currentReport) { showToast('没有可复制的内容', 'warning'); return; }
  navigator.clipboard.writeText(currentReport).then(function() {
    showToast('报告已复制到剪贴板', 'success');
  }).catch(function() {
    showToast('复制失败', 'error');
  });
}

function _reportFileBase() {
  var skill = currentSkill ? currentSkill.name : 'report';
  var args = (document.getElementById('arg-field').value || '').trim()
    .replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 24);
  var ts = new Date().toISOString().slice(0,19).replace(/:/g, '-');
  return (args ? skill + '_' + args : skill) + '_' + ts;
}

function downloadReport() {
  if (!currentReport) { showToast('没有可下载的内容', 'warning'); return; }
  var blob = new Blob([currentReport], { type: 'text/markdown' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = _reportFileBase() + '.md';
  a.click();
  URL.revokeObjectURL(url);
  showToast('已下载', 'success');
}

function exportHtml() {
  if (!currentReport) { showToast('没有可导出的内容', 'warning'); return; }
  var body = DOMPurify.sanitize(marked.parse(currentReport));
  var title = _reportFileBase();
  var html = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
    '<title>' + escHtml(title) + '</title>' +
    '<style>body{font-family:\'Noto Serif SC\',\'Source Han Serif SC\',STSong,Georgia,serif;' +
    'max-width:760px;margin:0 auto;padding:32px 24px 60px;background:#f7f3e9;color:#3d3d3d;line-height:1.85}' +
    'h1{font-size:1.35rem;border-bottom:1px solid #d4c5a9;padding-bottom:10px;font-weight:400}' +
    'h2{color:#6b8e6b;font-weight:400;margin-top:28px}h3{color:#c4a77d;font-weight:400}' +
    'table{width:100%;border-collapse:collapse;margin:14px 0;font-size:0.85rem}' +
    'th{background:#f0ebe0;padding:8px 12px;text-align:left;border-bottom:1px solid #d4c5a9;font-weight:400}' +
    'td{padding:7px 12px;border-bottom:1px solid #e5dac8}' +
    'code{background:#f0ebe0;padding:2px 6px;border-radius:2px;color:#6b8e6b}' +
    'pre{background:#f0ebe0;padding:14px;border-radius:4px;overflow-x:auto}' +
    'blockquote{border-left:2px solid #c4a77d;padding:8px 18px;margin:14px 0;color:#8c8273;font-style:italic}' +
    'hr{border:none;border-top:1px solid #e5dac8;margin:28px 0}</style></head>' +
    '<body>' + body + '</body></html>';
  var blob = new Blob([html], { type: 'text/html' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = title + '.html';
  a.click();
  URL.revokeObjectURL(url);
  showToast('已导出 HTML', 'success');
}

function printReport() {
  if (!currentReport) { showToast('没有可打印的内容', 'warning'); return; }
  window.print();
}

function clearResult() {
  currentReport = '';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('report-content').innerHTML = '';
  document.getElementById('report-meta').style.display = 'none';
  document.getElementById('restore-banner').style.display = 'none';
  hideToc();
  document.getElementById('empty-state').style.display = 'flex';
  try {
    sessionStorage.removeItem('ai_berkshire_report');
    sessionStorage.removeItem('ai_berkshire_report_meta');
  } catch(e) {}
}

// ==================== History ====================
async function showHistory() {
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('progress-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';

  var historyArea = document.getElementById('history-area');
  var list = document.getElementById('history-list');
  document.getElementById('historySearch').value = '';
  _compareSelection.clear();
  updateCompareBtn();

  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/reports', { headers: headers });
    var data = await res.json();
    _historyData = data.reports || [];
    renderHistory(_historyData);
  } catch (e) {
    list.innerHTML = '<p style="color:var(--accent-red)">加载失败</p>';
  }

  historyArea.style.display = 'block';
}

function filterHistory() {
  var q = document.getElementById('historySearch').value.toLowerCase();
  var filtered = _historyData.filter(function(r) {
    var hay = ((r.name || '') + ' ' + (r.arguments || '') + ' ' +
      (r.skill_name || '') + ' ' + skillDisplayName(r.skill_name || '')).toLowerCase();
    return hay.indexOf(q) >= 0;
  });
  renderHistory(filtered);
}

function renderHistory(reports) {
  var list = document.getElementById('history-list');
  if (!reports.length) {
    list.innerHTML = '<p style="color:var(--text-secondary)">暂无匹配报告</p>';
    return;
  }
  list.innerHTML = reports.map(function(r) {
    var safeName = escHtml(r.name || '');
    var args = (r.arguments || '').trim();
    var title = args || r.name;
    var metaParts = [];
    if (r.skill_name) metaParts.push(skillDisplayName(r.skill_name));
    metaParts.push(new Date(r.modified).toLocaleString());
    metaParts.push((r.size/1024).toFixed(1) + 'KB');
    if (r.duration_seconds) metaParts.push('耗时 ' + fmtDuration(r.duration_seconds));
    var checked = _compareSelection.has(r.name) ? ' checked' : '';
    return '<div class="history-item" onclick="loadReport(\'' + safeName.replace(/'/g, "\\'") + '\')">' +
      '<input type="checkbox" class="compare-check" data-name="' + safeName + '"' + checked +
      ' onclick="event.stopPropagation();toggleCompareSelect(this)" title="选择两份报告进行对比">' +
      '<div class="history-item-body">' +
      '<div class="report-name">' + escHtml(title) + '</div>' +
      '<div class="report-meta">' + escHtml(metaParts.join(' | ')) + '</div>' +
      (args ? '<div class="report-filename">' + safeName + '</div>' : '') +
      '</div>' +
      '<button class="btn-delete-report" onclick="event.stopPropagation();deleteReport(\'' + safeName.replace(/'/g, "\\'") + '\')" title="删除报告">✕</button>' +
      '</div>';
  }).join('');
}

async function deleteReport(filename) {
  if (!confirm('确定删除报告「' + filename + '」？')) return;
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/reports/' + encodeURIComponent(filename), {
      method: 'DELETE', headers: headers
    });
    if (res.ok) {
      _historyData = _historyData.filter(function(r) { return r.name !== filename; });
      _compareSelection.delete(filename);
      renderHistory(_historyData.filter(function(r) {
        var q = document.getElementById('historySearch').value.toLowerCase();
        var hay = ((r.name || '') + ' ' + (r.arguments || '') + ' ' + (r.skill_name || '')).toLowerCase();
        return hay.indexOf(q) >= 0;
      }));
      updateCompareBtn();
      showToast('已删除', 'success');
    } else {
      showToast('删除失败', 'error');
    }
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ==================== Report Compare ====================
function toggleCompareSelect(checkbox) {
  var name = checkbox.dataset.name;
  if (checkbox.checked) {
    if (_compareSelection.size >= 2) {
      checkbox.checked = false;
      showToast('最多选择两份报告进行对比', 'warning');
      return;
    }
    _compareSelection.add(name);
  } else {
    _compareSelection.delete(name);
  }
  updateCompareBtn();
}

function updateCompareBtn() {
  var btn = document.getElementById('btn-compare');
  btn.disabled = _compareSelection.size !== 2;
  btn.textContent = _compareSelection.size === 2 ? '对比所选' :
    '对比所选 (' + _compareSelection.size + '/2)';
}

async function compareSelected() {
  if (_compareSelection.size !== 2) return;
  var names = Array.from(_compareSelection);
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var results = await Promise.all(names.map(function(n) {
      return fetch('/api/reports/' + encodeURIComponent(n), { headers: headers })
        .then(function(r) { return r.json(); });
    }));
    if (!results[0].content || !results[1].content) {
      showToast('报告加载失败', 'error');
      return;
    }
    document.getElementById('history-area').style.display = 'none';
    document.getElementById('compare-area').style.display = 'flex';
    document.getElementById('compare-title').textContent = names[0] + ' ⇄ ' + names[1];
    document.getElementById('compare-left').innerHTML =
      '<h3 class="compare-pane-title">' + escHtml(names[0]) + '</h3>' +
      DOMPurify.sanitize(marked.parse(results[0].content));
    document.getElementById('compare-right').innerHTML =
      '<h3 class="compare-pane-title">' + escHtml(names[1]) + '</h3>' +
      DOMPurify.sanitize(marked.parse(results[1].content));
  } catch (e) {
    showToast('对比加载失败: ' + e.message, 'error');
  }
}

function closeCompare() {
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('history-area').style.display = 'block';
}

async function loadReport(filename) {
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/reports/' + encodeURIComponent(filename), { headers: headers });
    var data = await res.json();
    if (data.content) {
      currentReport = data.content;
      document.getElementById('history-area').style.display = 'none';
      document.getElementById('result-area').style.display = 'block';
      document.getElementById('restore-banner').style.display = 'none';
      renderReport(data.content);
      buildToc();
      // Show stored run metadata if available
      var metaEl = document.getElementById('report-meta');
      if (data.meta && (data.meta.duration_seconds || (data.meta.tokens && data.meta.tokens.total_tokens))) {
        var parts = [];
        if (data.meta.arguments) parts.push('目标：' + data.meta.arguments);
        if (data.meta.duration_seconds) parts.push('耗时 ' + fmtDuration(data.meta.duration_seconds));
        var tk = fmtTokens(data.meta.tokens);
        if (tk) parts.push('消耗 ' + tk);
        metaEl.textContent = parts.join(' · ');
        metaEl.style.display = 'block';
      } else {
        metaEl.style.display = 'none';
      }
      try { sessionStorage.setItem('ai_berkshire_report', data.content); } catch(e) {}
    }
  } catch (e) {
    showToast('加载报告失败', 'error');
  }
}

// ==================== URL Pre-fill ====================
// ?skill=industry-research&arguments=商业航天 → pre-fill skill + argument
// ?company=宁德时代 → pre-fill argument field only
function prefillFromURL() {
  var params = new URLSearchParams(window.location.search);
  var skill = params.get('skill') || '';
  var args = params.get('arguments') || params.get('company') || '';

  if (!skill && !args) return false;

  if (skill) {
    var found = allSkills.find(function(s) { return s.name === skill; });
    if (found) {
      selectSkill(skill);
      if (args) document.getElementById('arg-field').value = args;
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', window.location.pathname);
      }
      if (args && params.get('auto') !== '0' && params.get('noauto') !== '1') {
        setTimeout(function() { startResearch(); }, 600);
      }
      return true;
    }
  }

  if (args) {
    document.getElementById('arg-field').value = args;
    if (window.history && window.history.replaceState) {
      window.history.replaceState({}, '', window.location.pathname);
    }
    return true;
  }

  return false;
}

// ==================== Save on unload ====================
window.addEventListener('beforeunload', function() {
  if (currentReport) {
    try {
      sessionStorage.setItem('ai_berkshire_report', currentReport);
    } catch(e) {}
  }
  if (ws) { ws.close(); }
});
