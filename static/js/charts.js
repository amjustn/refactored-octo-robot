/* Financial / valuation charts for research reports (P0 可视化).
 * Automatically attaches an echarts panel under the report when the
 * report targets a resolvable stock.  Silent when unresolvable.
 */
(function () {
  'use strict';

  var CHARTS_API = '/api/charts/financials';
  var PALETTE = ['#6b8e6b', '#c4a77d', '#c4665a', '#7a6b5c', '#8ca8a0'];
  var PANEL_ID = 'financial-charts';

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmt(v, digits) {
    if (v == null || isNaN(v)) return '-';
    return Number(v).toLocaleString('zh-CN', {
      minimumFractionDigits: digits == null ? 2 : digits,
      maximumFractionDigits: digits == null ? 2 : digits
    });
  }

  /* Extract the target stock from report markdown:
   *   1. heading like "# 投资论文追踪：腾讯 (0700.HK)" -> code
   *   2. any A-share code (6 digits)
   *   3. Chinese company name right after the first ：/: in a heading */
  function extractQuery(md) {
    if (!md) return null;
    var m = md.match(/#[^\n]*[：:][^\n]*\((\d{4}\.HK|[A-Z]{2,5})\)/);
    if (m) return m[1];
    m = md.match(/#[^\n]*[：:][^\n]*\((\d{6})\)/);
    if (m) return m[1];
    m = md.match(/\b(6\d{5}|0\d{5}|3\d{5})\b/);
    if (m) return m[1];
    m = md.match(/#[^\n]*[：:]\s*([\u4e00-\u9fa5A-Za-z0-9·]{2,14})/);
    if (m) return m[1];
    return null;
  }

  function authHeaders() {
    var headers = {};
    var jwt = localStorage.getItem('ai_berkshire_jwt');
    if (jwt) headers['Authorization'] = 'Bearer ' + jwt;
    return headers;
  }

  function fetchSeries(query) {
    return fetch(CHARTS_API + '?query=' + encodeURIComponent(query), { headers: authHeaders() })
      .then(function (r) { return r.json(); })
      .catch(function () { return { resolved: false }; });
  }

  function percentileBadge(pct) {
    if (pct == null) return '<span class="fc-badge fc-badge-na">分位 -</span>';
    var cls = 'fc-badge fc-badge-low', label = '低估';
    if (pct >= 75) { cls = 'fc-badge fc-badge-high'; label = '高估'; }
    else if (pct >= 40) { cls = 'fc-badge fc-badge-mid'; label = '中性'; }
    return '<span class="' + cls + '">历史分位 ' + fmt(pct, 0) + '% ' + label + '</span>';
  }

  function mkPanel(data) {
    var s = data.series || {};
    var p = data.percentiles || {};
    var el = document.createElement('div');
    el.className = 'fc-panel';
    el.id = PANEL_ID;
    el.innerHTML =
      '<div class="fc-head">' +
        '<span class="fc-title">📊 ' + escHtml(data.name || data.symbol) + '</span>' +
        '<span class="fc-sub">' + escHtml(data.symbol || '') + ' · ' +
          escHtml(data.market || '') + ' · ' + escHtml(data.units || '') + '</span>' +
        '<span class="fc-asof">' + escHtml(data.as_of || '') + '</span>' +
      '</div>' +
      '<div class="fc-badges">' +
        '<span class="fc-badge fc-badge-key">PE ' + fmt(p.pe_now, 1) + '</span>' + percentileBadge(p.pe_pct) +
        '<span class="fc-badge fc-badge-key">PB ' + fmt(p.pb_now, 1) + '</span>' + percentileBadge(p.pb_pct) +
      '</div>' +
      '<div class="fc-tabs">' +
        '<button class="fc-tab active" data-tab="profit">📈 盈利</button>' +
        '<button class="fc-tab" data-tab="cashflow">💰 现金流</button>' +
        '<button class="fc-tab" data-tab="valuation">📊 估值</button>' +
      '</div>' +
      '<div class="fc-chart" id="fc-chart-profit"></div>' +
      '<div class="fc-chart" id="fc-chart-cashflow" style="display:none"></div>' +
      '<div class="fc-chart" id="fc-chart-valuation" style="display:none"></div>';

    var name = data.name || data.symbol || '该标的';
    el.setAttribute('data-label', name);
    el.setAttribute('data-symbol', data.symbol || '');
    return el;
  }

  function timeSeries(dates, values) {
    var out = [];
    for (var i = 0; i < dates.length; i++) {
      if (values[i] == null) continue;
      out.push([dates[i], values[i]]);
    }
    return out;
  }

  function baseGrid() {
    return { left: 56, right: 24, top: 40, bottom: 64, containLabel: true };
  }

  function drawProfit(container, data) {
    var s = data.series;
    var chart = echarts.init(container);
    chart.setOption({
      color: PALETTE,
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return fmt(v); } },
      legend: { data: ['营业总收入', '净利润(TTM)', '毛利率'], top: 0 },
      grid: baseGrid(),
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
      xAxis: { type: 'category', data: s.dates, axisLabel: { color: '#8c8273' } },
      yAxis: [
        { type: 'value', name: '亿元', axisLabel: { color: '#8c8273' } },
        { type: 'value', name: '%', max: 100, axisLabel: { color: '#8c8273' }, splitLine: { show: false } }
      ],
      series: [
        { name: '营业总收入', type: 'line', data: s.revenue, smooth: true, symbol: 'none', areaStyle: { opacity: 0.08 } },
        { name: '净利润(TTM)', type: 'line', data: s.net_profit, smooth: true, symbol: 'none' },
        { name: '毛利率', type: 'line', yAxisIndex: 1, data: s.gross_margin, smooth: true, symbol: 'none', lineStyle: { type: 'dashed' } }
      ]
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  function drawCashflow(container, data) {
    var s = data.series;
    var chart = echarts.init(container);
    chart.setOption({
      color: PALETTE,
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return fmt(v); } },
      grid: baseGrid(),
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
      xAxis: { type: 'category', data: s.dates, axisLabel: { color: '#8c8273' } },
      yAxis: { type: 'value', name: '亿元', axisLabel: { color: '#8c8273' } },
      series: [
        { name: '经营现金流净额', type: 'bar', data: s.ocf, itemStyle: { color: 'rgba(107,142,107,0.55)' } }
      ]
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  function drawValuation(container, data) {
    var s = data.series;
    var p = data.percentiles || {};
    var peData = (s.pe || []).map(function (x) { return [x.date, x.value]; });
    var pbData = (s.pb || []).map(function (x) { return [x.date, x.value]; });
    var chart = echarts.init(container);
    var series = [];
    if (peData.length) {
      series.push({
        name: 'PE(TTM)', type: 'line', data: peData, smooth: true, symbol: 'none',
        markLine: p.pe_now != null ? {
          symbol: 'none', data: [{ yAxis: p.pe_now, name: '当前 ' + fmt(p.pe_now, 1) }],
          lineStyle: { color: '#c4665a', type: 'dashed' },
          label: { formatter: '当前 ' + fmt(p.pe_now, 1) + '（分位 ' + fmt(p.pe_pct, 0) + '%）' }
        } : undefined
      });
    }
    if (pbData.length) {
      series.push({
        name: 'PB', type: 'line', data: pbData, smooth: true, symbol: 'none',
        markLine: p.pb_now != null ? {
          symbol: 'none', data: [{ yAxis: p.pb_now, name: '当前 ' + fmt(p.pb_now, 1) }],
          lineStyle: { color: '#c4665a', type: 'dashed' },
          label: { formatter: '当前 ' + fmt(p.pb_now, 1) + '（分位 ' + fmt(p.pb_pct, 0) + '%）' }
        } : undefined
      });
    }
    if (!series.length) {
      container.innerHTML = '<div class="fc-empty">估值历史数据暂不可用</div>';
      return;
    }
    chart.setOption({
      color: PALETTE,
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return fmt(v, 2); } },
      legend: { data: series.map(function (x) { return x.name; }), top: 0 },
      grid: baseGrid(),
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
      xAxis: { type: 'time', axisLabel: { color: '#8c8273' } },
      yAxis: { type: 'value', axisLabel: { color: '#8c8273' } },
      series: series
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  function bindTabs(el) {
    var tabs = el.querySelectorAll('.fc-tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        var name = this.getAttribute('data-tab');
        for (var j = 0; j < tabs.length; j++) tabs[j].classList.remove('active');
        this.classList.add('active');
        ['profit', 'cashflow', 'valuation'].forEach(function (k) {
          var c = el.querySelector('#fc-chart-' + k);
          if (c) c.style.display = (k === name) ? '' : 'none';
        });
        var chartEl = el.querySelector('#fc-chart-' + name);
        if (chartEl && chartEl.children.length === 0 && !chartEl.classList.contains('fc-empty')) {
          if (name === 'profit') drawProfit(chartEl, el._fcData);
          else if (name === 'cashflow') drawCashflow(chartEl, el._fcData);
          else drawValuation(chartEl, el._fcData);
        }
      });
    }
  }

  function insertPanel(data) {
    var resultBody = document.getElementById('report-content');
    if (!resultBody) return;
    var old = document.getElementById(PANEL_ID);
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var el = mkPanel(data);
    el._fcData = data;
    bindTabs(el);
    // Insert right after the report content, before the follow-up area.
    var parent = resultBody.parentNode;
    var followUp = document.getElementById('follow-up-area');
    if (followUp) parent.insertBefore(el, followUp);
    else parent.appendChild(el);
    drawProfit(document.getElementById('fc-chart-profit'), data);
  }

  function attachFinancialCharts(md) {
    if (!md || !window.echarts) return;
    var query = extractQuery(md);
    if (!query) return;
    fetchSeries(query).then(function (data) {
      if (!data || data.resolved === false) return;
      insertPanel(data);
    });
  }

  window.attachFinancialCharts = attachFinancialCharts;
})();
