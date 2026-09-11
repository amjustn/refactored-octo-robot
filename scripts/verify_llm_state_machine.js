// verify_llm_state_machine.js — 模型状态机「四组合」行为验证（批A核心）
//
// 组合矩阵：服务端是否持有默认 Key × 浏览器是否存有自定义配置
// 断言：左下角状态行文案、提示条显隐与类型、是否允许无自有配置直接开跑。
//
// 用法：node scripts/verify_llm_state_machine.js    （退出码 0=全过, 1=有失败）
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');

function makeEl() {
  return {
    textContent: '', value: '', innerHTML: '', className: '',
    style: {}, dataset: {}, options: [], files: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, closest() { return null; }
  };
}

function makeStore() {
  const m = {};
  return {
    getItem: (k) => (k in m ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    removeItem: (k) => { delete m[k]; },
    _dump: () => Object.assign({}, m)
  };
}

function loadApp() {
  const ids = ['powered-by', 'llm-notice', 'llm-model', 'llm-model-select', 'llm-base-url',
    'llm-api-key', 'llm-preset', 'llm-test-result', 'llm-per-agent-block', 'llm-per-agent-table',
    'llm-per-agent-fields', 'llm-per-agent-enabled', 'llm-notice-text', 'llm-remember'];
  const els = {};
  ids.forEach(function (id) { els[id] = makeEl(); });
  const localStorage = makeStore();
  const sessionStorage = makeStore();

  const sandbox = {
    console: console,
    setTimeout: function (fn) { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout: function () {},
    fetch: function () { return Promise.reject(new Error('verify: no network')); },
    navigator: { userAgent: 'verify' },
    location: { reload: function () {}, pathname: '/app', search: '', hash: '' },
    document: {
      getElementById: function (id) { if (!els[id]) els[id] = makeEl(); return els[id]; },
      querySelectorAll: function () { return []; },
      querySelector: function () { return null; },
      createElement: function () { return makeEl(); },
      addEventListener: function () {},
      body: { classList: { add() {}, remove() {} }, innerHTML: '' }
    },
    localStorage: localStorage,
    sessionStorage: sessionStorage
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  ['static/js/core.js', 'static/js/llm-config.js'].forEach(function (f) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, f), 'utf8'), sandbox, { filename: f });
  });
  return { sb: sandbox, els: els, localStorage: localStorage, sessionStorage: sessionStorage };
}

const SERVER_MODEL = 'deepseek-v4-flash';
const GW_MODELS = ['deepseek-v4-pro', 'deepseek-v4-flash', 'kimi-k2.5', 'glm-4.7'];

const results = [];
function check(name, cond, detail) {
  results.push({ name: name, ok: !!cond, detail: detail });
}

function runCombo(label, serverHasKey, browserCfg, expect) {
  const app = loadApp();
  if (browserCfg) app.sessionStorage.setItem('ai_berkshire_llm', JSON.stringify(browserCfg));
  app.sb._initLlmDefaults({
    provider: 'deepseek', model: SERVER_MODEL, base_url: 'https://api.deepseek.com',
    has_key: serverHasKey, supported_models: GW_MODELS
  });

  const powered = app.els['powered-by'].textContent;
  const noticeDisplay = app.els['llm-notice'].style.display;
  const noticeCls = app.els['llm-notice'].className;
  // 「能否开跑」= 自有配置可用 或 服务端默认模型可用（research.js 的放行条件同一语义）
  const canRun = (typeof app.sb._hasUsableModel === 'function') ? app.sb._hasUsableModel() : null;

  check(label + ' / 状态行文案', powered.indexOf(expect.poweredContains) >= 0,
    '实际="' + powered + '" 期望含="' + expect.poweredContains + '"');
  check(label + ' / 提示条显隐', noticeDisplay === expect.noticeDisplay,
    '实际 display="' + noticeDisplay + '" 期望="' + expect.noticeDisplay + '"');
  if (expect.noticeDisplay === 'block') {
    check(label + ' / 提示条类型', noticeCls.indexOf(expect.noticeClass) >= 0,
      '实际 class="' + noticeCls + '" 期望含="' + expect.noticeClass + '"');
  }
  check(label + ' / 无自有配置能否开跑', canRun === expect.canRun,
    '实际=' + canRun + ' 期望=' + expect.canRun);
  return { powered: powered, noticeDisplay: noticeDisplay, noticeCls: noticeCls, canRun: canRun };
}

// 1) 服务端有 Key + 浏览器无配置 → 状态行标明「服务端默认」，提示条为信息态，允许直接开跑
runCombo('组合1 服务端Key=有, 浏览器配置=无', true, null, {
  poweredContains: '服务端默认', noticeDisplay: 'block', noticeClass: 'info', canRun: true
});
// 2) 服务端有 Key + 浏览器有配置 → 以浏览器配置为准，提示条隐藏
runCombo('组合2 服务端Key=有, 浏览器配置=有', true, { _preset: 'deepseek', model: 'deepseek-v4-pro' }, {
  poweredContains: '自定义', noticeDisplay: 'none', noticeClass: '', canRun: true
});
// 3) 服务端无 Key + 浏览器无配置 → 明确「未配置」红色阻断，不允许开跑
runCombo('组合3 服务端Key=无, 浏览器配置=无', false, null, {
  poweredContains: '未配置', noticeDisplay: 'block', noticeClass: 'warn', canRun: false
});
// 4) 服务端无 Key + 浏览器有配置 → 允许开跑
runCombo('组合4 服务端Key=无, 浏览器配置=有', false, { _preset: 'custom', model: 'x', api_key: 'sk-x' }, {
  poweredContains: '自定义', noticeDisplay: 'none', noticeClass: '', canRun: true
});

// 5) 网关模型列表必须完整暴露（不再被 filter 成 2 个）
const app5 = loadApp();
app5.sb._initLlmDefaults({ model: SERVER_MODEL, has_key: true, supported_models: GW_MODELS });
const gw = app5.sb.LLM_PROVIDER_PRESETS.server;
check('网关预设暴露全部模型', gw && gw.models && gw.models.length === GW_MODELS.length,
  '实际=' + (gw && gw.models ? gw.models.length : '无 server 预设') + ' 期望=' + GW_MODELS.length);

// 6) 凭据默认不落 localStorage（收紧后）
const app6 = loadApp();
app6.sb._saveLlmConfig({ _preset: 'custom', model: 'm', api_key: 'sk-secret' });
check('默认凭据写入 sessionStorage', !!app6.sessionStorage.getItem('ai_berkshire_llm'),
  'sessionStorage=' + JSON.stringify(app6.sessionStorage._dump()));
check('默认凭据不落 localStorage', !app6.localStorage.getItem('ai_berkshire_llm'),
  'localStorage=' + JSON.stringify(app6.localStorage._dump()));

const failed = results.filter(function (r) { return !r.ok; });
results.forEach(function (r) {
  console.log((r.ok ? '  PASS ' : '  FAIL ') + r.name + (r.ok ? '' : '  →  ' + r.detail));
});
console.log('\n状态机检查：' + (results.length - failed.length) + '/' + results.length + ' 通过');
process.exit(failed.length ? 1 : 0);
