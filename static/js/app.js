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
let _agentStreamBuffers = {}; // per-agent stream buffers (event protocol v2)
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

  // Restore failed tasks
  renderFailedTasks();
  updateUploadVisibility();

  // Restore last report from sessionStorage (only if model is configured)
  try {
    var savedReport = sessionStorage.getItem('ai_berkshire_report');
    var savedMeta = JSON.parse(sessionStorage.getItem('ai_berkshire_report_meta') || '{}');
    var _cfgCheck = _loadLlmConfig();
    var _hasCfg = !!(_cfgCheck && (_cfgCheck.model || _cfgCheck.api_key || _cfgCheck.base_url));
    if (savedReport && _hasCfg) {
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

  // Enter key to submit (Shift+Enter inserts a newline)
  document.getElementById('arg-field').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      startResearch();
    }
  });

  // Input char count & hint, plus textarea auto-grow
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
    // Auto-grow with content, capped at ~160px
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
  });

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
  // Close history panel when switching skills
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';
  localStorage.setItem('ai_berkshire_skill', name);

  document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.nav-item[data-skill="' + name + '"]')
    .forEach(function(el) { el.classList.add('active'); });

  document.getElementById('skill-name').textContent = skill.name;
  document.getElementById('skill-label').textContent = skill.display_name || '';
  document.getElementById('skill-icon').textContent = CAT_ICONS[skill.category] || '?';
  document.getElementById('skill-desc').textContent = skill.description || '';
  document.getElementById('input-hint').textContent = skill.input_hint || '';
  document.getElementById('arg-field').placeholder = skill.input_hint || '描述你的分析目标...';

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
    models: []
  },
  openai: {
    label: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    models: ['gpt-5.6', 'gpt-5.5-pro', 'gpt-5.4-pro', 'gpt-5.1-codex-max', 'o4-mini']
  },
  anthropic: {
    label: 'Anthropic（Claude）',
    base_url: 'https://api.anthropic.com/v1',
    models: ['claude-opus-5', 'claude-opus-4-7-20260416', 'claude-sonnet-5',
             'claude-sonnet-4-6', 'claude-opus-4-6-20260205']
  },
  google: {
    label: 'Google（Gemini）',
    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    models: ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-pro',
             'gemini-2.5-flash', 'gemini-2.0-flash']
  },
  zhipu: {
    label: '智谱（GLM）',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    models: ['glm-5.2', 'glm-5.1', 'glm-5', 'glm-4.7', 'glm-4.6']
  },
  qwen: {
    label: '阿里（通义千问）',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: ['qwen3-max', 'qwen3-plus', 'qwen3-turbo', 'qwen-vl-max', 'qwen-vl-plus']
  },
  baidu: {
    label: '百度（文心）',
    base_url: 'https://qianfan.baidubce.com/v2',
    models: ['ernie-4.5-turbo', 'ernie-4.5', 'ernie-4.0-turbo', 'ernie-speed', 'ernie-lite']
  },
  hunyuan: {
    label: '腾讯混元',
    base_url: 'https://api.hunyuan.cloud.tencent.com/v1',
    models: ['hunyuan-turbos', 'hunyuan-t1', 'hunyuan-a13b', 'hunyuan-lite']
  },
  doubao: {
    label: '字节（豆包）',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    models: ['doubao-1.5-pro-256k', 'doubao-1.5-lite-128k', 'doubao-1.5-vision-pro']
  },
  kimi: {
    label: 'Kimi（Moonshot）',
    base_url: 'https://api.moonshot.cn/v1',
    models: ['kimi-k3', 'kimi-k2.7-code', 'kimi-k2.6', 'kimi-k2.5', 'kimi-k2-thinking-251104']
  },
  ollama: {
    label: 'Ollama（本地）',
    base_url: 'http://localhost:11434/v1',
    models: ['qwen3:14b', 'qwen3:7b', 'llama4:8b', 'deepseek-r1:8b', 'gemma3:12b']
  },
  custom: {
    label: '自定义中转站',
    base_url: '',
    models: []
  }
};
var _serverDefaultModel = '';

// Agent name → human-readable label for per-agent model UI
var AGENT_LABELS = {
  'business-analyst': '商业模式分析师',
  'financial-analyst': '财务估值分析师',
  'industry-researcher': '行业竞争分析师',
  'risk-assessor': '风险评估师',
  'company-event-scout': '公司事件侦察员',
  'regulatory-watcher': '监管政策观察员',
  'industry-peer-analyst': '行业对手分析师',
  'sentiment-tracker': '市场情绪追踪员',
  'business-interpreter': '生意本质解读者',
  'financial-auditor': '财务质量审计师',
  'competition-analyst': '竞争变化解读者',
  'risk-hunter': '风险信号猎手',
  'editor': '公众号财经编辑',
  'reader-reviewer': '投资者评审',
  'researcher': '深度研究员'
};

function _getAgentLabel(name) {
  return AGENT_LABELS[name] || name;
}

// Called once on page load to pull the server's supported model list.
function _initLlmDefaults(serverInfo) {
  // DeepSeek models come from the server (owner may add/remove DeepSeek variants).
  // Other providers' models are maintained in LLM_PROVIDER_PRESETS above.
  if (serverInfo && serverInfo.supported_models && serverInfo.supported_models.length) {
    var dsModels = serverInfo.supported_models.filter(function(m) {
      return m.indexOf('deepseek') === 0;
    });
    if (dsModels.length) LLM_PROVIDER_PRESETS.deepseek.models = dsModels;
  }
  _serverDefaultModel = (serverInfo && serverInfo.model) || '';
  updatePoweredBy();
}

function updatePoweredBy() {
  var el = document.getElementById('powered-by');
  var cfg = _loadLlmConfig();
  if (cfg && (cfg.model || cfg.base_url || cfg.api_key)) {
    var presetLabel = '';
    if (cfg._preset && LLM_PROVIDER_PRESETS[cfg._preset]) {
      presetLabel = LLM_PROVIDER_PRESETS[cfg._preset].label + ' · ';
    }
    el.textContent = '模型：' + presetLabel + (cfg.model || '自定义') + '（自定义）';
  } else if (cfg && cfg.agent_models && Object.keys(cfg.agent_models).length) {
    // Only per-agent configured — show first agent's model
    var agents = Object.keys(cfg.agent_models);
    var first = cfg.agent_models[agents[0]];
    var label = typeof first === 'string' ? first : (first.model || '自定义');
    var p = (typeof first === 'object' && first._provider) ? first._provider : '';
    el.textContent = '模型：' + (p ? p + ' · ' : '') + label + '（Agent）';
  } else {
    el.textContent = '模型：' + (_serverDefaultModel || 'DeepSeek');
  }
  updateLlmNotice();
}

