// init.js — 启动引导（唯一 DOMContentLoaded）/ URL 深链 / pagehide（拆分自 app.js，加载顺序最后）

document.addEventListener('DOMContentLoaded', function() {
  // Restore favorites
  try {
    _favorites = new Set(JSON.parse(localStorage.getItem('ai_berkshire_favs') || '[]'));
  } catch(e) { _favorites = new Set(); }

  // Restore failed tasks
  renderFailedTasks();
  updateUploadVisibility();

  // 报告渲染依赖 DOMPurify 做 XSS 清洗；本地化成 /static/js/purify.min.js 后
  // 仍需在启动时断言一次——缺失时明确报错，而不是在渲染时静默白屏。
  if (typeof DOMPurify === 'undefined') {
    showToast('⚠ 渲染安全库 DOMPurify 未加载（/static/js/purify.min.js），报告无法安全显示，请强制刷新（Ctrl+F5）', 'error');
    console.error('[init] DOMPurify missing — report rendering is blocked');
  }

  // Restore last report (需要可用模型：自有配置 或 服务端默认模型)
  restoreLastReportIfPossible();

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
    .catch(function() { _initLlmDefaults(null); })
    .then(function() { restoreLastReportIfPossible(); });  // 服务端默认模型可用时同样允许恢复
  loadSkills();
});

// 恢复上次会话的报告：自有配置 或 服务端默认模型 任一可用即可。
// 幂等——DOMContentLoaded 与 /api/llm/default 返回后各调一次，只有第一次生效。
var _reportRestored = false;

function restoreLastReportIfPossible() {
  if (_reportRestored || currentReport) return;
  try {
    var savedReport = sessionStorage.getItem('ai_berkshire_report');
    if (!savedReport) return;
    if (!_hasUsableModel()) return;
    _reportRestored = true;
    var savedMeta = JSON.parse(sessionStorage.getItem('ai_berkshire_report_meta') || '{}');
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
  } catch(e) {}
}

function dismissRestoreBanner() {
  document.getElementById('restore-banner').style.display = 'none';
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
