// report-actions.js — 报告动作 / 历史 / 对比 / 断点续跑（拆分自 app.js）

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
