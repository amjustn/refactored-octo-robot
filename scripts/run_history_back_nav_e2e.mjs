// run_history_back_nav_e2e.mjs —— 自顶向下验证的运行器（真实 Chromium 驱动 /app）
//
// 用法（需要一份 playwright 安装，默认 ~/tools/pw，可用 PW_DIR 覆盖）：
//     PW_DIR=~/tools/pw node scripts/run_history_back_nav_e2e.mjs
//     BASE=http://127.0.0.1:8001 PW_DIR=~/tools/pw node scripts/run_history_back_nav_e2e.mjs
//
// 它做三件事：打开 /app → 在页面里执行 verify_history_back_nav_e2e.js（真实点击流程断言）
// → 打印 JSON 实测结果；结论 PASS 退出码 0，FAIL 1，运行错误 2。
import { createRequire } from 'node:module';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';

const HOME = process.env.HOME;
const PW_DIR = process.env.PW_DIR || path.join(HOME, 'tools', 'pw');
const BASE = process.env.BASE || 'http://127.0.0.1:8001';
const require = createRequire(path.join(PW_DIR, 'package.json'));
const { chromium } = require('playwright');

// headless-shell 可能没下全（CDN 抽风），退回用完整版 chromium 可执行文件
function findChrome() {
  if (process.env.PW_CHROME) return process.env.PW_CHROME;
  const base = path.join(HOME, '.cache', 'ms-playwright');
  try {
    const dirs = readdirSync(base).filter((d) => d.startsWith('chromium-')).sort().reverse();
    for (const d of dirs) {
      const exe = path.join(base, d, 'chrome-linux', 'chrome');
      if (existsSync(exe)) return exe;
    }
  } catch (e) { /* 交给 playwright 默认解析 */ }
  return undefined;
}

const src = readFileSync(
  path.join(path.dirname(new URL(import.meta.url).pathname), 'verify_history_back_nav_e2e.js'),
  'utf8'
);

const errors = [];
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: findChrome(), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 200)); });
  await page.goto(BASE + '/app', { waitUntil: 'load', timeout: 30000 });
  await page.waitForTimeout(3000);   // 等技能列表 /api/llm/default / 会话恢复就位
  const result = await page.evaluate(src);
  console.log(JSON.stringify({ 页面错误: errors, 实测: result }, null, 2));
  const verdict = result && (result.总结论 || result.结论);
  process.exitCode = verdict === 'PASS' ? 0 : 1;
} catch (e) {
  console.log(JSON.stringify({ 结论: 'ERROR', 错误: String((e && e.message) || e), 页面错误: errors }, null, 2));
  process.exitCode = 2;
} finally {
  if (browser) await browser.close();
}
