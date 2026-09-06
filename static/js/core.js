// core.js — 共享全局状态 / 基础 helpers / toast（拆分自 app.js，加载顺序第 1）

// 无论本页是手动打开还是新开标签页，统一窗口名，实现「已开则复用、不重复新开」。
// window.name 在后续导航中自动保留，这里只需在首次加载时设一次。
// AI Berkshire Web - Application Logic
let currentSkill = null;
let allSkills = [];
let currentReport = '';
let ws = null;
let _researchAbort = null;   // AbortController for batch mode
let _historyData = [];       // Cached history for search
let _wsReconnectAttempts = 0;
let _wsShouldReconnect = false;
const _WS_MAX_RECONNECT = 3;
const _WS_RECONNECT_DELAYS = [2000, 5000, 10000]; // ms

let currentTaskId = null;    // task_id reported by server (for real cancel)
let _agentStreamBuffers = {}; // per-agent stream buffers (event protocol v2)
let _userCancelled = false;  // distinguish user cancel from timeout abort
let _progressTimer = null;   // elapsed-time ticker
let _progressStartTs = 0;
let _renderScheduled = false; // streaming render throttle flag
let _favorites = new Set();  // starred skills
let _compareSelection = new Set(); // history compare checkboxes

// Category icons
const CAT_ICONS = {
  '深度研究': 'K',
  '财报分析': 'E',
  '行业筛选': 'I',
  '持仓管理': 'P',
  '思维工具': 'T',
  '日报': 'D'
};

// ==================== Helpers ====================
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return sec + '秒';
  var m = Math.floor(sec / 60), s = sec % 60;
  return m + '分' + (s ? s + '秒' : '');
}

function fmtTokens(t) {
  if (!t || !t.total_tokens) return '';
  var n = t.total_tokens;
  var s = n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n);
  return '约 ' + s + ' tokens';
}

function skillDisplayName(name) {
  var s = allSkills.find(function(x) { return x.name === name; });
  return s ? (s.display_name || s.name) : name;
}

// ==================== Toast ====================
function showToast(msg, type) {
  type = type || 'info';
  var c = document.getElementById('toast-container');
  var el = document.createElement('div');
  el.className = 'toast-msg ' + type;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(function() {
    el.classList.add('out');
    setTimeout(function() { el.remove(); }, 300);
  }, 3000);
}

// ==================== Init ====================

function _promptForToken(callback) {
  var secret = prompt('请输入访问令牌（服务器 BERKSHIRE_API_TOKEN）：');
  if (!secret) { document.body.innerHTML = '<div style="padding:40px;text-align:center;color:#c4a77d">需要令牌才能访问</div>'; return; }
  loginWithToken(secret).then(function(data) {
    if (data && data.note === 'Auth disabled') {
      localStorage.setItem('ai_berkshire_jwt', 'disabled');
    }
    callback();
  }).catch(function(e) {
    alert('令牌无效：' + e.message);
    _promptForToken(callback);
  });
}
