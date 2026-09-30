// skills-nav.js — 技能导航 / 侧边栏 / 折叠 / quickStart（拆分自 app.js）

// ==================== Sidebar (mobile) ====================
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

// ── 批E(2026-09-11): 最近使用（最多 5 个，本地记录） ──────────────
var _RECENT_MAX = 5;

function recentSkills() {
  try { return JSON.parse(localStorage.getItem('ai_berkshire_recent') || '[]') || []; }
  catch(e) { return []; }
}

// 返回 true 表示列表有变化（调用方据此决定要不要重渲染侧栏）
function recentSkillsPush(name) {
  if (!name) return false;
  var list = recentSkills();
  if (list[0] === name) return false;
  list = list.filter(function(n) { return n !== name; });
  list.unshift(name);
  try { localStorage.setItem('ai_berkshire_recent', JSON.stringify(list.slice(0, _RECENT_MAX))); }
  catch(e) {}
  return true;
}

// ==================== Skills ====================
async function loadSkills() {
  try {
    var res = await fetch('/api/skills', { headers: getAuthHeaders() });
    if (res.status === 401) {
      // Token expired or server restarted with a new secret — re-login
      _clearJwt();
      location.reload();
      return;
    }
    var data = await res.json();
    allSkills = data.skills;
    document.getElementById('nav-loading').style.display = 'none';
    renderSkillNav(allSkills);
    if (!prefillFromURL()) {
      var saved = localStorage.getItem('ai_berkshire_skill');
      if (saved) selectSkill(saved);
      else if (allSkills.length > 0) selectSkill(allSkills[0].name);
    }
  } catch (e) {
    document.getElementById('nav-loading').innerHTML =
      '<p style="color:var(--accent-red);padding:12px 20px;font-size:0.8rem">技能加载失败 <button onclick="location.reload()" style="cursor:pointer;background:#30363d;color:#e6edf3;border:1px solid #484f58;border-radius:4px;padding:2px 10px;margin-left:8px">重试</button></p>';
    console.error('Failed to load skills:', e);
    setTimeout(function() {
      var params = new URLSearchParams(window.location.search);
      var skill = params.get('skill') || '';
      var args = params.get('arguments') || params.get('company') || '';
      if (skill && args) {
        document.getElementById('arg-field').value = args;
        document.getElementById('skill-name').textContent = skill;
        if (params.get('auto') !== '0' && params.get('noauto') !== '1') {
          setTimeout(function() {
            try { startResearch(); } catch(e2) { console.error('auto-start failed:', e2); }
          }, 1000);
        }
      }
    }, 500);
  }
}

// Build nav sections dynamically from API categories — new categories
// (e.g. 日报) appear automatically without touching the HTML.
function renderSkillNav(skills) {
  var nav = document.getElementById('nav-categories');
  var cats = [];
  var catMap = {};
  skills.forEach(function(s) {
    if (!catMap[s.category]) {
      catMap[s.category] = [];
      cats.push(s.category);
    }
    catMap[s.category].push(s);
  });

  var html = '';

  // Favorites pinned on top
  var favs = skills.filter(function(s) { return _favorites.has(s.name); });
  if (favs.length) {
    html += buildSection('⭐ 收藏', 'favorites', favs);
  }
  // 批E: 最近使用（最多 5 个，本地记录）
  var recents = recentSkills().map(function(n) {
    return skills.find(function(s) { return s.name === n; });
  }).filter(Boolean);
  if (recents.length) {
    html += buildSection('🕘 最近使用', 'recent', recents);
  }
  cats.forEach(function(cat) {
    html += buildSection(cat, cat, catMap[cat]);
  });
  nav.innerHTML = html;

  // 折叠态：优先用用户保存的状态；首次访问（无任何记录）只展开
  // 「收藏 / 最近使用 / 当前技能所在分类」，其余收起（批E，避免首屏 22 个技能全铺）
  var state = {};
  try { state = JSON.parse(localStorage.getItem('ai_berkshire_toggle') || '{}'); } catch(e) {}
  var firstRun = Object.keys(state).length === 0;
  var activeName = (currentSkill && currentSkill.name) || localStorage.getItem('ai_berkshire_skill') || '';
  var activeSkill = skills.find(function(s) { return s.name === activeName; });
  nav.querySelectorAll('.nav-section').forEach(function(section) {
    var items = section.querySelector('.nav-items');
    var header = section.querySelector('.nav-header');
    if (!items || !header) return;
    var cat = items.id ? items.id.replace('cat-', '') : '';
    var collapsed;
    if (firstRun) {
      collapsed = !(cat === 'favorites' || cat === 'recent' ||
        (activeSkill && cat === activeSkill.category));
    } else {
      collapsed = !!state[cat];
    }
    header.classList.toggle('collapsed', collapsed);
    items.classList.toggle('collapsed', collapsed);
    header.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  });

  _bindNavKeyboard(nav);
  filterSkills();
}

