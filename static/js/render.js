// render.js — 报告渲染 / 追问 Q&A / 目录 TOC + scroll-spy（拆分自 app.js）

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
