// verify_skill_search_autofill.mjs —— 搜索框「浏览器自动填充」防范的端到端验证
// 用法：cd /home/q/ai_berkshire_web && PW_DIR=$HOME/tools/pw node scripts/verify_skill_search_autofill_e2e.mjs
// 变异测试：把 skills-nav.js 里 _purge 的这行改成 if (true) return; 后重跑，应转红（实测 8/13）
import { createRequire } from 'node:module';
import { readdirSync, existsSync } from 'node:fs';
import path from 'node:path';

const HOME = process.env.HOME;
const PW_DIR = process.env.PW_DIR || path.join(HOME, 'tools', 'pw');
const BASE = process.env.BASE || 'http://127.0.0.1:8001';
const require = createRequire(path.join(PW_DIR, 'package.json'));
const { chromium } = require('playwright');

function findChrome() {
  const base = path.join(HOME, '.cache', 'ms-playwright');
  try {
    const dirs = readdirSync(base).filter((d) => d.startsWith('chromium-')).sort().reverse();
    for (const d of dirs) {
      const exe = path.join(base, d, 'chrome-linux', 'chrome');
      if (existsSync(exe)) return exe;
    }
  } catch (e) { /* fallback */ }
  return undefined;
}

const checks = [];
function check(name, ok, detail) { checks.push({ 项: name, 结果: ok ? 'PASS' : 'FAIL', 实测: detail }); }

const pageErrors = [];
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: findChrome(), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => pageErrors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') pageErrors.push('console: ' + m.text().slice(0, 200)); });

  await page.goto(BASE + '/app', { waitUntil: 'load', timeout: 30000 });
  await page.waitForTimeout(3000);

  const visible = () => page.evaluate(`(() => {
    var n = 0;
    document.querySelectorAll('#nav-categories .nav-section').forEach(function (s) {
      if (s.style.display === 'none') return;
      var rows = s.querySelectorAll('.nav-item-row');
      var targets = rows.length ? rows : s.querySelectorAll('.nav-item');
      targets.forEach(function (r) { if (r.style.display !== 'none') n++; });
    });
    return n;
  })()`);

  const attrs = await page.evaluate(`(() => {
    var s = document.getElementById('skill-search');
    var b = document.getElementById('llm-base-url');
    return {
      s_name: s && s.getAttribute('name'),
      s_val: s && s.value,
      s_autocomplete: s && s.getAttribute('autocomplete'),
      b_name: b && b.getAttribute('name')
    };
  })()`);
  check('搜索框有独立 name', attrs.s_name === 'berkshire-skill-search', attrs.s_name);
  check('Base URL 框有自己的 name 且不同名', attrs.b_name === 'berkshire-llm-base-url', attrs.b_name);
  check('打开页面时搜索框为空', attrs.s_val === '', JSON.stringify(attrs.s_val));

  const baseline = await visible();
  check('技能列表已渲染（基线可数）', baseline > 0, `可见条目=${baseline}`);

  // ── 核心 1：模拟浏览器自动填充（先设值，再 mousedown/focus）──
  await page.evaluate(`(() => {
    var el = document.getElementById('skill-search');
    el.value = 'https://api.deepseek.com';
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
  })()`);
  await page.waitForTimeout(700);
  let v = await page.evaluate(`document.getElementById('skill-search').value`);
  check('注入的 deepseek 网址被清掉（先设值后聚焦）', v === '', JSON.stringify(v));

  // ── 核心 2：模拟浏览器先给焦点、再填值 ──
  await page.evaluate(`(() => {
    var el = document.getElementById('skill-search');
    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
    setTimeout(function () { el.value = 'https://api.deepseek.com'; }, 50);
  })()`);
  await page.waitForTimeout(700);
  v = await page.evaluate(`document.getElementById('skill-search').value`);
  check('注入的 deepseek 网址被清掉（先聚焦后设值）', v === '', JSON.stringify(v));

  // ── 核心 3：同一批记录里另一串中转站网址 ──
  await page.evaluate(`(() => {
    var el = document.getElementById('skill-search');
    el.value = 'https://dashscope.aliyuncs.com/compatible-mode/v1';
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
  })()`);
  await page.waitForTimeout(700);
  v = await page.evaluate(`document.getElementById('skill-search').value`);
  check('注入的中转站网址也被清掉', v === '', JSON.stringify(v));

  const afterPurge = await visible();
  check('清空后技能列表恢复完整', afterPurge === baseline, `可见条目=${afterPurge}（基线 ${baseline}）`);

  // ── 反向：用户自己打字不受影响 ──
  await page.click('#skill-search');
  await page.keyboard.type('财', { delay: 40 });
  await page.waitForTimeout(300);
  v = await page.evaluate(`document.getElementById('skill-search').value`);
  const typed = await visible();
  check('用户输入的中文保留（不被误清）', v === '财', JSON.stringify(v));
  check('用户输入仍能过滤技能（0 < 命中 < 基线）', typed > 0 && typed < baseline, `可见条目=${typed}（基线 ${baseline}）`);

  // ── 反向：用户粘贴不被误清 ──
  await page.evaluate(`(() => {
    var el = document.getElementById('skill-search');
    el.dispatchEvent(new ClipboardEvent('paste', { bubbles: true }));
    el.value = '腾讯';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  })()`);
  await page.waitForTimeout(600);
  v = await page.evaluate(`document.getElementById('skill-search').value`);
  check('用户粘贴的值保留', v === '腾讯', JSON.stringify(v));

  // ── 收尾：清空搜索框，列表恢复 ──
  await page.fill('#skill-search', '');
  await page.evaluate(`filterSkills()`);
  await page.waitForTimeout(200);
  const restore = await visible();
  check('清空搜索框后列表回到基线', restore === baseline, `可见条目=${restore}（基线 ${baseline}）`);

  check('页面无 JS 报错', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | ') || '无');

  const failed = checks.filter((c) => c.结果 === 'FAIL');
  console.log(JSON.stringify({
    通过: checks.length - failed.length, 失败: failed.length,
    总结论: failed.length === 0 ? 'PASS' : 'FAIL', 明细: checks
  }, null, 2));
  process.exitCode = failed.length === 0 ? 0 : 1;
} catch (e) {
  console.log(JSON.stringify({ 总结论: 'ERROR', 错误: String((e && e.message) || e), 页面错误: pageErrors }, null, 2));
  process.exitCode = 2;
} finally {
  if (browser) await browser.close();
}
