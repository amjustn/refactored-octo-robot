// llm-config.js — 每用户模型·每 Agent 配置 / auth / save·reset·test（拆分自 app.js）

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