// 批E(2026-09-11): 技能项与收藏星改为真 <button>，补齐 aria 与焦点态 ——
// 此前是带 onclick 的 div/span，键盘与读屏都不可达（只在鼠标下可用）。
function buildSection(label, key, skills) {
  var items = skills.map(function(s) {
    var badge = s.is_multi_agent ? '<span class="multi-badge">' + s.agent_count + 'A</span>' : '';
    var fav = _favorites.has(s.name);
    var label2 = s.display_name || s.name;
    return '<div class="nav-item-row">' +
      '<button type="button" class="nav-item" data-skill="' + escHtml(s.name) + '"' +
      ' onclick="selectSkill(\'' + escHtml(s.name) + '\')"' +
      ' title="' + escHtml(s.description || '') + '"' +
      ' aria-label="' + escHtml(label2) + (s.is_multi_agent ? '（多 Agent）' : '') + '">' +
      '<span class="nav-item-name">' + escHtml(label2) + badge + '</span></button>' +
      '<button type="button" class="fav-star' + (fav ? ' on' : '') + '" data-star="' + escHtml(s.name) + '"' +
      ' aria-pressed="' + (fav ? 'true' : 'false') + '"' +
      ' aria-label="' + (fav ? '取消收藏 ' : '收藏 ') + escHtml(label2) + '"' +
      ' onclick="toggleFav(event,\'' + escHtml(s.name) + '\')">★</button>' +
      '</div>';
  }).join('');
  return '<div class="nav-section">' +
    '<button type="button" class="nav-header" aria-expanded="true" onclick="toggleSection(this)">' +
    '<span>' + escHtml(label) + '</span><span class="arrow" aria-hidden="true">V</span></button>' +
    '<div class="nav-items" id="cat-' + escHtml(key) + '" data-cat="' + escHtml(label) + '">' +
    items + '</div></div>';
}

// 方向键在技能之间移动焦点（Tab 已可用，方向键是键盘用户的常规预期）
function _bindNavKeyboard(nav) {
  if (!nav || nav.dataset.kbBound === '1') return;
  nav.dataset.kbBound = '1';
  nav.addEventListener('keydown', function(e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    var focusables = Array.prototype.slice
      .call(nav.querySelectorAll('.nav-item, .nav-header'))
      .filter(function(el) { return el.offsetParent !== null; });
    var i = focusables.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    var next = e.key === 'ArrowDown' ? i + 1 : i - 1;
    if (next >= 0 && next < focusables.length) focusables[next].focus();
  });
}

function toggleFav(e, name) {
  e.stopPropagation();
  if (_favorites.has(name)) _favorites.delete(name);
  else _favorites.add(name);
  localStorage.setItem('ai_berkshire_favs', JSON.stringify(Array.from(_favorites)));
  // Re-render nav but keep the active skill highlighted
  var active = currentSkill ? currentSkill.name : null;
  renderSkillNav(allSkills);
  if (active) {
    document.querySelectorAll('.nav-item[data-skill="' + active + '"]')
      .forEach(function(el) { el.classList.add('active'); });
  }
}

