/* ════════════════════════════════════════════════════════════
   AI Berkshire Web 换肤面板 — 参考 dsh-dream-skin 设计
   右下角悬浮按钮 → 皮肤预设 / 强调色 / 背景照片(透明度+模糊)
   CSS 变量覆盖 + 服务端持久化（/api/skin），localStorage 兜底
   ════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var LS_KEY = 'bk_skin_v1';

  /* ── 皮肤预设（覆盖 style.css 的 :root 变量）── */
  var SKINS = [
    {
      id: 'default', name: '和風 · 禅（默认）',
      swatch: ['#f7f3e9', '#e8dfce', '#6b8e6b'],
      tokens: {}
    },
    {
      id: 'green', name: '抹茶 · 清',
      swatch: ['#eef3ea', '#dfe9d6', '#4d7c4d'],
      tokens: {
        '--bg': '#eef3ea', '--bg-secondary': '#e2ebdb', '--bg-card': '#f7faf3',
        '--border': '#c3d2b4', '--border-light': '#d5e0ca',
        '--text': '#2f3a2d', '--text-secondary': '#6e7a66',
        '--accent': '#4d7c4d', '--accent-green': '#4d7c4d',
        '--ink': '#2f3a2d', '--washi': '#eef3ea', '--kare': '#dde7d2'
      }
    },
    {
      id: 'pink', name: '桜 · 粉',
      swatch: ['#faf0f2', '#f2dfe4', '#c26a7e'],
      tokens: {
        '--bg': '#faf0f2', '--bg-secondary': '#f3e2e7', '--bg-card': '#fffafa',
        '--border': '#e0c2cb', '--border-light': '#ecd6dc',
        '--text': '#453038', '--text-secondary': '#92737e',
        '--accent': '#c26a7e', '--accent-green': '#c26a7e',
        '--ink': '#453038', '--washi': '#faf0f2', '--kare': '#f0dde2'
      }
    },
    {
      id: 'nightblue', name: '紺青 · 夜',
      swatch: ['#101826', '#182233', '#6ea8fe'],
      tokens: {
        '--bg': '#101826', '--bg-secondary': '#182233', '--bg-card': '#1d2940',
        '--border': '#2c3a52', '--border-light': '#263348',
        '--text': '#e3e8f0', '--text-secondary': '#93a0b5',
        '--accent': '#6ea8fe', '--accent-green': '#7bc496', '--accent-red': '#e08070',
        '--accent-yellow': '#d4b98a', '--accent-purple': '#9a86c8',
        '--ink': '#e3e8f0', '--washi': '#101826', '--kare': '#182233',
        '--shadow': '0 1px 3px rgba(0,0,0,0.4)'
      }
    },
    {
      id: 'black', name: '墨 · 黑',
      swatch: ['#121212', '#1e1e1e', '#c9a86a'],
      tokens: {
        '--bg': '#121212', '--bg-secondary': '#1c1c1c', '--bg-card': '#242424',
        '--border': '#363636', '--border-light': '#2c2c2c',
        '--text': '#e8e6e3', '--text-secondary': '#9a958c',
        '--accent': '#c9a86a', '--accent-green': '#8aa87f', '--accent-red': '#c4665a',
        '--accent-yellow': '#c9a86a', '--accent-purple': '#a08c6c',
        '--ink': '#e8e6e3', '--washi': '#121212', '--kare': '#1c1c1c',
        '--shadow': '0 1px 3px rgba(0,0,0,0.5)'
      }
    },
    {
      id: 'purple', name: '夜 · 紫',
      swatch: ['#171224', '#1f1830', '#b79cf0'],
      tokens: {
        '--bg': '#171224', '--bg-secondary': '#1f1830', '--bg-card': '#271d3a',
        '--border': '#382c50', '--border-light': '#2e2444',
        '--text': '#e9e4f5', '--text-secondary': '#a094c0',
        '--accent': '#b79cf0', '--accent-green': '#8ec49a', '--accent-red': '#e08070',
        '--accent-yellow': '#d4b98a', '--accent-purple': '#b79cf0',
        '--ink': '#e9e4f5', '--washi': '#171224', '--kare': '#1f1830',
        '--shadow': '0 1px 3px rgba(0,0,0,0.4)'
      }
    },
    /* ── 渐变系（柔和/克制/干净，莫兰迪压饱和；gradient 铺壁纸层，照片与渐变互斥）── */
    {
      id: 'mist-sakura', name: '雾蓝 · 樱粉',
      gradient: 'linear-gradient(135deg,#b7c5d2 0%,#d6cdd3 48%,#efd9de 100%)',
      swatch: ['#b7c5d2', '#d6cdd3', '#efd9de'],
      tokens: {
        '--bg': '#cdccd2', '--bg-secondary': '#d9d6d9', '--bg-card': '#f9f8f9',
        '--border': '#c2bcc2', '--border-light': '#d8d4d8',
        '--text': '#3a3f45', '--text-secondary': '#85888e',
        '--accent': '#7e95a8', '--accent-green': '#7e95a8',
        '--ink': '#3a3f45', '--washi': '#cdccd2', '--kare': '#d6cdd3'
      }
    },
    {
      id: 'lake-pink', name: '湖蓝 · 淡粉',
      gradient: 'linear-gradient(135deg,#a7c8d6 0%,#d2e0e4 45%,#f3dde3 100%)',
      swatch: ['#a7c8d6', '#d2e0e4', '#f3dde3'],
      tokens: {
        '--bg': '#cdd9dc', '--bg-secondary': '#dbe3e4', '--bg-card': '#fafbfb',
        '--border': '#c3cdd0', '--border-light': '#d5dfe1',
        '--text': '#37424a', '--text-secondary': '#828d92',
        '--accent': '#6d97ad', '--accent-green': '#6d97ad',
        '--ink': '#37424a', '--washi': '#cdd9dc', '--kare': '#d2e0e4'
      }
    },
    {
      id: 'mint-mist', name: '晨雾 · 青',
      gradient: 'linear-gradient(135deg,#b4cfc6 0%,#d5e1da 50%,#e6e8e2 100%)',
      swatch: ['#b4cfc6', '#d5e1da', '#e6e8e2'],
      tokens: {
        '--bg': '#d0d9d3', '--bg-secondary': '#dde3dc', '--bg-card': '#f9faf7',
        '--border': '#c3cdc5', '--border-light': '#d5ded6',
        '--text': '#35403a', '--text-secondary': '#828d85',
        '--accent': '#6d9487', '--accent-green': '#6d9487',
        '--ink': '#35403a', '--washi': '#d0d9d3', '--kare': '#d5e1da'
      }
    },
    {
      id: 'peach-cream', name: '奶油 · 蜜桃',
      gradient: 'linear-gradient(135deg,#e9dcc6 0%,#eecfc3 55%,#f2d4c8 100%)',
      swatch: ['#e9dcc6', '#eecfc3', '#f2d4c8'],
      tokens: {
        '--bg': '#e8d5c8', '--bg-secondary': '#f0e0d5', '--bg-card': '#fdf9f4',
        '--border': '#d8c5b8', '--border-light': '#e6d8cd',
        '--text': '#463b34', '--text-secondary': '#8f8177',
        '--accent': '#b08a72', '--accent-green': '#b08a72',
        '--ink': '#463b34', '--washi': '#e8d5c8', '--kare': '#eecfc3'
      }
    },
    {
      id: 'dusk-purple', name: '暮山 · 紫',
      gradient: 'linear-gradient(135deg,#242b45 0%,#3a3457 48%,#5c4b62 100%)',
      swatch: ['#242b45', '#3a3457', '#5c4b62'],
      tokens: {
        '--bg': '#36314d', '--bg-secondary': '#403959', '--bg-card': '#4a4168',
        '--border': '#51497a', '--border-light': '#453d66',
        '--text': '#e9e6f2', '--text-secondary': '#a49cc0',
        '--accent': '#9a8fc9', '--accent-green': '#8ec49a', '--accent-red': '#e08070',
        '--accent-yellow': '#d4b98a', '--accent-purple': '#9a8fc9',
        '--ink': '#e9e6f2', '--washi': '#36314d', '--kare': '#403959',
        '--shadow': '0 1px 3px rgba(0,0,0,0.4)'
      }
    },
    {
      id: 'deep-sea', name: '深海 · 蓝',
      gradient: 'linear-gradient(135deg,#15202e 0%,#1e3240 55%,#26434c 100%)',
      swatch: ['#15202e', '#1e3240', '#26434c'],
      tokens: {
        '--bg': '#1b2a37', '--bg-secondary': '#223546', '--bg-card': '#29404f',
        '--border': '#33536a', '--border-light': '#2a4255',
        '--text': '#e3e9ec', '--text-secondary': '#93a6b0',
        '--accent': '#6f9db5', '--accent-green': '#7bc496', '--accent-red': '#e08070',
        '--accent-yellow': '#d4b98a', '--accent-purple': '#9a86c8',
        '--ink': '#e3e9ec', '--washi': '#1b2a37', '--kare': '#223546',
        '--shadow': '0 1px 3px rgba(0,0,0,0.4)'
      }
    }
  ];

  /* ── 状态 ── */
  var state = { skin: 'default', accent: '', wallpaper: '', wpOpacity: 0.5, wpBlur: 0, sidebarAlpha: 0.85, gradient: '' };

  // 旧版皮肤ID → 双站共享ID 自动迁移
  var ID_MIGRATE = { matcha:'green', sakura:'pink', kon:'nightblue', sumi:'black', yoru:'purple' };
  function normalizeId() { if (ID_MIGRATE[state.skin]) state.skin = ID_MIGRATE[state.skin]; }

  function applyState(s) {
    for (var k in state) if (s[k] !== undefined) state[k] = s[k];
  }
  function loadState() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (raw) applyState(JSON.parse(raw));
    } catch (e) { /* 保持默认 */ }
  }

  function authHeaders() {
    var jwt = localStorage.getItem('ai_berkshire_jwt') || '';
    return jwt ? { 'Authorization': 'Bearer ' + jwt } : {};
  }

  /* ── 服务端同步 ── */
  var _saveTimer = null;
  function pushToServer(obj) {
    try {
      var h = authHeaders();
      h['Content-Type'] = 'application/json';
      fetch('/api/skin', { method: 'POST', headers: h, body: JSON.stringify(obj) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.code !== 0 && typeof showToast === 'function') showToast(d.message || '皮肤同步失败', 'warning');
        })
        .catch(function () {});
    } catch (e) {}
  }
  function syncFromServer(done) {
    fetch('/api/skin', { headers: authHeaders() })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.code === 0 && d.data) {
          applyState(d.data);
          normalizeId();
          try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (e) {}
        } else {
          try {
            var raw = localStorage.getItem(LS_KEY);
            if (raw) pushToServer(JSON.parse(raw));
          } catch (e) {}
        }
        if (done) done();
      })
      .catch(function () { if (done) done(); });
  }
  function saveState() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(state));
    } catch (e) {
      if (typeof showToast === 'function') showToast('壁纸过大，无法保存', 'warning');
      return;
    }
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(function () { pushToServer(state); }, 800);
  }

  /* ── 应用皮肤 ── */
  function currentSkin() {
    for (var i = 0; i < SKINS.length; i++) if (SKINS[i].id === state.skin) return SKINS[i];
    return SKINS[0];
  }

  function applyAll() {
    var skin = currentSkin();
    var styleEl = document.getElementById('bkSkinOverrides');
    if (!styleEl) {
      styleEl = document.createElement('style');
      styleEl.id = 'bkSkinOverrides';
      document.head.appendChild(styleEl);
    }
    var tokens = {};
    for (var k in skin.tokens) tokens[k] = skin.tokens[k];
    if (state.accent) {
      tokens['--accent'] = state.accent;
      tokens['--accent-green'] = state.accent;
    }
    var decls = [];
    for (var t in tokens) decls.push(t + ':' + tokens[t]);
    // 渐变皮肤：渐变直接铺 body 背景（真正的页面底色，不依赖 z-index:-1 壁纸层，不会被
    // body 实心背景盖住）；照片仍走 wp 层叠在渐变之上。非渐变皮肤时此规则不注入，
    // body 恢复 style.css 的默认背景（radial 装饰）
    var css = decls.length ? ':root{' + decls.join(';') + '}' : '';
    if (state.gradient) css += 'body{background-image:' + state.gradient + ';background-attachment:fixed}';
    styleEl.textContent = css;

    var wp = document.getElementById('bkSkinWallpaper');
    if (!wp) {
      wp = document.createElement('div');
      wp.id = 'bkSkinWallpaper';
      // NOTE: transition 只保留 opacity —— filter(blur) 动画在全屏 fixed 层上会让
      // Edge/Chrome GPU 每帧重栅格化，是标签页崩溃的高危点（dsh-dream-skin 同款警示）
      wp.style.cssText = 'position:fixed;inset:0;z-index:-1;pointer-events:none;background-size:cover;background-position:center;transition:opacity .3s';
      document.body.appendChild(wp);
    }
    // 背景层：照片 wallpaper 独立铺在 wp 层（z-index:-1，位于 body 渐变之上、内容之下）；
    // 渐变皮肤时渐变铺在 body 背景（见上方 css 注入），照片半透明叠加在渐变之上
    if (state.wallpaper) {
      wp.style.backgroundImage = 'url(' + state.wallpaper + ')';
      wp.style.opacity = state.wpOpacity;
      wp.style.filter = state.wpBlur > 0 ? 'blur(' + state.wpBlur + 'px)' : 'none';
      wp.style.display = '';
    } else {
      wp.style.display = 'none';
    }

    // 表面半透明（参考 dsh-dream-skin：壁纸铺底时把大块表面填充降为半透明，
    // 壁纸透出、文字仍可读；无壁纸时保持原实色，不改变现有外观）
    var sbStyle = document.getElementById('bkSkinSidebarRule');
    if (state.wallpaper || state.gradient) {
      if (!sbStyle) {
        sbStyle = document.createElement('style');
        sbStyle.id = 'bkSkinSidebarRule';
        document.head.appendChild(sbStyle);
      }
      var sbPct = Math.round(state.sidebarAlpha * 100);
      sbStyle.textContent = '#sidebar{background:color-mix(in srgb, var(--bg-secondary) ' + sbPct + '%, transparent) !important;}' +
        '#input-area{background:color-mix(in srgb, var(--bg) ' + sbPct + '%, transparent) !important;}';
    } else if (sbStyle) {
      sbStyle.textContent = '';
    }
  }

  /* ── 照片压缩（≤2MB）── */
  var MAX_FILE_MB = 12;  // 超过则拒绝：全分辨率解码会瞬间吃掉数百MB内存
  function compressImage(file, cb) {
    if (file.size > MAX_FILE_MB * 1024 * 1024) { cb(null, 'too_large'); return; }
    function finish(bmp) {
      var maxW = 1280;
      var scale = Math.min(1, maxW / bmp.width, maxW / bmp.height);
      var w = Math.max(1, Math.round(bmp.width * scale));
      var h = Math.max(1, Math.round(bmp.height * scale));
      var canvas = document.createElement('canvas');
      canvas.width = w; canvas.height = h;
      canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
      if (bmp.close) bmp.close();
      if (bmp.tagName === 'IMG') bmp.src = '';
      var q = 0.8, url = canvas.toDataURL('image/jpeg', q);
      while (url.length > 1024 * 1024 * 1.37 && q > 0.3) {
        q -= 0.12;
        url = canvas.toDataURL('image/jpeg', q);
      }
      canvas.width = canvas.height = 0;  // 立即释放位图内存
      cb(url);
    }
    // 优先 createImageBitmap：解码时直接缩放，不在内存驻留全尺寸位图
    if (window.createImageBitmap) {
      createImageBitmap(file, { resizeWidth: 1280, resizeQuality: 'high' })
        .then(finish)
        .catch(function () { legacyDecode(file, cb); });
    } else {
      legacyDecode(file, cb);
    }
  }
  function legacyDecode(file, cb) {
    var reader = new FileReader();
    reader.onload = function (e) {
      var img = new Image();
      img.onload = function () { finish(img); };
      img.onerror = function () { cb(null); };
      img.src = e.target.result;
    };
    reader.onerror = function () { cb(null); };
    reader.readAsDataURL(file);
  }

  /* ── UI（面板跟随当前皮肤的 CSS 变量）── */
  function buildUI() {
    var btn = document.createElement('button');
    btn.id = 'bkSkinFab';
    btn.title = '换肤';
    btn.innerHTML = '🎨';
    btn.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:9999;width:44px;height:44px;' +
      'border-radius:50%;border:1px solid var(--border);background:var(--bg-card);' +
      'color:var(--text);font-size:19px;cursor:pointer;box-shadow:0 3px 12px rgba(0,0,0,0.18);' +
      'transition:transform .2s';
    btn.onmouseenter = function () { btn.style.transform = 'scale(1.1)'; };
    btn.onmouseleave = function () { btn.style.transform = 'scale(1)'; };

    var panel = document.createElement('div');
    panel.id = 'bkSkinPanel';
    panel.style.cssText = 'position:fixed;right:20px;bottom:74px;z-index:9999;width:290px;display:none;' +
      'background:var(--bg-card);border:1px solid var(--border);' +
      'border-radius:10px;padding:16px;box-shadow:0 10px 32px rgba(0,0,0,0.2);color:var(--text);' +
      'font-size:13px;max-height:70vh;overflow-y:auto;' +
      'font-family:"Noto Serif SC","Source Han Serif SC",Georgia,serif';

    var html = '<div style="font-weight:600;margin-bottom:10px">✨ 皮肤</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">';
    SKINS.forEach(function (s) {
      html += '<div class="bk-skin-item" data-skin="' + s.id + '" style="cursor:pointer;border-radius:6px;' +
        'padding:6px;border:2px solid transparent;text-align:center">' +
        '<div style="height:32px;border-radius:4px;background:linear-gradient(135deg,' +
        s.swatch[0] + ',' + s.swatch[1] + ' 60%,' + s.swatch[2] + ')"></div>' +
        '<div style="font-size:11px;margin-top:4px;color:var(--text-secondary)">' + s.name + '</div></div>';
    });
    html += '</div>';

    html += '<hr style="border:none;border-top:1px solid var(--border-light);margin:12px 0">' +
      '<div style="font-weight:600;margin-bottom:8px">🌈 强调色</div>' +
      '<div style="display:flex;align-items:center;gap:8px">' +
      '<input type="color" id="bkSkinAccent" style="width:42px;height:28px;border:none;background:none;cursor:pointer;padding:0">' +
      '<span style="font-size:11px;color:var(--text-secondary)">点色块自定义强调色</span>' +
      '<button id="bkSkinAccentReset" style="margin-left:auto;font-size:11px;background:var(--bg-secondary);' +
      'border:1px solid var(--border);color:var(--text-secondary);border-radius:4px;padding:3px 8px;cursor:pointer">跟随皮肤</button></div>';

    html += '<hr style="border:none;border-top:1px solid var(--border-light);margin:12px 0">' +
      '<div style="font-weight:600;margin-bottom:8px">🖼️ 背景照片</div>' +
      '<input type="file" id="bkSkinFile" accept="image/*" style="display:none">' +
      '<button id="bkSkinUpload" style="width:100%;background:var(--bg-secondary);border:1px dashed var(--border);' +
      'color:var(--text);border-radius:6px;padding:8px;cursor:pointer;font-size:12px">📷 上传照片（原图≤12MB，自动压缩）</button>' +
      '<div style="margin-top:10px;font-size:11px;color:var(--text-secondary)">透明度 <span id="bkSkinOpVal">50%</span></div>' +
      '<input type="range" id="bkSkinOpacity" min="10" max="100" value="50" style="width:100%">' +
      '<div style="font-size:11px;color:var(--text-secondary)">模糊 <span id="bkSkinBlurVal">0px</span></div>' +
      '<input type="range" id="bkSkinBlur" min="0" max="20" value="0" style="width:100%">' +
      '<button id="bkSkinWpClear" style="width:100%;margin-top:8px;background:none;border:none;' +
      'color:var(--text-secondary);font-size:11px;cursor:pointer;text-decoration:underline">移除背景照片</button>' +
      '<button id="bkSkinLightPreset" style="width:100%;margin-top:10px;background:var(--bg-secondary);border:1px solid var(--border);' +
      'color:var(--text);border-radius:6px;padding:8px;cursor:pointer;font-size:12px">☁️ 一键浅色（透明度25% + 模糊8px + 侧边栏透出）</button>' +
      '<div style="margin-top:10px;font-size:11px;color:var(--text-secondary)">表面透明度（侧边栏/输入区/工具栏，有壁纸时生效）<span id="bkSkinSbVal">85%</span></div>' +
      '<input type="range" id="bkSkinSidebar" min="20" max="100" value="85" style="width:100%">';

    html += '<hr style="border:none;border-top:1px solid var(--border-light);margin:12px 0">' +
      '<button id="bkSkinReset" style="width:100%;background:var(--bg-secondary);border:1px solid var(--border);' +
      'color:var(--text);border-radius:6px;padding:8px;cursor:pointer;font-size:12px">↩️ 恢复默认外观</button>' +
      '<div style="margin-top:10px;font-size:10px;color:var(--text-secondary);text-align:center;opacity:0.6">灵感来自 dsh-dream-skin · 双站同步</div>';

    panel.innerHTML = html;
    document.body.appendChild(btn);
    document.body.appendChild(panel);

    btn.onclick = function () {
      panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    };
    document.addEventListener('click', function (e) {
      if (panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
        panel.style.display = 'none';
      }
    });

    panel.querySelectorAll('.bk-skin-item').forEach(function (el) {
      el.onclick = function () {
        state.skin = el.dataset.skin;
        var sk = currentSkin();
        if (sk.gradient) {
          // 渐变皮肤：铺渐变底色；保留用户照片（叠加在渐变之上，透明度由滑块控制，不互斥）
          state.gradient = sk.gradient;
        } else {
          state.gradient = '';
        }
        applyAll(); saveState(); markSelected();
      };
    });
    function markSelected() {
      panel.querySelectorAll('.bk-skin-item').forEach(function (el) {
        el.style.borderColor = el.dataset.skin === state.skin ? 'var(--accent)' : 'transparent';
      });
    }
    markSelected();

    var accentInput = panel.querySelector('#bkSkinAccent');
    accentInput.value = state.accent || '#6b8e6b';
    accentInput.oninput = function () {
      state.accent = accentInput.value;
      applyAll(); saveState();
    };
    panel.querySelector('#bkSkinAccentReset').onclick = function () {
      state.accent = '';
      applyAll(); saveState();
    };

    var fileInput = panel.querySelector('#bkSkinFile');
    panel.querySelector('#bkSkinUpload').onclick = function () { fileInput.click(); };
    fileInput.onchange = function () {
      var f = fileInput.files[0];
      if (!f) return;
      compressImage(f, function (url, err) {
        if (err === 'too_large') {
          if (typeof showToast === 'function') showToast('图片超过12MB，请先压缩再上传', 'warning');
          return;
        }
        if (!url) {
          if (typeof showToast === 'function') showToast('图片读取失败', 'warning');
          return;
        }
        state.wallpaper = url;  // 照片与渐变共存：照片叠加在渐变之上
        applyAll(); saveState();
        if (typeof showToast === 'function') showToast('背景照片已应用', 'success');
      });
      fileInput.value = '';
    };

    var opRange = panel.querySelector('#bkSkinOpacity');
    var opVal = panel.querySelector('#bkSkinOpVal');
    opRange.value = Math.round(state.wpOpacity * 100);
    opVal.textContent = opRange.value + '%';
    opRange.oninput = function () {
      state.wpOpacity = opRange.value / 100;
      opVal.textContent = opRange.value + '%';
      applyAll(); saveState();
    };
    var blurRange = panel.querySelector('#bkSkinBlur');
    var blurVal = panel.querySelector('#bkSkinBlurVal');
    blurRange.value = state.wpBlur;
    blurVal.textContent = state.wpBlur + 'px';
    blurRange.oninput = function () {
      state.wpBlur = parseInt(blurRange.value, 10);
      blurVal.textContent = blurRange.value + 'px';
      applyAll(); saveState();
    };

    var sbRange = panel.querySelector('#bkSkinSidebar');
    var sbVal = panel.querySelector('#bkSkinSbVal');
    sbRange.value = Math.round(state.sidebarAlpha * 100);
    sbVal.textContent = sbRange.value + '%';
    sbRange.oninput = function () {
      state.sidebarAlpha = sbRange.value / 100;
      sbVal.textContent = sbRange.value + '%';
      applyAll(); saveState();
    };
    panel.querySelector('#bkSkinLightPreset').onclick = function () {
      state.wpOpacity = 0.25; state.wpBlur = 8; state.sidebarAlpha = 0.78;
      applyAll(); saveState();
      opRange.value = 25; opVal.textContent = '25%';
      blurRange.value = 8; blurVal.textContent = '8px';
      sbRange.value = 78; sbVal.textContent = '78%';
      if (typeof showToast === 'function') showToast('已切换浅色铺底', 'success');
    };

    panel.querySelector('#bkSkinWpClear').onclick = function () {
      state.wallpaper = '';  // 只清照片，当前皮肤的渐变底色保留
      applyAll(); saveState();
      pushToServer({ wallpaper: null });  // 显式移除标记：服务端据此清除共享壁纸
    };
    panel.querySelector('#bkSkinReset').onclick = function () {
      state = { skin: 'default', accent: '', wallpaper: '', wpOpacity: 0.5, wpBlur: 0, sidebarAlpha: 0.85, gradient: '' };
      applyAll(); saveState(); markSelected();
      pushToServer({ wallpaper: null });  // 恢复默认同样视为主动移除共享壁纸
      opRange.value = 50; opVal.textContent = '50%';
      blurRange.value = 0; blurVal.textContent = '0px';
      sbRange.value = 85; sbVal.textContent = '85%';
      if (typeof showToast === 'function') showToast('已恢复默认外观', 'success');
    };
  }

  /* ── 启动：按钮立刻出现，不等待网络 ── */
  loadState();
  normalizeId();
  function start() {
    applyAll();
    buildUI();
    syncFromServer(function () {
      applyAll();
      var b = document.getElementById('bkSkinFab');
      var p = document.getElementById('bkSkinPanel');
      var wasOpen = p && p.style.display !== 'none';
      if (b) b.remove();
      if (p) p.remove();
      buildUI();
      if (wasOpen) document.getElementById('bkSkinPanel').style.display = 'block';
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