// Red reminder for visitors who haven't configured their own provider/key.
// Shown until any provider config is saved; click opens the settings modal.
function updateLlmNotice() {
  var notice = document.getElementById('llm-notice');
  if (!notice) return;
  var cfg = _loadLlmConfig();
  var hasGlobal = !!(cfg && (cfg.api_key || cfg.base_url || cfg.model));
  var hasPerAgent = !!(cfg && cfg.agent_models && Object.keys(cfg.agent_models).length > 0);
  var configured = hasGlobal || hasPerAgent;
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
// per_agent_enabled / agent_models：按Agent分配不同模型（可选功能），
// 仅在 per_agent_enabled 开关开启时发送。
function getLlmConfig() {
  var cfg = _loadLlmConfig();
  if (!cfg) return null;
  var out = {};
  if (cfg.model) out.model = cfg.model;
  if (cfg.base_url) out.base_url = cfg.base_url;
  if (cfg.api_key) out.api_key = cfg.api_key;
  if (cfg.per_agent_enabled) {
    out.per_agent_enabled = true;
    if (cfg.agent_models && Object.keys(cfg.agent_models).length) {
      // Strip _provider (UI-only field) before sending
      var clean = {};
      Object.keys(cfg.agent_models).forEach(function(k) {
        var v = cfg.agent_models[k];
        if (typeof v === 'string') { clean[k] = v; return; }
        var sub = {};
        if (v.model) sub.model = v.model;
        if (v.base_url) sub.base_url = v.base_url;
        if (v.api_key) sub.api_key = v.api_key;
        if (Object.keys(sub).length) clean[k] = sub;
      });
      if (Object.keys(clean).length) out.agent_models = clean;
    }
  }
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
  var preset = (cfg && cfg._preset) || 'deepseek';

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

  // Per-agent model: rebuild table + restore
  _rebuildPerAgentTable();
  var paEnabled = !!(cfg && cfg.per_agent_enabled);
  document.getElementById('llm-per-agent-enabled').checked = paEnabled;
  // Ensure per-agent block is visible when checkbox is on (onProviderChange may have hidden it)
  if (paEnabled || (cfg && cfg.agent_models && Object.keys(cfg.agent_models).length)) {
    document.getElementById('llm-per-agent-block').style.display = 'block';
  }
  updateUploadVisibility();
  onPerAgentToggle();

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
    // Server default — hide all fields
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
      var isMulti = _MULTIMODAL_MODELS.some(function(k) { return m.toLowerCase().indexOf(k) >= 0; });
      var tag = isMulti ? ' 📎' : '';
      modelSelect.innerHTML += '<option value="' + m + '">' + m + tag + '</option>';
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
    // Per-agent block: only show for multi-agent skills (dynamically when modal opens)
  _rebuildPerAgentTable();
}

// When a model is picked from the dropdown, sync it to the hidden input for save.
function onModelSelectChange() {
  document.getElementById('llm-model').value = document.getElementById('llm-model-select').value;
}

// ── Per-agent model assignment (optional feature) ─────────
// Each agent can independently choose provider → model → base_url → api_key.
// Unconfigured agents fall back to the global (top-level) LLM config.

// Per-agent provider dropdown options: "跟随全局" + all providers from LLM_PROVIDER_PRESETS
function _paProviderOptions(selected) {
  var opts = '';
  Object.keys(LLM_PROVIDER_PRESETS).forEach(function(k) {
    var p = LLM_PROVIDER_PRESETS[k];
    opts += '<option value="' + k + '"' + (k === selected ? ' selected' : '') + '>' + escHtml(p.label) + '</option>';
  });
  return opts;
}

// Get agent names for the active skill (multi-agent only)
function _getActiveSkillAgents() {
  if (!currentSkill || !currentSkill.is_multi_agent || !currentSkill.agents) return [];
  return currentSkill.agents;
}

// Rebuild the per-agent table: agent label + provider + model + url + key per row.
// Team Lead appended at end.
function _rebuildPerAgentTable() {
  var table = document.getElementById('llm-per-agent-table');
  var block = document.getElementById('llm-per-agent-block');
  var agents = _getActiveSkillAgents();
  if (!agents.length) {
    // Still show block if user has saved per-agent config from previous session
    var _savedCfg = _loadLlmConfig();
    if (!_savedCfg || !_savedCfg.agent_models || !Object.keys(_savedCfg.agent_models).length) {
      block.style.display = 'none'; return;
    }
  }
  block.style.display = 'block';

  var cfg = _loadLlmConfig();
  var stored = (cfg && cfg.agent_models) || {};
  var allNames = agents.concat(['team-lead']);
  var rows = '';

  allNames.forEach(function(name) {
    var isTL = (name === 'team-lead');
    var label = isTL ? 'Team Lead' : _getAgentLabel(name);
    var rowClass = isTL ? ' per-agent-row-tl' : '';
    var agentCfg = stored[name] || {};
    // Support both legacy string and new dict format
    if (typeof agentCfg === 'string') agentCfg = { model: agentCfg };

    var paProvider = agentCfg._provider || '';
    var paModel = agentCfg.model || '';
    var paUrl = agentCfg.base_url || '';
    var paKey = agentCfg.api_key || '';
    var showFields = !!paProvider;  // show url/key when provider is set

    rows += '<div class="per-agent-row' + rowClass + '" data-agent="' + escHtml(name) + '">' +
      '<span class="agent-label">' + escHtml(label) + '</span>' +
      '<select class="pa-provider" onchange="_onPaProviderChange(this)" style="width:120px">' +
      _paProviderOptions(paProvider) +
      '</select>' +
      '<span class="pa-model-wrap" style="' + (showFields ? '' : 'display:none') + '">' +
      '<input type="text" class="pa-model" value="' + escHtml(paModel) +
      '" placeholder="模型名" style="width:130px">' +
      '</span>' +
      '<span class="pa-extra" style="' + (showFields ? '' : 'display:none') + '">' +
      '<input type="text" class="pa-url" value="' + escHtml(paUrl) +
      '" placeholder="Base URL" style="width:170px">' +
      '<input type="password" class="pa-key" value="' + escHtml(paKey) +
      '" placeholder="API Key" style="width:140px">' +
      '</span>' +
      '<button class="pa-test-btn" onclick="_onPaTest(this)"' + (showFields ? '' : ' style="display:none"') + '>测试</button>' +
      '<button class="pa-save-btn" onclick="_onPaSave(this)"' + (showFields ? '' : ' style="display:none"') + '>保存</button>' +
      '<span class="pa-test-result"></span>' +
      '</div>';
  });

  if (rows) {
    rows += '<p class="modal-tip" style="margin-top:8px">未指定服务商的Agent将使用上方全局模型配置。</p>';
  }
  table.innerHTML = rows;
}

// When a per-agent provider changes, show/hide model/url/key fields and auto-fill URL
function _onPaProviderChange(sel) {
  var row = sel.closest('.per-agent-row');
  var provider = sel.value;
  var modelWrap = row.querySelector('.pa-model-wrap');
  var extra = row.querySelector('.pa-extra');
  if (provider) {
    modelWrap.style.display = '';
    extra.style.display = '';
    // Auto-fill base_url from provider preset
    var info = LLM_PROVIDER_PRESETS[provider];
    var urlInput = row.querySelector('.pa-url');
    if (info && info.base_url && !urlInput.value) {
      urlInput.value = info.base_url;
    }
    // Read current model value from whichever element exists
    var curModelEl = row.querySelector('.pa-model-sel') || row.querySelector('.pa-model');
    var curModel = (curModelEl && curModelEl.value) || '';
    // If provider has known models, swap to dropdown
    if (info && info.models && info.models.length) {
      var selHtml = '<select class="pa-model-sel" style="width:130px"><option value="">请选择</option>';
      info.models.forEach(function(m) {
        var isMulti = _MULTIMODAL_MODELS.some(function(k) { return m.toLowerCase().indexOf(k) >= 0; });
        var tag = isMulti ? ' 📎' : '';
        selHtml += '<option value="' + m + '"' + (m === curModel ? ' selected' : '') + '>' + m + tag + '</option>';
      });
      selHtml += '</select>';
      modelWrap.innerHTML = selHtml;
    } else {
      modelWrap.innerHTML = '<input type="text" class="pa-model" value="' + escHtml(curModel) +
        '" placeholder="模型名" style="width:130px">';
    }
  } else {
    modelWrap.style.display = 'none';
    extra.style.display = 'none';
  }
  // Show/hide test/save buttons + clear result
  var testBtn = row.querySelector('.pa-test-btn');
  var saveBtn = row.querySelector('.pa-save-btn');
  var testResult = row.querySelector('.pa-test-result');
  if (testBtn) testBtn.style.display = provider ? '' : 'none';
  if (saveBtn) saveBtn.style.display = provider ? '' : 'none';
  if (testResult) testResult.textContent = '';
}

// Per-agent test connection: reads current row config, calls /api/llm/test
async function _onPaTest(btn) {
  var row = btn.closest('.per-agent-row');
  var resultEl = row.querySelector('.pa-test-result');
  var modelEl = row.querySelector('.pa-model-sel') || row.querySelector('.pa-model');
  var model = (modelEl || {}).value || '';
  var url = (row.querySelector('.pa-url') || {}).value || '';
  var key = (row.querySelector('.pa-key') || {}).value || '';

  if (!model) { resultEl.textContent = '请填写模型名'; resultEl.className = 'pa-test-result err'; return; }

  resultEl.textContent = '测试中...';
  resultEl.className = 'pa-test-result';
  btn.disabled = true;

  var cfg = { model: model };
  if (url) cfg.base_url = url;
  if (key) cfg.api_key = key;

  try {
    var res = await fetch('/api/llm/test', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
      body: JSON.stringify({ llm_config: cfg })
    });
    if (res.status === 401 || res.status === 403) {
      resultEl.textContent = '登录过期';
      resultEl.className = 'pa-test-result err';
      return;
    }
    var data = await res.json();
    if (data.ok) {
      if (data.tools_supported === false) {
        resultEl.textContent = '⚠ 可连接但不支持工具调用，该Agent取数时将回退默认模型';
        resultEl.className = 'pa-test-result err';
      } else {
        resultEl.textContent = '✓ ' + (data.reply || '').slice(0, 30);
        resultEl.className = 'pa-test-result ok';
      }
    } else {
      resultEl.textContent = '✗ ' + (data.error || '失败').slice(0, 40);
      resultEl.className = 'pa-test-result err';
    }
  } catch(e) {
    resultEl.textContent = '✗ ' + e.message.slice(0, 40);
    resultEl.className = 'pa-test-result err';
  } finally {
    btn.disabled = false;
  }
}

// Per-agent save: reads current row config, persists to localStorage immediately
function _onPaSave(btn) {
  var row = btn.closest('.per-agent-row');
  var agent = row.dataset.agent;
  var resultEl = row.querySelector('.pa-test-result');
  var modelEl = row.querySelector('.pa-model-sel') || row.querySelector('.pa-model');
  var model = (modelEl || {}).value || '';
  var url = (row.querySelector('.pa-url') || {}).value || '';
  var key = (row.querySelector('.pa-key') || {}).value || '';
  var provider = (row.querySelector('.pa-provider') || {}).value || '';

  if (!model) { resultEl.textContent = '请填写模型名'; resultEl.className = 'pa-test-result err'; return; }

  var cfg = _loadLlmConfig() || {};
  var agentModels = cfg.agent_models || {};
  var sub = { model: model };
  if (url) sub.base_url = url;
  if (key) sub.api_key = key;
  if (provider) sub._provider = provider;
  agentModels[agent] = sub;
  cfg.agent_models = agentModels;
  cfg.per_agent_enabled = true;
  _saveLlmConfig(cfg);
  updateLlmNotice();
  updatePoweredBy();

  resultEl.textContent = '✓ 已保存';
  resultEl.className = 'pa-test-result ok';
  setTimeout(function() { resultEl.textContent = ''; }, 1500);
}

// Toggle per-agent fields visibility
function onPerAgentToggle() {
  var enabled = document.getElementById('llm-per-agent-enabled').checked;
  // Rebuild table when enabling, so it always reflects current skill's agents
  if (enabled) _rebuildPerAgentTable();
  document.getElementById('llm-per-agent-fields').style.display = enabled ? 'block' : 'none';
}