function filterSkills() {
  var q = (document.getElementById('skill-search').value || '').trim().toLowerCase();
  document.querySelectorAll('#nav-categories .nav-section').forEach(function(section) {
    var visible = 0;
    var rows = section.querySelectorAll('.nav-item-row');
    var targets = rows.length ? rows : section.querySelectorAll('.nav-item');
    targets.forEach(function(row) {
      var btn = row.querySelector ? (row.querySelector('.nav-item') || row) : row;
      var skill = allSkills.find(function(s) { return s.name === btn.dataset.skill; });
      var hay = ((btn.textContent || '') + ' ' +
        (skill ? (skill.name + ' ' + (skill.description || '')) : '')).toLowerCase();
      var show = !q || hay.indexOf(q) >= 0;
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    section.style.display = visible ? '' : 'none';
  });
}

function selectSkill(name) {
  var skill = allSkills.find(function(s) { return s.name === name; });
  if (!skill) return;

  currentSkill = skill;
  // Close history panel when switching skills
  document.getElementById('history-area').style.display = 'none';
  document.getElementById('costs-area').style.display = 'none';
  document.getElementById('decisions-area').style.display = 'none';
  localStorage.setItem('ai_berkshire_skill', name);

  document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.nav-item[data-skill="' + name + '"]')
    .forEach(function(el) { el.classList.add('active'); });

  document.getElementById('skill-name').textContent = skill.name;
  document.getElementById('skill-label').textContent = skill.display_name || '';
  document.getElementById('skill-icon').textContent = CAT_ICONS[skill.category] || '?';
  document.getElementById('skill-desc').textContent = skill.description || '';
  document.getElementById('input-hint').textContent = skill.input_hint || '';
  document.getElementById('arg-field').placeholder = skill.input_hint || '描述你的分析目标...';

  var btn = document.getElementById('btn-research');
  btn.textContent = skill.is_multi_agent ? '启动 ' + skill.agent_count + ' Agent 并行研究' : '开始研究';

  // On mobile, close the drawer after picking a skill
  if (window.innerWidth <= 860) document.body.classList.remove('sidebar-open');

  // 批E: 更新「最近使用」；列表有变化才重渲染（避免每次点击都重排侧栏）
  if (recentSkillsPush(name)) {
    renderSkillNav(allSkills);
    document.querySelectorAll('.nav-item[data-skill="' + name + '"]')
      .forEach(function(el) { el.classList.add('active'); });
  }
}

// ==================== Sidebar Toggle ====================
function toggleSection(header) {
  header.classList.toggle('collapsed');
  var items = header.nextElementSibling;
  items.classList.toggle('collapsed');
  header.setAttribute('aria-expanded', header.classList.contains('collapsed') ? 'false' : 'true');
  var cat = items.id ? items.id.replace('cat-', '') : '';
  if (cat) {
    var state = JSON.parse(localStorage.getItem('ai_berkshire_toggle') || '{}');
    state[cat] = header.classList.contains('collapsed');
    localStorage.setItem('ai_berkshire_toggle', JSON.stringify(state));
  }
}

function quickStart(skillName, args) {
  selectSkill(skillName);
  document.getElementById('arg-field').value = args;
  startResearch();
}

// ── 搜索框防「浏览器自动填充」 ────────────────────────────────
// 症状：浏览器（实测 Edge）把模型设置里填过的 Base URL（如 https://api.deepseek.com）
// 自动填进侧栏搜索框；实测触发点不止「点搜索框」，**点技能也会**——selectSkill() 里的
// renderSkillNav() 会重建侧栏 DOM，浏览器重新解析表单就把记住的值填回来。
// 根因在浏览器自动填充：搜索框与 Base URL 框都是无 name 的 type=text，浏览器把它们
// 当成同一类"可填字段"共享记住的值；autocomplete="off" 对自动填充不生效。
// 应用侧兜底原则：**不是用户自己敲/粘产生的值，且长得像网址，一律清掉**。
// 因此判定不看"谁触发"，改成比对"用户自己产生的值"（_userValue），
// 并叠加定时巡检 + 侧栏重渲染监听 —— 浏览器什么时候塞进来都能清掉。
(function () {
  var el = document.getElementById('skill-search');
  if (!el) return;
  var _userValue = '';      // 用户自己敲/粘产生的值（浏览器填充不会触发 input 事件）
  var _lastLog = 0;

  function _looksInjected(v) {
    v = (v || '').trim();
    if (!v) return false;
    return v.indexOf('://') >= 0 || /^https?\./i.test(v) || v.indexOf('api.deepseek.com') >= 0;
  }

  function _purge(why) {
    var v = el.value;
    if (!v || v === _userValue) return;      // 空值 / 用户自己产生的值 → 不动
    if (!_looksInjected(v)) return;          // 用户查技能的中文/英文关键词 → 不动
    el.value = '';
    _userValue = '';
    var now = Date.now();
    if (now - _lastLog > 2000) {
      _lastLog = now;
      console.info('[skill-search] 已清除浏览器自动填充写入的值：' + v + '（触发：' + why + '）');
    }
    if (typeof filterSkills === 'function') filterSkills();
  }

  function _schedule(why) {
    // 浏览器填充发生在 focus/mousedown 前后，多打几个时间点，避免时序漏网
    [0, 120, 400].forEach(function (d) {
      setTimeout(function () { _purge(why + '+' + d + 'ms'); }, d);
    });
  }

  // ① 用户自己产生的值：只有真实输入/粘贴才更新（浏览器自动填充不发 input 事件）
  el.addEventListener('input', function () { _userValue = el.value; });
  el.addEventListener('paste', function () { _userValue = el.value; }, true);

  // ② 定时巡检：不依赖任何触发时机（覆盖"没碰它、自己冒出来"）
  setInterval(function () { _purge('巡检'); }, 400);

  // ③ 侧栏重渲染（点技能 → renderSkillNav）后立即补一次
  var nav = document.getElementById('nav-categories');
  if (nav && typeof MutationObserver === 'function') {
    new MutationObserver(function () { _schedule('侧栏重渲染'); })
      .observe(nav, { childList: true, subtree: true });
  }

  // ④ 交互触发点（保留，反应比巡检快）
  el.addEventListener('mousedown', function () { _schedule('mousedown'); }, true);
  el.addEventListener('focus', function () { _schedule('focus'); }, true);
  window.addEventListener('load', function () { setTimeout(function () { _purge('load'); }, 250); });
})();
