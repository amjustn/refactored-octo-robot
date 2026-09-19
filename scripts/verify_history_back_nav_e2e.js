// verify_history_back_nav_e2e.js —— 自顶向下验证：/app 历史报告「打开 → ← 返回」回到原处
// 用法：在 http://127.0.0.1:8001/app 页面上下文执行本文件内容（async IIFE，返回 JSON）。
//      推荐经 scripts/run_history_back_nav_e2e.mjs 用真实 Chromium 驱动运行。
//
// 场景覆盖（全部为实测值，不做推测）：
//   A 默认列表 + 滚动     B 搜索过滤 + 滚动     C 技能筛选/按大小排序 + 滚到底部
//   D 对照：非历史来源「返回」仍回主页面
//   E 对比两份报告 → 「← 返回列表」也回到进入对比前的列表位置
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const $ = (s) => document.querySelector(s);
  const itemsOf = () => Array.from(document.querySelectorAll('#history-list .history-item'));

  async function openHistory() {
    await showHistory();
    for (let i = 0; i < 60 && itemsOf().length === 0; i++) await sleep(250);
    await sleep(120);
  }

  function stateOf() {
    return {
      scrollTop: $('#history-area').scrollTop,
      search: $('#historySearch').value,
      skill: $('#historySkillFilter').value,
      sort: $('#historySort').value
    };
  }

  function sameState(a, b) {
    return a.scrollTop === b.scrollTop && a.search === b.search && a.skill === b.skill && a.sort === b.sort;
  }

  const results = [];
  const out = { 条目总数: 0, 场景: results };

  // 全新浏览器会话没有模型配置时，loadReport 的前置守卫会直接拦下（真实用户是配过模型的）。
  // 注入服务端网关模型名以放行守卫；只影响「能不能打开」，不触碰被测的返回逻辑。
  if (!(function () { const c = _loadLlmConfig(); return c && (c.model || c.api_key || c.base_url); })()) {
    const m = (typeof _serverDefaultModel === 'string' && _serverDefaultModel) ? _serverDefaultModel : 'deepseek-chat';
    try { sessionStorage.setItem('ai_berkshire_llm', JSON.stringify({ model: m })); } catch (e) {}
    out.注入模型配置 = m;
  }

  // 单场景：造列表状态 → 点开一条 → 点「← 返回」→ 断言回到原处
  async function scenario(name, setup, scrollTo) {
    const r = { 场景: name };
    await openHistory();
    setup();
    await sleep(200);
    const area = $('#history-area');
    const items = itemsOf();
    r.条目数 = items.length;
    if (!items.length) { r.结论 = 'SKIP(无匹配条目)'; results.push(r); return r; }
    const maxScroll = Math.max(0, area.scrollHeight - area.clientHeight);
    area.scrollTop = Math.min(scrollTo === 'bottom' ? maxScroll : scrollTo, maxScroll);
    await sleep(100);
    r.可滚动 = maxScroll > 0;
    r.最大滚动 = maxScroll;

    // 优先点视口内的那条（真实场景就是点眼前这条），没有就退回第一条
    let target = null;
    for (const it of items) {
      const b = it.getBoundingClientRect();
      if (b.top >= 60 && b.bottom <= window.innerHeight - 20) { target = it; break; }
    }
    r.目标在视口内 = !!target;
    if (!target) target = items[0];
    r.目标报告 = target.dataset.name;

    const before = stateOf();
    r.点开前 = before;

    target.click();
    for (let i = 0; i < 40 && $('#result-area').style.display !== 'block'; i++) await sleep(250);
    r.点开后_结果区可见 = $('#result-area').style.display === 'block';
    r.点开后_列表隐藏 = $('#history-area').style.display === 'none';
    r.点开后_来源标记 = _reportOrigin;
    if (!r.点开后_结果区可见) { r.结论 = 'FAIL(报告未打开)'; results.push(r); return r; }

    $('#result-area .btn-back').click();
    await sleep(450);   // 留出 requestAnimationFrame 二次落位的窗口
    const after = stateOf();
    r.返回后 = after;
    r.返回后_列表可见 = $('#history-area').style.display === 'block';
    r.返回后_主页面隐藏 = $('#empty-state').style.display === 'none';
    r.返回后_结果区隐藏 = $('#result-area').style.display === 'none';
    const hl = $('#history-list .history-item.last-opened');
    r.返回后_高亮 = hl ? hl.dataset.name : null;

    r.断言_滚动位置一致 = after.scrollTop === before.scrollTop;
    r.断言_筛选状态一致 = sameState(after, before);
    r.断言_高亮命中同一条 = r.返回后_高亮 === r.目标报告;
    r.结论 = (r.返回后_列表可见 && r.返回后_主页面隐藏 && r.断言_滚动位置一致
      && r.断言_筛选状态一致 && r.断言_高亮命中同一条) ? 'PASS' : 'FAIL';
    results.push(r);
    return r;
  }

  await scenario('A 默认列表+滚动', () => {
    $('#historySearch').value = ''; $('#historySkillFilter').value = '';
    $('#historySort').value = 'time_desc'; filterHistory();
  }, 260);

  await scenario('B 搜索过滤+滚动', () => {
    $('#historySkillFilter').value = ''; $('#historySort').value = 'time_asc';
    $('#historySearch').value = 'investment'; filterHistory();
  }, 300);

  await scenario('C 技能筛选+按大小排序+滚到底', () => {
    const sel = $('#historySkillFilter');
    const opt = Array.from(sel.options).find((o) => o.value);
    $('#historySearch').value = '';
    sel.value = opt ? opt.value : '';
    $('#historySort').value = 'size_desc'; filterHistory();
  }, 'bottom');

  const c = results.find((x) => x.场景.startsWith('C'));
  if (c && c.点开前) out.场景C筛选 = { 技能: c.点开前.skill, 排序: c.点开前.sort, 滚动: c.点开前.scrollTop };

  // ── 场景 D：对照——非历史来源「返回」仍回主页面 ────────────────
  {
    const r = { 场景: 'D 对照:非历史来源回主页面' };
    await openHistory();
    _reportOrigin = '';
    $('#result-area').style.display = 'block';
    $('#history-area').style.display = 'block';
    $('#empty-state').style.display = 'none';
    backToHome();
    await sleep(150);
    r.主页面可见 = $('#empty-state').style.display === 'block';
    r.列表隐藏 = $('#history-area').style.display === 'none';
    r.结果区隐藏 = $('#result-area').style.display === 'none';
    r.结论 = (r.主页面可见 && r.列表隐藏 && r.结果区隐藏) ? 'PASS' : 'FAIL';
    results.push(r);
  }

  // ── 场景 E：对比 → 「← 返回列表」回到进入对比前的列表位置 ──────
  {
    const r = { 场景: 'E 对比返回列表位置' };
    await openHistory();
    $('#historySearch').value = ''; $('#historySkillFilter').value = '';
    $('#historySort').value = 'time_desc'; filterHistory();
    await sleep(200);
    const area = $('#history-area');
    area.scrollTop = Math.min(400, Math.max(0, area.scrollHeight - area.clientHeight));
    await sleep(100);
    const before = stateOf();
    const checks = Array.from(document.querySelectorAll('#history-list .compare-check')).slice(0, 2);
    r.可对比份数 = checks.length;
    if (checks.length < 2) { r.结论 = 'SKIP(报告不足两份)'; results.push(r); return out; }
    checks.forEach((cb) => { cb.checked = true; toggleCompareSelect(cb); });
    r.点开前 = before;
    await compareSelected();
    await sleep(400);
    r.对比页可见 = $('#compare-area').style.display === 'flex';
    closeCompare();
    await sleep(450);
    const after = stateOf();
    r.返回后 = after;
    r.返回后_列表可见 = $('#history-area').style.display === 'block';
    r.断言_滚动位置一致 = after.scrollTop === before.scrollTop;
    r.结论 = (r.对比页可见 && r.返回后_列表可见 && r.断言_滚动位置一致) ? 'PASS' : 'FAIL';
    results.push(r);
  }

  out.总结论 = results.every((r) => r.结论 === 'PASS') ? 'PASS' : 'FAIL';
  return out;
})()