// ── Save / Reset / Test ───────────────────────────────────
function saveLlmConfig() {
  var preset = document.getElementById('llm-preset').value;
  var model = document.getElementById('llm-model').value.trim();
  var baseUrl = document.getElementById('llm-base-url').value.trim();
  var apiKey = document.getElementById('llm-api-key').value.trim();

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

  // Per-agent model config: read provider + model + url + key from each row
  var perAgentEnabled = document.getElementById('llm-per-agent-enabled').checked;
  var agentModels = {};
  if (perAgentEnabled) {
    var rows = document.querySelectorAll('#llm-per-agent-table .per-agent-row');
    for (var ri = 0; ri < rows.length; ri++) {
      var row = rows[ri];
      var agent = row.dataset.agent;
      var provider = (row.querySelector('.pa-provider') || {}).value || '';
      if (!provider) continue;  // no provider = use global config
      // Read model (could be select or text input)
      var modelEl = row.querySelector('.pa-model-sel') || row.querySelector('.pa-model');
      var model = (modelEl || {}).value || '';
      if (!model) continue;
      var url = (row.querySelector('.pa-url') || {}).value || '';
      var key = (row.querySelector('.pa-key') || {}).value || '';
      var sub = { model: model };
      if (url) sub.base_url = url;
      if (key) sub.api_key = key;
      sub._provider = provider;  // stored for UI restore, not sent to server
      agentModels[agent] = sub;
    }
  }

  var cfg = { _preset: preset };
  if (model) cfg.model = model;
  if (baseUrl) cfg.base_url = baseUrl;
  if (apiKey) cfg.api_key = apiKey;
  
  // Per-agent model: switch + mapping
  if (perAgentEnabled) {
    cfg.per_agent_enabled = true;
    if (Object.keys(agentModels).length) cfg.agent_models = agentModels;
  }

  _saveLlmConfig(cfg);
  updateUploadVisibility();
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
  document.getElementById('llm-per-agent-enabled').checked = false;
  document.getElementById('llm-per-agent-table').innerHTML = '';
  document.getElementById('llm-per-agent-fields').style.display = 'none';
  document.getElementById('llm-test-result').textContent = '';
  onProviderChange();
  _saveLlmConfig(null);
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
      var note = data.using_server_key ? '（使用服务器 Key）' : '';
      resultEl.textContent = '✓ 连接成功！模型 ' + (data.model || '?') + ' → ' + (data.reply || '') + note;
      resultEl.className = 'llm-test-result ok';
      if (!data.model) {
        resultEl.textContent = '⚠ 连接成功但模型名为空，请填写模型名称';
        resultEl.className = 'llm-test-result err';
      } else if (data.tools_supported === false) {
        resultEl.textContent = '⚠ 连接成功，但工具调用测试未通过：' + (data.tools_detail || '该模型/端点不支持 function calling')
          + '。投研 Agent 依赖工具获取实时数据，用它会产出空泛报告（运行时系统将自动回退服务器默认模型取数）。';
        resultEl.className = 'llm-test-result err';
      }
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
    var trimmed = a.trim();
    // Detect quarter pattern: 2025Q4, 2025 Q4, etc.
    var qMatch = trimmed.match(/(\d{4})\s*Q([1-4])/);
    var hasLatest = trimmed.indexOf('最新') >= 0;
    if (!trimmed || (!qMatch && !hasLatest)) {
      return '需要同时提供公司名和季度，如：腾讯 2025Q4 / 腾讯2025Q4 / 最新';
    }
    // Check for future quarters
    if (qMatch) {
      var year = parseInt(qMatch[1]), quarter = parseInt(qMatch[2]);
      var now = new Date();
      var curYear = now.getFullYear(), curQuarter = Math.floor(now.getMonth() / 3) + 1;
      var availYear = curYear, availQuarter = curQuarter - 1;
      if (availQuarter === 0) { availQuarter = 4; availYear--; }
      if (year > availYear || (year === availYear && quarter > availQuarter)) {
        return '⚠ ' + year + 'Q' + quarter + ' 财报尚未发布！最新可用为 ' + availYear + 'Q' + availQuarter + '。请使用已发布的季度或输入"最新"';
      }
    }
    return null;
  },
  'earnings-team': function(a) {
    var trimmed = a.trim();
    var qMatch = trimmed.match(/(\d{4})\s*Q([1-4])/);
    var hasLatest = trimmed.indexOf('最新') >= 0;
    if (!trimmed || (!qMatch && !hasLatest)) {
      return '需要同时提供公司名和季度，如：腾讯 2025Q4 / 腾讯2025Q4 / 最新';
    }
    // Check for future quarters
    var qMatch = trimmed.match(/(\d{4})\s*Q([1-4])/);
    if (qMatch) {
      var year = parseInt(qMatch[1]), quarter = parseInt(qMatch[2]);
      var now = new Date();
      var curYear = now.getFullYear(), curQuarter = Math.floor(now.getMonth() / 3) + 1;
      var availYear = curYear, availQuarter = curQuarter - 1;
      if (availQuarter === 0) { availQuarter = 4; availYear--; }
      if (year > availYear || (year === availYear && quarter > availQuarter)) {
        return '⚠ ' + year + 'Q' + quarter + ' 财报尚未发布！最新可用为 ' + availYear + 'Q' + availQuarter + '。请使用已发布的季度或输入"最新"';
      }
    }
    return null;
  },
  'management-deep-dive': function(a) {
    return a.trim().length >= 2 ? null : '请输入公司名，如：腾讯 / 茅台 / 美团';
  },
  'investment-research': function(a) {
    return a.trim().length >= 2 ? null : '请输入公司名或股票代码，如：腾讯 / 0700.HK / 600519';
  },
  'investment-team': function(a) {
    return a.trim().length >= 2 ? null : '请输入公司名或股票代码，如：腾讯 / 0700.HK / 600519';
  },
  'investment-checklist': function(a) {
    return a.trim().length >= 2 ? null : '请输入公司名或股票代码（支持多个逗号分隔），如：腾讯,茅台,美团';
  },
  'financial-data': function(a) {
    return a.trim().length >= 2 ? null : '请输入公司名或股票代码，如：腾讯 / 600519';
  },
  'industry-research': function(a) {
    return a.trim().length >= 2 ? null : '请输入行业名称，如：半导体 / 新能源 / 消费';
  },
  'industry-funnel': function(a) {
    return a.trim().length >= 2 ? null : '请输入行业或方向，如：AI应用 / 光伏 / 创新药';
  },
  'quality-screen': function(a) {
    return a.trim().length >= 2 ? null : '请输入行业/指数/主题，如：沪深300 / 半导体';
  },
  'bottleneck-hunter': function(a) {
    return a.trim().length >= 2 ? null : '请输入超级趋势方向，如：AI算力 / 固态电池';
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
// ==================== File Upload ====================
var _uploadedFiles = [];  // extracted text from uploaded files
var _uploadedFileItems = [];  // uploaded file metadata for cumulative display

// Known multimodal models
var _MULTIMODAL_MODELS = ['gpt-4o', 'gpt-4-turbo', 'gpt-4-vision', 'gpt-5.6', 'gpt-5.5', 'gpt-5.4', 'claude-sonnet-5', 'claude-opus-5', 'claude-sonnet-4', 'claude-opus-4', 'claude-3', 'gemini', 'glm-4v', 'qwen-vl', 'qwenvl', 'doubao-vision', 'doubao-1.5-vision', 'llava', 'pixtral', 'yi-vision', 'grok-vision', 'gemma3'];

function updateUploadVisibility() {
  // Upload row is always visible — vision model only affects image OCR quality
  document.getElementById('file-upload-row').style.display = '';
}

async function handleFileUpload(fileList) {
  if (!fileList || !fileList.length) return;
  var status = document.getElementById('file-status');
  var preview = document.getElementById('file-preview');
  var formData = new FormData();
  var names = [];
  
  for (var i = 0; i < fileList.length; i++) {
    formData.append('files', fileList[i]);
    names.push(fileList[i].name);
  }
  
  status.textContent = '处理中...';
  preview.style.display = 'none';
  
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    // Auto-use main model as vision model when it's multimodal
    var llmCfg = _loadLlmConfig() || {};
    var visionModel = '';
    if (llmCfg.model) {
      var mainLower = llmCfg.model.toLowerCase();
      var isMulti = _MULTIMODAL_MODELS.some(function(k) { return mainLower.indexOf(k) >= 0; });
      if (isMulti) visionModel = llmCfg.model;
    }
    if (visionModel) {
      formData.append('vision_model', visionModel);
      if (llmCfg.base_url) formData.append('vision_base_url', llmCfg.base_url);
      if (llmCfg.api_key) formData.append('vision_api_key', llmCfg.api_key);
    }
    if (llmCfg.model) formData.append('llm_config', JSON.stringify(llmCfg));
    var res = await fetch('/api/upload', { method: 'POST', headers: headers, body: formData });
    if (!res.ok) {
      var err = await res.json().catch(function() { return {}; });
      throw new Error(err.detail || '上传失败 (' + res.status + ')');
    }
    var data = await res.json();
    // Accumulate files (append, don't overwrite) so users can add 5+ files across multiple picks
    data.files.forEach(function(f) { _uploadedFileItems.push(f); });
    renderUploadPreview();
    status.textContent = '✓ ' + _uploadedFileItems.length + ' 个文件已就绪';
    status.style.color = '#6b8e6b';

    // If no text in arg-field, suggest the file names
    var af = document.getElementById('arg-field');
    if (!af.value.trim() && _uploadedFileItems.length) {
      af.value = _uploadedFileItems.map(function(x) { return x.filename; }).join(', ');
    }
  } catch(e) {
    status.textContent = '✗ ' + e.message;
    status.style.color = '#b85c5c';
    preview.style.display = 'none';
  }
}

