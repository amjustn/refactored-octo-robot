// stats.js — 成本统计 / 决策日志面板（拆分自 app.js）

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
