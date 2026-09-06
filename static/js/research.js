// research.js — 输入校验 / 文件上传 / 进度 / research 编排 / 失败任务（拆分自 app.js）

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