function renderUploadPreview() {
  var preview = document.getElementById('file-preview');
  _uploadedFiles = [];
  var html = '<div class="fp-header">已上传 ' + _uploadedFileItems.length + ' 个文件（将自动注入研究）</div>';
  _uploadedFileItems.forEach(function(f, idx) {
    html += '<div class="fp-item">';
    html += '<button class="fp-remove" onclick="removeUploadedFile(' + idx + ')" title="移除">✕</button>';
    html += '<span class="fp-filename">' + escHtml(f.filename) + '</span>';
    if (f.error) {
      html += ' <span class="fp-error">✗ ' + escHtml(f.error) + '</span>';
    } else {
      var text = f.text || '';
      if (f.summary) {
        html += ' <span style="color:#6b8e6b;font-size:0.75rem">🤖 AI摘要 (' + (f.method || '') + ')</span>';
        html += '<div class="fp-summary">' + escHtml(f.summary) + '</div>';
        var MAX_INJECT = 5000;
        if (text && text.length <= MAX_INJECT) {
          _uploadedFiles.push('【文件：' + f.filename + '】\n' + text);
          html += ' <span style="color:#8c8273;font-size:0.7rem">（注入原文 ' + text.length + '字）</span>';
        } else {
          _uploadedFiles.push('【文件：' + f.filename + ' - AI摘要】\n' + f.summary);
          html += ' <span style="color:#8c8273;font-size:0.7rem">（原文 ' + (text ? text.length : 0) + '字过长，注入摘要）</span>';
        }
      } else {
        _uploadedFiles.push('【文件：' + f.filename + '】\n' + text);
        var pv = text ? text.slice(0, 100) + (text.length > 100 ? '...' : '') : '';
        html += ' <span style="color:#8c8273;font-size:0.75rem">(' + (f.method || '') + ', ' + (text ? text.length : 0) + '字)</span>';
        html += '<div style="color:#8c8273;font-size:0.72rem;margin-top:2px">' + escHtml(pv) + '</div>';
      }
    }
    html += '</div>';
  });
  preview.innerHTML = html;
  preview.style.display = _uploadedFileItems.length ? 'block' : 'none';
}

function removeUploadedFile(idx) {
  _uploadedFileItems.splice(idx, 1);
  renderUploadPreview();
  var status = document.getElementById('file-status');
  if (_uploadedFileItems.length) {
    status.textContent = '✓ ' + _uploadedFileItems.length + ' 个文件已就绪';
    status.style.color = '#6b8e6b';
  } else {
    status.textContent = '';
    status.style.color = '';
  }
}

