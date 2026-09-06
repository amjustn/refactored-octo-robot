// skills-nav.js — 技能导航 / 侧边栏 / 折叠 / quickStart（拆分自 app.js）

// ==================== Sidebar (mobile) ====================
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

// ==================== Skills ====================
async function loadSkills() {
  try {
    var res = await fetch('/api/skills', { headers: getAuthHeaders() });
    if (res.status === 401) {
      // Token expired or server restarted with a new secret — re-login
      localStorage.removeItem('ai_berkshire_jwt');
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
  cats.forEach(function(cat) {
    html += buildSection(cat, cat, catMap[cat]);
  });
  nav.innerHTML = html;

  // Restore collapsed states
  try {
    var state = JSON.parse(localStorage.getItem('ai_berkshire_toggle') || '{}');
    nav.querySelectorAll('.nav-section').forEach(function(section) {
      var items = section.querySelector('.nav-items');
      var header = section.querySelector('.nav-header');
      var cat = items.id ? items.id.replace('cat-', '') : '';
      if (state[cat]) {
        header.classList.add('collapsed');
        items.classList.add('collapsed');
      }
    });
  } catch(e) {}

  filterSkills();
}

function buildSection(label, key, skills) {
  var items = skills.map(function(s) {
    var badge = s.is_multi_agent ? '<span class="multi-badge">' + s.agent_count + 'A</span>' : '';
    var star = '<span class="fav-star' + (_favorites.has(s.name) ? ' on' : '') +
      '" data-star="' + escHtml(s.name) + '" onclick="toggleFav(event,\'' + escHtml(s.name) + '\')">★</span>';
    return '<div class="nav-item" data-skill="' + escHtml(s.name) + '" onclick="selectSkill(\'' + escHtml(s.name) + '\')" title="' + escHtml(s.description || '') + '">' +
      '<span class="nav-item-name">' + escHtml(s.display_name || s.name) + badge + '</span>' + star + '</div>';
  }).join('');
  return '<div class="nav-section">' +
    '<div class="nav-header" onclick="toggleSection(this)"><span>' + escHtml(label) + '</span><span class="arrow">V</span></div>' +
    '<div class="nav-items" id="cat-' + escHtml(key) + '" data-cat="' + escHtml(label) + '">' + items + '</div></div>';
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
    section.querySelectorAll('.nav-item').forEach(function(item) {
      var name = item.textContent.toLowerCase();
      var skill = allSkills.find(function(s) { return s.name === item.dataset.skill; });
      var hay = name + ' ' + (skill ? (skill.name + ' ' + (skill.description || '')).toLowerCase() : '');
      var show = !q || hay.indexOf(q) >= 0;
      item.style.display = show ? '' : 'none';
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
}

// ==================== Sidebar Toggle ====================
function toggleSection(header) {
  header.classList.toggle('collapsed');
  var items = header.nextElementSibling;
  items.classList.toggle('collapsed');
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