async function startResearch() {
  var args = document.getElementById('arg-field').value.trim();
  // Uploaded file content is sent as a separate attachments field
  var attachments = _uploadedFiles.length ? _uploadedFiles.join('\n\n') : '';
  if (attachments) {
    // Fallback target from filenames if user didn't specify one
    if (!args) {
      var fi = document.getElementById('file-input');
      var names = [];
      if (fi && fi.files && fi.files.length) {
        for (var i = 0; i < fi.files.length; i++) names.push(fi.files[i].name);
      }
      args = names.length ? '分析上传的文件：' + names.join('、') : '分析上传的文件';
    }
    // Clear file preview immediately — content already captured in attachments
    document.getElementById('file-preview').style.display = 'none';
    document.getElementById('file-preview').innerHTML = '';
    document.getElementById('file-status').textContent = '';
    document.getElementById('file-status').style.color = '';
    var fi2 = document.getElementById('file-input');
    if (fi2) fi2.value = '';
  }
  if (!currentSkill) { showToast('请先选择技能', 'warning'); return; }

  // Block if no model configured (per-agent-only config also counts)
  var cfg = _loadLlmConfig();
  var hasGlobal = !!(cfg && (cfg.model || cfg.base_url || cfg.api_key));
  var hasPerAgent = !!(cfg && cfg.agent_models && Object.keys(cfg.agent_models).length);
  if (!hasGlobal && !hasPerAgent) {
    showToast('⚠ 请先在模型设置中配置供应商、模型和 API Key', 'error');
    document.getElementById('llm-notice').style.display = 'block';
    return;
  }

  // Attachment-only runs skip the "arguments required" check
  var err = attachments ? null : validateArgs(currentSkill, args);
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
  _uploadedFiles = [];
  if (_researchAbort) { _researchAbort.abort(); _researchAbort = null; }
  if (ws) { ws.close(); ws = null; }

  // Hide other areas
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('report-content').innerHTML = '';
  document.getElementById('report-meta').style.display = 'none';
  document.getElementById('restore-banner').style.display = 'none';
  var hint = document.getElementById('report-dblclick-hint');
  if (hint) hint.style.display = 'none';
  document.getElementById('follow-up-area').style.display = 'none';
  document.getElementById('follow-up-qa').innerHTML = '';
  _followUpHistory = [];
  _clearFollowupFiles();
  try { localStorage.removeItem('ai_berkshire_followup'); } catch(e) {}
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
      '<span class="agent-status" style="color:var(--text-secondary);font-size:0.7rem">等待中</span>' +
      '<button class="agent-retry" onclick="retryFailedTask()" title="重试">⟳ 重试</button></div>';
  }).join('');

  // 断点续跑入口：历史报告"继续任务"带入了 task_id —— 只补跑失败 Agent
  if (_pendingResumeTaskId) {
    var _rid = _pendingResumeTaskId;
    _pendingResumeTaskId = null;
    await resumeAndWatch(_rid, args);
    return;
  }

  // Stream mode now works for ALL skills via WebSocket:
  // single-agent streams chunks; multi-agent/series stream per-agent progress.
  if (streamMode) {
    await streamResearch(args, attachments);
  } else {
    await batchResearch(args, attachments);
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

async function streamResearch(args, attachments) {
  _wsShouldReconnect = true;
  _wsReconnectAttempts = 0;
  _streamResearchImpl(args, attachments);
}

function _streamResearchImpl(args, attachments, watchTaskId) {
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var url = proto + '//' + location.host + '/ws/research/' + currentSkill.name;
  // Per-socket flag: set when we close the socket on purpose (watch-mode
  // timeout re-attach) so onclose does not schedule a duplicate reconnect.
  var _ignoreClose = false;

  // Timeout guard: if no data within 90s, fall back to batch mode
  var _wsTimeout = setTimeout(function() {
    if (!currentReport && _wsShouldReconnect) {
      if (watchTaskId) {
        // Watch mode must NOT fall back to batch (that would start a NEW
        // run). Re-check the task state and resume again instead.
        _ignoreClose = true;
        if (ws) { ws.close(); ws = null; }
        _reconnectResearch(args, attachments);
        return;
      }
      showToast('WebSocket响应超时，自动切换到普通模式', 'warning');
      _wsShouldReconnect = false;
      if (ws) { ws.close(); ws = null; }
      _batchFallback(args, attachments, true);
    }
  }, currentSkill && currentSkill.is_multi_agent ? 180000 : 90000);

  try {
    ws = new WebSocket(url);
    ws.onopen = function() {
      _wsReconnectAttempts = 0;
      var msg = { arguments: args };
      if (watchTaskId) msg.watch_task_id = watchTaskId;
      if (attachments) msg.attachments = attachments;
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
        _agentStreamBuffers = {};
      } else if (data.type === 'chunk') {
        if (data.agent) {
          // Event protocol v2: multi/series per-agent token stream.
          // Accumulate per agent; the assembled report still arrives in
          // 'complete', so the main report view is not disturbed.
          _agentStreamBuffers[data.agent] = (_agentStreamBuffers[data.agent] || '') + data.content;
          if (agentRowNames().indexOf(data.agent) >= 0) {
            updateAgentProgress(data.agent, 'running', '撰写中… ' + _agentStreamBuffers[data.agent].length + ' 字');
          }
        } else {
          currentReport += data.content;
          document.getElementById('progress-area').style.display = 'none';
          document.getElementById('result-area').style.display = 'block';
          scheduleRender();
        }
      } else if (data.type === 'progress') {
        updateAgentProgress(data.agent, data.status, data.message);
      } else if (data.type === 'context_ready') {
        // Event protocol v2: context assembly summary (info only)
        console.info('context_ready', data);
      } else if (data.type === 'tool_call') {
        // Event protocol v2: tool call observability (debug only)
        console.debug('tool_call', data);
      } else if (data.type === 'guard_warning') {
        // Event protocol v2: output gate warning (light notice)
        console.warn('guard_warning', data);
      } else if (data.type === 'cancelled') {
        // Event protocol v2: explicit cancel frame (legacy error cancelled still handled below)
        _wsShouldReconnect = false;
        finishResearch(false, data.message || '已被用户取消');
      } else if (data.type === 'complete') {
        _wsShouldReconnect = false;
        if (data.report) currentReport = data.report;
        finishResearch(true, null, { duration: data.duration_seconds, tokens: data.tokens });
      } else if (data.type === 'error') {
        _wsShouldReconnect = false;
        finishResearch(false, data.message);
      }
      // Unknown event types are ignored for forward compatibility.
    };

    ws.onerror = function() {
      // Will trigger onclose, handle reconnect there
    };
    ws.onclose = function(event) {
      if (_ignoreClose) return;
      if (_wsShouldReconnect && _wsReconnectAttempts < _WS_MAX_RECONNECT) {
        var delay = _WS_RECONNECT_DELAYS[_wsReconnectAttempts] || 10000;
        _wsReconnectAttempts++;
        showToast('连接断开，' + Math.round(delay/1000) + '秒后重试 (' + _wsReconnectAttempts + '/' + _WS_MAX_RECONNECT + ')', 'warning');
        setTimeout(function() {
          if (_wsShouldReconnect) _reconnectResearch(args, attachments);
        }, delay);
      } else if (_wsShouldReconnect) {
        // Max retries reached — fall back to batch mode
        if (currentReport) {
          finishResearch(true);
        } else {
          showToast('WebSocket连接失败，自动切换到普通模式', 'warning');
          _wsShouldReconnect = false;
          if (ws) { ws.close(); ws = null; }
          _batchFallback(args, attachments, false);
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

// 回退 batch 前先查任务状态（与 _reconnectResearch 同一思路）：
// 服务端任务仍在跑 → 转 watch / 提示，绝不重复 POST /api/research。
// allowWatch=true（WS 暂时静默）转 watch 继续收事件；false（WS 多次连不上）
// 只提示后台运行，避免在坏连线上做无谓的 watch 循环。
async function _batchFallback(args, attachments, allowWatch) {
  if (currentTaskId) {
    try {
      var headers = {};
      Object.assign(headers, getAuthHeaders());
      var res = await fetch('/api/task/' + currentTaskId, { headers: headers });
      var t = await res.json();
      if (t && t.status === 'running') {
        if (allowWatch) {
          showToast('任务仍在后台运行，转为进度接收模式', 'warning');
          _wsShouldReconnect = true;
          _streamResearchImpl(args, attachments, currentTaskId);
        } else {
          showToast('任务仍在后台运行，完成后可在「历史报告」中查看', 'warning');
          finishResearch(false, '连接中断，任务仍在后台运行，完成后请查看历史报告');
        }
        return;
      }
      if (t && t.status === 'completed' && t.report) {
        _wsShouldReconnect = false;
        currentReport = t.report;
        updateAllAgents('completed');
        finishResearch(true, null, { duration: t.duration_seconds });
        return;
      }
      // failed / cancelled / not found → 落到下面的 batch 重新开跑
    } catch (e) {
      // 状态查询失败（网络不通）→ 按原逻辑回退 batch
    }
  }
  batchResearch(args, attachments);
}

// Reconnect resume: before reopening the WS, ask the server about the
// in-flight task (task_id captured from the 'started' frame) and RESUME its
// display instead of blindly starting a duplicate run.
async function _reconnectResearch(args, attachments) {
  if (currentTaskId) {
    try {
      var headers = {};
      Object.assign(headers, getAuthHeaders());
      var res = await fetch('/api/task/' + currentTaskId, { headers: headers });
      var t = await res.json();
      if (t && t.status === 'running') {
        // Server-side run is still alive — re-attach in watch mode and keep
        // receiving its events (the terminal 'complete' frame carries the
        // full report, so missed chunks are harmless).
        showToast('已恢复连接，继续接收进度', 'success');
        _streamResearchImpl(args, attachments, currentTaskId);
        return;
      }
      if (t && t.status === 'completed' && t.report) {
        // Run finished while we were disconnected — render the stored report
        // exactly as if the 'complete' frame had arrived.
        _wsShouldReconnect = false;
        currentReport = t.report;
        updateAllAgents('completed');
        finishResearch(true, null, { duration: t.duration_seconds });
        return;
      }
      // failed / cancelled / interrupted / not found → fall through to a
      // fresh run (previous behavior: the reconnect re-sends the payload).
    } catch (e) {
      // Status check failed (server unreachable) — the fresh WS connect
      // below will fail too and the normal backoff loop keeps going.
    }
  }
  _streamResearchImpl(args, attachments);
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

async function batchResearch(args, attachments) {
  _researchAbort = new AbortController();
  // 30-minute budget: multi-agent / series runs legitimately take a long time.
  // On timeout we no longer report failure — the server keeps running and the
  // report will land in 历史报告.
  var timeout = setTimeout(function() { if(_researchAbort) _researchAbort.abort(); }, 1800000);
  try {
    var headers = { 'Content-Type': 'application/json' };
    Object.assign(headers, getAuthHeaders());
    var body = {
      skill_name: currentSkill.name,
      arguments: args,
      stream: false,
      llm_config: getLlmConfig() || undefined
    };
    if (attachments) body.attachments = attachments;
    var res = await fetch('/api/research', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(body),
      signal: _researchAbort.signal
    });
    clearTimeout(timeout);
    var data = await res.json();
    if (data.task_id) currentTaskId = data.task_id;
    if (res.status === 409) {
      // 服务端去重命中：同 skill+arguments 的任务正在运行，不重复开跑
      showToast(data.error || '相同参数的任务正在运行中', 'warning');
      finishResearch(false, data.error || '相同参数的任务正在运行中');
      return;
    }

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
  var statusText = row.querySelector('.agent-status');

  dot.className = 'agent-dot ' + status;
  if (!statusText) return;
  if (status === 'completed') {
    statusText.textContent = '完成';
    statusText.style.color = 'var(--accent-green)';
  } else if (status === 'running') {
    statusText.textContent = message || '分析中...';
    statusText.style.color = 'var(--accent)';
  } else if (status === 'failed') {
    statusText.textContent = message || '失败';
    statusText.style.color = 'var(--accent-red)';
    row.classList.add('failed');
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
    row.querySelector('.agent-status').textContent = status === 'completed' ? '完成' : '';
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
      // Save failed task for retry on next visit
      saveFailedTask(errorMsg);
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
  // Clear uploaded file preview after research completes
  _uploadedFiles = [];
  _uploadedFileItems = [];
  document.getElementById('file-preview').style.display = 'none';
  document.getElementById('file-preview').innerHTML = '';
  document.getElementById('file-status').textContent = '';
  document.getElementById('file-status').style.color = '';
  // Reset file input
  var fi = document.getElementById('file-input');
  if (fi) fi.value = '';
  localStorage.setItem('ai_berkshire_skill', currentSkill ? currentSkill.name : '');
}

// ==================== Rendering / TOC ====================
// ==================== Failed Task Persistence ====================
function saveFailedTask(errorMsg) {
  if (!currentSkill) return;
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_failed') || '[]');
    list.push({
      skill: currentSkill.name,
      skillLabel: currentSkill.display_name || currentSkill.name,
      arguments: document.getElementById('arg-field').value.trim(),
      error: errorMsg || '未知错误',
      ts: Date.now()
    });
    // Keep last 10 failed tasks
    localStorage.setItem('ai_berkshire_failed', JSON.stringify(list.slice(-10)));
  } catch(e) {}
}

function renderFailedTasks() {
  var el = document.getElementById('failed-tasks');
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_failed') || '[]');
  } catch(e) { el.style.display = 'none'; return; }
  if (!list.length) { el.style.display = 'none'; return; }
  
  var html = '<div style="font-size:0.85rem;color:#b85c5c;margin-bottom:8px">⚠ 上次有 ' + list.length + ' 个未完成的任务：</div>';
  list.forEach(function(t, i) {
    var when = new Date(t.ts).toLocaleString();
    html += '<div class="failed-task-card" id="ft-' + i + '">' +
      '<div class="ft-info">' +
      '<div class="ft-skill">' + escHtml(t.skillLabel) + '</div>' +
      '<div class="ft-args">' + escHtml(t.arguments || '(空)') + '</div>' +
      '<div class="ft-error">' + escHtml(t.error) + '</div>' +
      '<div class="ft-time">' + when + '</div>' +
      '</div>' +
      '<button class="btn-retry" onclick="retryFailedTask(' + i + ')">⟳ 继续</button>' +
      '<button class="btn-dismiss" onclick="dismissFailedTask(' + i + ')" title="忽略">✕</button>' +
      '</div>';
  });
  el.innerHTML = html;
  el.style.display = 'block';
}

function retryFailedTask(idx) {
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_failed') || '[]');
    if (idx >= list.length) return;
    var t = list[idx];
    // Remove this task from the list
    list.splice(idx, 1);
    localStorage.setItem('ai_berkshire_failed', JSON.stringify(list));
    // Re-render
    renderFailedTasks();
  updateUploadVisibility();
    // Select skill and start research
    selectSkill(t.skill);
    document.getElementById('arg-field').value = t.arguments || '';
    startResearch();
  } catch(e) { showToast('重试失败: ' + e.message, 'error'); }
}

function dismissFailedTask(idx) {
  try {
    var list = JSON.parse(localStorage.getItem('ai_berkshire_failed') || '[]');
    list.splice(idx, 1);
    localStorage.setItem('ai_berkshire_failed', JSON.stringify(list));
    renderFailedTasks();
  updateUploadVisibility();
  } catch(e) {}
}

function renderReport(md) {
  document.getElementById('report-content').innerHTML =
    DOMPurify.sanitize(marked.parse(md)) +
    '<div class="ai-disclaimer">该数据由AI在数据的基础上进行分析，仅供参考</div>';
  // Show double-click hint (new-tab view)
  var hint = document.getElementById('report-dblclick-hint');
  if (hint) hint.style.display = 'block';
  // Bind double-click → open report in new tab (once)
  _bindReportDblClick();
  // Show follow-up area when a report is rendered
  document.getElementById('follow-up-area').style.display = 'block';
  // Restore follow-up Q&A history
  _loadFollowUpHistory();
  // P0: attach financial / valuation chart panel for this report's target
  if (window.attachFinancialCharts) window.attachFinancialCharts(md);
}

// ==================== Follow-up Q&A ====================
var _followUpLoading = false;
var _followUpHistory = [];
var _followupFiles = [];  // extracted text from files attached to a follow-up question
var _followupFileItems = [];  // uploaded file metadata for cumulative display

function _htmlToText(html) {
  var d = document.createElement('div');
  d.innerHTML = html;
  return d.textContent || '';
}

function _clearFollowupFiles() {
  _followupFiles = [];
  _followupFileItems = [];
  var fi = document.getElementById('followup-file-input');
  if (fi) fi.value = '';
  var pv = document.getElementById('followup-file-preview');
  if (pv) { pv.style.display = 'none'; pv.innerHTML = ''; }
  var st = document.getElementById('followup-file-status');
  if (st) { st.textContent = ''; st.style.color = ''; }
}

async function handleFollowupFileUpload(fileList) {
  if (!fileList || !fileList.length) return;
  var status = document.getElementById('followup-file-status');
  var preview = document.getElementById('followup-file-preview');
  var formData = new FormData();
  for (var i = 0; i < fileList.length; i++) {
    formData.append('files', fileList[i]);
  }
  status.textContent = '处理中...';
  status.style.color = '';
  preview.style.display = 'none';

  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    // Auto-use main model as vision model when it's multimodal
    var llmCfg = _loadLlmConfig() || {};
    var visionModel = '';
    if (llmCfg.model) {
      var mainLower = llmCfg.model.toLowerCase();
      var isMulti = _MULTIMODAL_MODELS.some(function(k) { return mainLower.indexOf(k) >= 0; });
      if (isMulti) visionModel = llmCfg.model;
    }
    if (visionModel) {
      formData.append('vision_model', visionModel);
      if (llmCfg.base_url) formData.append('vision_base_url', llmCfg.base_url);
      if (llmCfg.api_key) formData.append('vision_api_key', llmCfg.api_key);
    }
    if (llmCfg.model) formData.append('llm_config', JSON.stringify(llmCfg));
    var res = await fetch('/api/upload', { method: 'POST', headers: headers, body: formData });
    if (!res.ok) {
      var err = await res.json().catch(function() { return {}; });
      throw new Error(err.detail || '上传失败 (' + res.status + ')');
    }
    var data = await res.json();
    data.files.forEach(function(f) { _followupFileItems.push(f); });
    renderFollowupPreview();
    status.textContent = '✓ ' + _followupFileItems.length + ' 个文件已就绪';
    status.style.color = '#6b8e6b';
  } catch(e) {
    status.textContent = '✗ ' + e.message;
    status.style.color = '#b85c5c';
    preview.style.display = 'none';
  }
}

function renderFollowupPreview() {
  var preview = document.getElementById('followup-file-preview');
  _followupFiles = [];
  var html = '<div class="fp-header">已上传 ' + _followupFileItems.length + ' 个文件（将随下一次追问发送）</div>';
  _followupFileItems.forEach(function(f, idx) {
    html += '<div class="fp-item">';
    html += '<button class="fp-remove" onclick="removeFollowupFile(' + idx + ')" title="移除">✕</button>';
    html += '<span class="fp-filename">' + escHtml(f.filename) + '</span>';
    if (f.error) {
      html += ' <span class="fp-error">✗ ' + escHtml(f.error) + '</span>';
    } else if (f.summary && (!f.text || f.text.length > 5000)) {
      _followupFiles.push('【文件：' + f.filename + ' - AI摘要】\n' + f.summary);
      html += ' <span style="color:#8c8273;font-size:0.7rem">（注入AI摘要）</span>';
    } else {
      _followupFiles.push('【文件：' + f.filename + '】\n' + (f.text || f.summary || ''));
      html += ' <span style="color:#8c8273;font-size:0.7rem">(' + (f.text ? f.text.length : 0) + '字)</span>';
    }
    html += '</div>';
  });
  preview.innerHTML = html;
  preview.style.display = _followupFileItems.length ? 'block' : 'none';
}

function removeFollowupFile(idx) {
  _followupFileItems.splice(idx, 1);
  renderFollowupPreview();
  var status = document.getElementById('followup-file-status');
  if (_followupFileItems.length) {
    status.textContent = '✓ ' + _followupFileItems.length + ' 个文件已就绪';
    status.style.color = '#6b8e6b';
  } else {
    status.textContent = '';
    status.style.color = '';
  }
}

function _saveFollowUpHistory() {
  var items = [];
  document.querySelectorAll('#follow-up-qa .follow-up-qa-item').forEach(function(el) {
    var isQ = el.classList.contains('follow-up-q');
    items.push({
      type: isQ ? 'q' : 'a',
      html: el.querySelector('.qa-content').innerHTML,
      label: el.querySelector('.qa-label').textContent
    });
  });
  _followUpHistory = items;
  try { localStorage.setItem('ai_berkshire_followup', JSON.stringify(items)); } catch(e) {}
}

function _loadFollowUpHistory() {
  try {
    var items = JSON.parse(localStorage.getItem('ai_berkshire_followup') || '[]');
    if (!items.length) return;
    _followUpHistory = items;
    var qa = document.getElementById('follow-up-qa');
    var html = '';
    items.forEach(function(item) {
      var cls = item.type === 'q' ? 'follow-up-q' : 'follow-up-a';
      var mdCls = item.type === 'a' ? ' markdown-body' : '';
      html += '<div class="follow-up-qa-item ' + cls + '">' +
        '<div class="qa-label">' + escHtml(item.label) + '</div>' +
        '<div class="qa-content' + mdCls + '">' + item.html + '</div>' +
        '</div>';
    });
    qa.innerHTML = html;
    document.getElementById('follow-up-area').style.display = 'block';
  // Restore follow-up Q&A history
  _loadFollowUpHistory();
  } catch(e) {}
}

async function submitFollowUp() {
  if (_followUpLoading) return;
  var field = document.getElementById('follow-up-field');
  var question = field.value.trim();
  if (!question) return;
  if (!currentReport) { showToast('请先生成报告', 'warning'); return; }

  _followUpLoading = true;
  var btn = document.getElementById('btn-follow-up');
  btn.disabled = true;
  btn.textContent = '思考中...';
  field.disabled = true;

  // Show question immediately
  var qa = document.getElementById('follow-up-qa');
  var qId = 'fu-q-' + Date.now();
  qa.insertAdjacentHTML('beforeend',
    '<div class="follow-up-qa-item follow-up-q" id="' + qId + '">' +
    '<div class="qa-label">🙋 追问</div>' +
    '<div class="qa-content">' + escHtml(question) + '</div>' +
    '</div>');
  var aId = 'fu-a-' + Date.now();
  qa.insertAdjacentHTML('beforeend',
    '<div class="follow-up-qa-item follow-up-a" id="' + aId + '">' +
    '<div class="qa-label">🤖 AI 回复</div>' +
    '<div class="qa-content follow-up-loading">思考中...</div>' +
    '</div>');
  qa.scrollTop = qa.scrollHeight;

  try {
    // Multi-turn history: previous Q&A turns of this report session
    var history = [];
    _followUpHistory.forEach(function(item) {
      if (item.type === 'a' && item.html.indexOf('accent-red') >= 0) return;  // skip failed answers
      var text = _htmlToText(item.html).trim();
      if (text) history.push({ role: item.type === 'q' ? 'user' : 'assistant', content: text.slice(0, 2000) });
    });
    history = history.slice(-10);
    var attachments = _followupFiles.length ? _followupFiles.join('\n\n') : '';

    var headers = { 'Content-Type': 'application/json' };
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/follow-up', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({
        report: currentReport,
        question: question,
        skill_name: currentSkill ? currentSkill.name : '',
        llm_config: getLlmConfig() || undefined,
        history: history.length ? history : undefined,
        attachments: attachments || undefined
      })
    });
    if (!res.ok) {
      var err = await res.json().catch(function() { return {}; });
      throw new Error(err.detail || '请求失败 (' + res.status + ')');
    }
    var data = await res.json();
    var answerEl = document.getElementById(aId).querySelector('.qa-content');
    answerEl.innerHTML = DOMPurify.sanitize(marked.parse(data.answer || '(无回复)'));
    answerEl.classList.add('markdown-body');
    // Persist Q&A to localStorage
    _saveFollowUpHistory();
    // Attachments are single-shot — clear after a successful send
    if (attachments) _clearFollowupFiles();
  } catch(e) {
    document.getElementById(aId).querySelector('.qa-content').innerHTML =
      '<span style="color:var(--accent-red)">✗ ' + escHtml(e.message) + '</span>';
  } finally {
    _followUpLoading = false;
    btn.disabled = false;
    btn.textContent = '发送';
    field.disabled = false;
    field.value = '';
    field.focus();
    qa.scrollTop = qa.scrollHeight;
  }
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

// ⤢ 新窗口 — 把当前报告渲染成独立 HTML 文件，在新标签页打开（内容/字体不变）
async function openReportNewTab() {
  if (!currentReport) { showToast('没有可打开的报告', 'warning'); return; }
  try {
    var res = await fetch('/api/report/view', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
      body: JSON.stringify({ markdown: currentReport, filename: _reportFileBase() })
    });
    if (!res.ok) {
      var err = await res.json().catch(function() { return {}; });
      throw new Error(err.detail || '生成失败 (' + res.status + ')');
    }
    var data = await res.json();
    if (data.url) {
      // 同标签页跳转（不是新开标签页）——与“← 返回”按钮形成连续导航闭环
      window.location.href = data.url;
    } else {
      showToast('未返回页面地址', 'error');
    }
  } catch (e) {
    showToast('打开失败: ' + e.message, 'error');
  }
}

// 双击报告内容区 → 新标签页；点击右侧提示条 → 新标签页
function _bindReportDblClick() {
  var rc = document.getElementById('report-content');
  if (rc && !rc._dblclickBound) {
    rc.addEventListener('dblclick', function(e) {
      // 双击时如果正在选中文本/点击了链接/按钮，不拦截
      var t = e.target;
      if (t && (t.tagName === 'A' || t.tagName === 'BUTTON' || t.tagName === 'INPUT')) return;
      openReportNewTab();
    });
    rc._dblclickBound = true;
  }
  var hint = document.getElementById('report-dblclick-hint');
  if (hint && !hint._hintBound) {
    hint.addEventListener('click', function() { openReportNewTab(); });
    hint._hintBound = true;
  }
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

function pushWechat() {
  if (!currentReport) { showToast('没有可推送的内容', 'warning'); return; }
  var title = _reportFileBase().replace(/[-_]/g, ' ') || 'AI Berkshire 报告';
  var btn = event ? event.target : null;
  if (btn) { btn.disabled = true; btn.textContent = '推送中...'; }
  var headers = { 'Content-Type': 'application/json' };
  var jwt = localStorage.getItem('ai_berkshire_jwt');
  if (jwt) { headers['Authorization'] = 'Bearer ' + jwt; }
  fetch('/api/export/wechat', {
    method: 'POST',
    headers: headers,
    body: JSON.stringify({ markdown: currentReport, title: title })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (btn) { btn.disabled = false; btn.textContent = '📲 微信'; }
      if (res.ok) { showToast('已推送到微信', 'success'); }
      else { showToast('推送失败: ' + (res.d.detail || '未知错误'), 'warning'); }
    })
    .catch(function () {
      if (btn) { btn.disabled = false; btn.textContent = '📲 微信'; }
      showToast('推送失败', 'warning');
    });
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

// P4: export PDF via weasyprint backend
async function exportPdf() {
  if (!currentReport) { showToast('没有可导出的内容', 'warning'); return; }
  var btn = event ? event.target : null;
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
  try {
    var llmCfg = getLlmConfig();
    var headers = { 'Content-Type': 'application/json' };
    var jwt = localStorage.getItem('ai_berkshire_jwt');
    if (jwt) { headers['Authorization'] = 'Bearer ' + jwt; }
    var resp = await fetch('/api/export/pdf', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({ markdown: currentReport })
    });
    if (!resp.ok) {
      var err = await resp.json().catch(function() { return { detail: '导出失败' }; });
      throw new Error(err.detail || '导出失败');
    }
    var blob = await resp.blob();
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    var title = 'report';
    try {
      var meta = JSON.parse(sessionStorage.getItem('ai_berkshire_report_meta') || '{}');
      title = (meta.skill || 'report') + '_' + new Date().toISOString().slice(0, 10);
    } catch(e) {}
    a.download = title.replace(/[^a-zA-Z0-9_\u4e00-\u9fff-]/g, '_') + '.pdf';
    a.click();
    URL.revokeObjectURL(url);
    showToast('PDF 导出成功', 'success');
  } catch(e) {
    showToast('PDF 导出失败: ' + e.message, 'error');
    console.error('exportPdf:', e);
  }
  if (btn) { btn.disabled = false; btn.textContent = '导出PDF'; }
}

function printReport() {
  if (!currentReport) { showToast('没有可打印的内容', 'warning'); return; }
  window.print();
}

function clearResult() {
  currentReport = '';
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('report-content').innerHTML = '';
  // P0: remove the financial charts panel if present
  var fc = document.getElementById('financial-charts');
  if (fc && fc.parentNode) fc.parentNode.removeChild(fc);
  document.getElementById('report-meta').style.display = 'none';
  document.getElementById('restore-banner').style.display = 'none';
  var hint = document.getElementById('report-dblclick-hint');
  if (hint) hint.style.display = 'none';
  document.getElementById('follow-up-area').style.display = 'none';
  document.getElementById('follow-up-qa').innerHTML = '';
  _followUpHistory = [];
  _clearFollowupFiles();
  try { localStorage.removeItem('ai_berkshire_followup'); } catch(e) {}
  hideToc();
  document.getElementById('empty-state').style.display = 'flex';
  try {
    sessionStorage.removeItem('ai_berkshire_report');
    sessionStorage.removeItem('ai_berkshire_report_meta');
  } catch(e) {}
}

// ==================== History ====================
function backToHome() {
  document.getElementById('result-area').style.display = 'none';
  var hint = document.getElementById('report-dblclick-hint');
  if (hint) hint.style.display = 'none';
  document.getElementById('progress-area').style.display = 'none';
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';
  document.getElementById('empty-state').style.display = 'block';
}

async function showHistory() {
  document.getElementById('empty-state').style.display = 'none';
  if (!currentTaskId) {
    document.getElementById('result-area').style.display = 'none';
    document.getElementById('progress-area').style.display = 'none';
  }
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';

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
    var summary = (r.summary || '').trim();
    return '<div class="history-item' + (r.partial ? ' partial' : '') + '" onclick="loadReport(\'' + safeName.replace(/'/g, "\\'") + '\')">' +
      '<input type="checkbox" class="compare-check" data-name="' + safeName + '"' + checked +
      ' onclick="event.stopPropagation();toggleCompareSelect(this)" title="选择两份报告进行对比">' +
      '<div class="history-item-body">' +
      '<div class="report-name">' + (r.partial ? '<span class="partial-badge">未完成</span>' : '') + escHtml(title) + '</div>' +
      (summary ? '<div class="report-summary" title="' + escHtml(summary) + '">' + escHtml(summary) + '</div>' : '') +
      '<div class="report-meta">' + escHtml(metaParts.join(' | ')) + '</div>' +
      (args ? '<div class="report-filename">' + safeName + '</div>' : '') +
      '</div>' +
      (r.partial ? '<button class="btn-continue-task" onclick="event.stopPropagation();continueTaskByName(\'' + safeName.replace(/'/g, "\\'") + '\')" title="' + (r.resumable ? '断点续跑：复用已完成成果，仅补跑失败的 Agent' : '以相同技能和目标重新运行') + '">⟳ 继续任务</button>' : '') +
      '<button class="btn-delete-report" onclick="event.stopPropagation();deleteReport(\'' + safeName.replace(/'/g, "\\'") + '\')" title="删除报告">✕</button>' +
      '</div>';
  }).join('');
}

// 历史报告中的失败/中断任务：可断点续跑（resumable）时只补跑失败 Agent，
// 否则以相同技能+目标整局重跑
var _pendingResumeTaskId = null;

function continueTaskByName(name) {
  var r = (_historyData || []).find(function(x) { return x.name === name; });
  if (!r) { showToast('找不到该报告的记录', 'error'); return; }
  if (!r.skill_name) { showToast('无法确定该报告的技能，请手动选择技能重跑', 'error'); return; }
  _pendingResumeTaskId = (r.resumable && r.task_id) ? r.task_id : null;
  selectSkill(r.skill_name);
  document.getElementById('arg-field').value = r.arguments || '';
  startResearch();
}

// 断点续跑：启动 resume 后台任务，随后用 WS watch 跟随进度
async function resumeAndWatch(taskId, args) {
  try {
    var headers = { 'Content-Type': 'application/json' };
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/resume', {
      method: 'POST', headers: headers,
      body: JSON.stringify({ llm_config: getLlmConfig() || undefined })
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok || data.error) {
      throw new Error(data.detail || data.error || ('HTTP ' + res.status));
    }
    currentTaskId = data.task_id;
    (data.skipped_agents || []).forEach(function(name) {
      updateAgentProgress(name, 'completed', '复用上次成果');
    });
    showToast('断点续跑：跳过 ' + (data.skipped_agents || []).length +
      ' 个已成功 Agent，补跑 ' + (data.rerun_agents || []).length + ' 个', 'info');
    _wsShouldReconnect = true;
    _wsReconnectAttempts = 0;
    _streamResearchImpl(args, '', data.task_id);
  } catch (e) {
    showToast('续跑启动失败：' + e.message + '，改为整局重跑', 'warning');
    streamResearch(args, '');
  }
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

// ==================== Cost Stats ====================
var _costDays = 30;
var _costChartDaily = null;
var _costChartSkill = null;
var _costModel = '';          // '' = 全部模型；否则为 by_model 返回的模型名
var _costModelOptions = null; // 未过滤响应里的模型清单，供下拉框保持完整

async function showCosts() {
  document.getElementById('empty-state').style.display = 'none';
  if (!currentTaskId) {
    document.getElementById('result-area').style.display = 'none';
    document.getElementById('progress-area').style.display = 'none';
  }
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'block';
  await loadCostStats();
}

function closeCosts() {
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('empty-state').style.display = 'block';
}

// ── P3: decision log panel ──────────────────────────────
async function showDecisions() {
  document.getElementById('empty-state').style.display = 'none';
  if (!currentTaskId) {
    document.getElementById('result-area').style.display = 'none';
    document.getElementById('progress-area').style.display = 'none';
  }
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('compare-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'block';

  var list = document.getElementById('decisions-list');
  list.innerHTML = '<p style="color:var(--text-secondary)">加载中...</p>';
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var res = await fetch('/api/decisions', { headers: headers });
    var data = await res.json();
    renderDecisions(data);
  } catch (e) {
    list.innerHTML = '<p style="color:var(--accent-red)">加载失败: ' + escHtml(e.message) + '</p>';
  }
}

function closeDecisions() {
  document.getElementById('decisions-area').style.display = 'none';
  document.getElementById('empty-state').style.display = 'block';
}

function renderDecisions(data) {
  var list = document.getElementById('decisions-list');
  var summary = document.getElementById('decisions-summary');
  var decisions = data.decisions || [];

  summary.textContent = decisions.length
    ? ('共 ' + data.total + ' 条 | ' + data.pending + ' 条待验证')
    : '';

  if (!decisions.length) {
    list.innerHTML = '<p style="color:var(--text-secondary)">暂无决策记录。完成一次包含明确结论（买入/持有/卖出）的研究后，会自动记录在这里。</p>';
    return;
  }

  list.innerHTML = decisions.map(function(d) {
    var status = d.status === 'resolved' ? 'resolved' : 'pending';
    var statusLabel = status === 'resolved' ? '已验证' : '待验证';
    var rows = '';
    if (d.confidence) rows += '<div class="decision-row"><b>置信度</b>：' + escHtml(d.confidence) + '</div>';
    if (d.assumptions && d.assumptions !== '未指定') rows += '<div class="decision-row"><b>关键假设</b>：' + escHtml(d.assumptions) + '</div>';
    if (d.target && d.target !== '未指定') rows += '<div class="decision-row"><b>目标价/信号</b>：' + escHtml(d.target) + '</div>';
    var meta = [];
    if (d.skill) meta.push(d.skill);
    if (d.time) meta.push(d.time);
    return '<div class="decision-item">' +
      '<div class="decision-head">' +
        '<span class="decision-stock">' + escHtml(d.stock || '未知标的') + '</span>' +
        '<span class="decision-badge ' + status + '">' + statusLabel + '</span>' +
        '<span class="decision-time">' + escHtml(meta.join(' | ')) + '</span>' +
      '</div>' +
      '<div class="decision-conclusion">' + escHtml(d.conclusion || '') + '</div>' +
      rows +
    '</div>';
  }).join('');
}

function setCostDays(days) {
  _costDays = days;
  document.querySelectorAll('#costs-days button').forEach(function(b) {
    b.classList.toggle('active', parseInt(b.dataset.days, 10) === days);
  });
  loadCostStats();
}

function setCostModel(model) {
  _costModel = model || '';
  loadCostStats();
}

function _renderCostModelOptions() {
  var sel = document.getElementById('cost-model-sel');
  if (!sel) return;
  var opts = ['<option value="">全部模型</option>'];
  (_costModelOptions || []).forEach(function(m) {
    var label = m === 'unknown' ? '未知（旧数据）' : m;
    var selected = (m === _costModel) ? ' selected' : '';
    opts.push('<option value="' + escHtml(m) + '"' + selected + '>' + escHtml(label) + '</option>');
  });
  sel.innerHTML = opts.join('');
  sel.value = _costModel;
}

async function loadCostStats() {
  try {
    var headers = {};
    Object.assign(headers, getAuthHeaders());
    var url = '/api/stats/costs?days=' + _costDays;
    if (_costModel) url += '&model=' + encodeURIComponent(_costModel);
    var res = await fetch(url, { headers: headers });
    var data = await res.json();
    // 只有未过滤的响应才刷新下拉框选项，避免选中某模型后选项塌缩
    if (!_costModel) {
      _costModelOptions = (data.by_model || []).map(function(r) { return r.model; });
    }
    _renderCostModelOptions();
    renderCostStats(data);
  } catch (e) {
    showToast('成本数据加载失败: ' + e.message, 'error');
  }
}

function renderCostStats(data) {
  var t = data.totals || {};
  var totalTokens = (t.tokens_prompt || 0) + (t.tokens_completion || 0);
  document.getElementById('cost-total-runs').textContent = t.runs || 0;
  document.getElementById('cost-total-tokens').textContent = totalTokens.toLocaleString();
  document.getElementById('cost-total-yuan').textContent = '¥' + (t.cost_yuan || 0).toFixed(4);
  document.getElementById('cost-avg-duration').textContent = fmtDuration(t.avg_duration_s || 0);

  // 按模型统计表：费用 / 次数 / Token / 平均耗时
  var byModel = data.by_model || [];
  var tbl = document.getElementById('cost-model-table');
  if (!byModel.length) {
    tbl.innerHTML = '<p style="color:var(--text-secondary);font-size:0.85rem">当前筛选条件下暂无模型数据</p>';
  } else {
    var rows = byModel.map(function(r) {
      var label = r.model === 'unknown' ? '未知（旧数据）' : r.model;
      return '<tr>' +
        '<td>' + escHtml(label) + '</td>' +
        '<td>' + (r.runs || 0) + '</td>' +
        '<td>¥' + (r.cost_yuan || 0).toFixed(4) + '</td>' +
        '<td>' + (r.tokens || 0).toLocaleString() + '</td>' +
        '<td>' + fmtDuration(r.avg_duration_s || 0) + '</td>' +
      '</tr>';
    }).join('');
    tbl.innerHTML = '<table class="cost-model-table"><thead><tr>' +
      '<th>模型</th><th>运行次数</th><th>总费用</th><th>总Token</th><th>平均耗时</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  var byDay = data.by_day || [];
  if (_costChartDaily) { _costChartDaily.dispose(); }
  _costChartDaily = echarts.init(document.getElementById('cost-chart-daily'));
  _costChartDaily.setOption({
    grid: { left: 60, right: 20, top: 30, bottom: 30 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: byDay.map(function(d) { return d.date.slice(5); }) },
    yAxis: { type: 'value', name: '¥' },
    series: [{
      name: '每日费用', type: 'bar', barMaxWidth: 24,
      itemStyle: { color: '#6b8e6b' },
      data: byDay.map(function(d) { return d.cost_yuan; })
    }]
  });

  var bySkill = data.by_skill || [];
  if (_costChartSkill) { _costChartSkill.dispose(); }
  _costChartSkill = echarts.init(document.getElementById('cost-chart-skill'));
  _costChartSkill.setOption({
    grid: { left: 10, right: 40, top: 10, bottom: 30, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', name: '¥' },
    yAxis: {
      type: 'category', inverse: true,
      data: bySkill.map(function(s) { return skillDisplayName(s.skill_name) || s.skill_name; })
    },
    series: [{
      name: '费用', type: 'bar', barMaxWidth: 20,
      itemStyle: { color: '#c4a77d' },
      label: { show: true, position: 'right', formatter: function(p) { return '¥' + p.value.toFixed(4); } },
      data: bySkill.map(function(s) { return s.cost_yuan; })
    }]
  });
}

async function loadReport(filename) {
  // Block if no model configured
  var _cfg = _loadLlmConfig();
  var _ok = !!(_cfg && (_cfg.model || _cfg.api_key || _cfg.base_url || (_cfg.agent_models && Object.keys(_cfg.agent_models).length)));
  if (!_ok) { showToast('⚠ 请先在模型设置中配置模型', 'error'); return; }
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

// ==================== URL Pre-fill (deep links) ====================
// Supported query params, e.g. /app?skill=investment-team&arguments=600519&autorun=1
//   skill      → select that skill via the normal selectSkill() path
//   arguments  → pre-fill the argument textarea (legacy alias: company)
//   autorun=1  → also start research (requires valid skill + non-empty arguments)
// Unknown skill names are ignored silently; arguments are capped at 500 chars.
// Consumed params are stripped from the URL via history.replaceState so a
// refresh never re-triggers the deep link (important for autorun).
var DEEPLINK_MAX_ARGS = 500;

// 解析深链参数：优先 query，其次 hash。
// dashboard 用 hash 传参（window.open 复用同名窗口时仅 hash 变化不会整页重载，只触发 hashchange），
// 首次新开标签页时参数也落在 hash，故 query 与 hash 两者都认。
function getDeepLinkParams() {
  var q = new URLSearchParams(window.location.search);
  var skill = q.get('skill') || '';
  var args = q.get('arguments') || q.get('company') || '';
  var autorun = q.get('autorun') === '1';
  if (!skill && !args && window.location.hash) {
    var h = window.location.hash;
    if (h.charAt(0) === '#') h = h.slice(1);
    if (h) {
      var hq = new URLSearchParams(h);
      skill = hq.get('skill') || '';
      args = hq.get('arguments') || hq.get('company') || '';
      autorun = hq.get('autorun') === '1';
    }
  }
  return { skill: skill, args: args, autorun: autorun };
}

// 应用深链：选中技能 + 填入参数 + 可选自动开始分析。首次加载与 hashchange 复用同一逻辑。
function applyDeepLink(skill, args, autorun) {
  var skillOk = false;
  if (skill) {
    // Unknown skill → ignore the whole deep link silently, stay on default view
    if (!allSkills.some(function(s) { return s.name === skill; })) return false;
    selectSkill(skill);
    skillOk = true;
  }
  if (args) {
    var field = document.getElementById('arg-field');
    field.value = args;
    // Fire the normal input listeners (char count, hint, textarea auto-grow)
    field.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (autorun && skillOk && args.trim()) {
    // Same flow as a manual click — startResearch() does its own validation
    setTimeout(function() { startResearch(); }, 600);
  }
  return true;
}

function prefillFromURL() {
  var p = getDeepLinkParams();
  if (!p.skill && !p.args) return false;
  if (p.args.length > DEEPLINK_MAX_ARGS) p.args = p.args.slice(0, DEEPLINK_MAX_ARGS);
  var ok = applyDeepLink(p.skill, p.args, p.autorun);
  if (window.history && window.history.replaceState) {
    window.history.replaceState({}, '', window.location.pathname);
  }
  return ok;
}

// 复用已开的 Berkshire 标签页时，window.open 只改 #hash（不整页重载），
// 监听 hashchange 直接填入代码并分析——实现「已开则复用、直接输入内容、不重新打开」。
window.addEventListener('hashchange', function() {
  var p = getDeepLinkParams();
  if (p.args) {
    if (p.args.length > DEEPLINK_MAX_ARGS) p.args = p.args.slice(0, DEEPLINK_MAX_ARGS);
    applyDeepLink(p.skill, p.args, p.autorun);
    // 清 hash，确保下次（即使参数相同）也能再次触发 hashchange
    if (window.history && window.history.replaceState) {
      window.history.replaceState({}, '', window.location.pathname);
    }
  }
});

// ==================== Save on pagehide (beforeunload triggers Permissions Policy violation) ====================
window.addEventListener('pagehide', function() {
  if (currentReport) {
    try {
      sessionStorage.setItem('ai_berkshire_report', currentReport);
    } catch(e) {}
  }
  if (ws) { ws.close(); }
});
