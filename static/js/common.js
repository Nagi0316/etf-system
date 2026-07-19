/**
 * common.js — 共用認證、API 請求、深色模式、通知
 */

// ══════════════════════════════════════════════════════
//  深色模式
// ══════════════════════════════════════════════════════
(function initDarkMode() {
  const stored = localStorage.getItem('theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (stored === 'dark' || (!stored && prefersDark)) {
    document.documentElement.classList.add('dark');
  }
})();

function toggleDarkMode() {
  const html = document.documentElement;
  const isDark = html.classList.toggle('dark');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
  const btn = document.getElementById('dark-toggle');
  if (btn) btn.innerHTML = isDark
    ? '<i class="fas fa-sun"></i>'
    : '<i class="fas fa-moon"></i>';
}

// ══════════════════════════════════════════════════════
//  XSS 防護：HTML 轉義
// ══════════════════════════════════════════════════════
function _escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ══════════════════════════════════════════════════════
//  認證 — HttpOnly Cookie 方案
//  access_token 儲存在 HttpOnly cookie（JS 無法讀取）
//  etf_session=1 為非敏感狀態標記（JS 僅用於判斷登入狀態）
// ══════════════════════════════════════════════════════
const Auth = {
  _getCookie(name) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const m = document.cookie.match('(?:^|; )' + escaped + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : null;
  },

  isLoggedIn() {
    // etf_session 由伺服器在登入時設定，是非敏感標記
    return this._getCookie('etf_session') === '1';
  },

  setToken(_token) {
    // HttpOnly cookie 已由伺服器設定，此處為向下相容的空操作
    // 清除舊版 localStorage 殘留
    localStorage.removeItem('access_token');
  },

  clearToken() {
    document.cookie = 'etf_session=; Max-Age=0; path=/; SameSite=Lax';
    localStorage.removeItem('access_token');
  },

  loginUrl(path = window.location.pathname + window.location.search) {
    return '/auth?next=' + encodeURIComponent(path || '/');
  },

  redirectToLogin(path) {
    window.location.href = this.loginUrl(path);
  },

  headers(extra = {}) {
    // 不在 JS 中傳送 Bearer token（token 已在 HttpOnly cookie 中自動附帶）
    return { 'Content-Type': 'application/json', ...extra };
  },

  async fetch(url, opts = {}, timeoutMs = 10000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    opts.headers    = this.headers(opts.headers || {});
    opts.credentials = 'include';
    opts.signal     = ctrl.signal;
    try {
      const resp = await fetch(url, opts);
      if (resp.status === 401) {
        this.clearToken();
        this.redirectToLogin();
        return null;
      }
      return resp;
    } catch (e) {
      if (e.name === 'AbortError') {
        console.warn(`[timeout] ${url}`);
        return null;
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  },

  async fetchJson(url, opts = {}, timeoutMs = 10000) {
    const resp = await this.fetch(url, opts, timeoutMs);
    if (!resp) return null;
    try { return await resp.json(); }
    catch { return null; }
  },

  logout() {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
      .finally(() => {
        this.clearToken();
        window.location.href = '/';
      });
  }
};

// ══════════════════════════════════════════════════════
//  使用者資訊顯示（profile + 通知 並行抓取）
// ══════════════════════════════════════════════════════
async function loadUserInfo(containerId = 'user-info') {
  const container = document.getElementById(containerId);
  if (!container) return;

  if (!Auth.isLoggedIn()) {
    container.innerHTML = `<a href="${Auth.loginUrl()}" class="btn-primary text-sm px-3 py-1.5 rounded-lg">
      <i class="fab fa-google mr-1"></i>登入</a>`;
    return;
  }

  try {
    // 並行抓取 profile + 未讀通知數，避免串行等待
    const [profileData, notifData] = await Promise.all([
      Auth.fetchJson('/api/user/profile'),
      Auth.fetchJson('/api/notifications?unread_only=true', {}, 6000),
    ]);

    if (!profileData || profileData.status !== 'success') {
      // 注意：401 已由 Auth.fetch() 處理（自動 clearToken + 跳轉），
      // 抵達這裡表示是 timeout / 5xx 等暫時性錯誤，不清除 token，
      // 避免 TiDB 冷啟動瞬間把所有使用者踢出去。
      container.innerHTML = `<a href="${Auth.loginUrl()}" class="btn-primary text-sm px-3 py-1.5 rounded-lg">登入</a>`;
      return;
    }

    const u = profileData.data;
    // XSS 防護：所有使用者資料透過 _escHtml 轉義後才插入 HTML
    const safeName   = _escHtml(u.username || 'User');
    const safeEmail  = _escHtml(u.email || '');
    const safeAvatar = _escHtml(u.display_avatar || u.avatar || '');
    const initial    = _escHtml((u.username || 'U')[0].toUpperCase());

    const avatarHtml = safeAvatar
      ? `<img src="${safeAvatar}" alt="avatar" class="w-8 h-8 rounded-full object-cover border-2 border-indigo-300">`
      : `<div class="w-8 h-8 rounded-full bg-indigo-500 flex items-center justify-center text-white text-xs font-bold">${initial}</div>`;

    container.innerHTML = `
      <div class="flex items-center gap-2">
        <a href="/notifications" class="relative text-slate-400 hover:text-indigo-500 dark:text-slate-400 dark:hover:text-indigo-400 p-1" title="通知">
          <i class="fas fa-bell text-lg"></i>
          <span id="notif-badge" class="hidden absolute -top-1 -right-1 bg-red-500 text-white text-xs rounded-full w-4 h-4 flex items-center justify-center"></span>
        </a>
        <div class="relative group">
          ${avatarHtml}
          <div class="absolute right-0 mt-1 w-44 bg-white dark:bg-slate-800 shadow-lg rounded-xl border border-slate-100 dark:border-slate-700 opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 top-full">
            <div class="px-3 py-2 border-b border-slate-100 dark:border-slate-700">
              <p class="text-xs font-medium text-slate-800 dark:text-slate-100 truncate">${safeName}</p>
              <p class="text-xs text-slate-400 truncate">${safeEmail}</p>
            </div>
            <a href="/profile" class="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700"><i class="fas fa-user w-4"></i>個人資料</a>
            <a href="/portfolio" class="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700"><i class="fas fa-wallet w-4"></i>投資庫存</a>
            <a href="/notifications" class="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700"><i class="fas fa-bell w-4"></i>通知中心</a>
            <div class="border-t border-slate-100 dark:border-slate-700">
              <button onclick="Auth.logout()" class="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-500 hover:bg-red-50 dark:hover:bg-slate-700"><i class="fas fa-sign-out-alt w-4"></i>登出</button>
            </div>
          </div>
        </div>
      </div>`;

    // 更新通知紅點
    const cnt = notifData?.unread_count || 0;
    const badge = document.getElementById('notif-badge');
    if (badge) {
      if (cnt > 0) { badge.textContent = cnt > 9 ? '9+' : cnt; badge.classList.remove('hidden'); }
      else { badge.classList.add('hidden'); }
    }
  } catch (e) {
    console.warn('loadUserInfo error:', e);
  }
}

// ══════════════════════════════════════════════════════
//  Toast 通知
// ══════════════════════════════════════════════════════
function showToast(msg, isSuccess = true, duration = 3000) {
  const el = document.createElement('div');
  el.className = `fixed bottom-5 left-1/2 -translate-x-1/2 z-[9999] px-5 py-3 rounded-xl shadow-xl text-white text-sm font-medium flex items-center gap-2 transition-all duration-300 opacity-0 translate-y-4`;
  el.style.backgroundColor = isSuccess ? '#10b981' : '#ef4444';
  // 訊息內容使用 textContent 而非 innerHTML，防止 toast 本身被 XSS 利用
  const icon = document.createElement('i');
  icon.className = `fas ${isSuccess ? 'fa-check-circle' : 'fa-exclamation-circle'}`;
  const text = document.createTextNode(msg);
  el.appendChild(icon);
  el.appendChild(text);
  document.body.appendChild(el);
  requestAnimationFrame(() => {
    el.classList.remove('opacity-0', 'translate-y-4');
    el.classList.add('opacity-100', 'translate-y-0');
  });
  setTimeout(() => {
    el.classList.add('opacity-0', 'translate-y-4');
    setTimeout(() => el.remove(), 300);
  }, duration);
}

// ══════════════════════════════════════════════════════
//  數字格式化
// ══════════════════════════════════════════════════════
function fmt(n, d = 2) {
  if (n == null) return '—';
  return parseFloat(n).toFixed(d);
}

function fmtPct(n, d = 2) {
  if (n == null) return '—';
  const v = parseFloat(n);
  return (v >= 0 ? '+' : '') + v.toFixed(d) + '%';
}

function fmtMoney(n, currency = 'TWD') {
  const v = parseFloat(n);
  if (n == null || isNaN(v)) return '—';
  if (currency === 'TWD') return `NT$${Math.round(v).toLocaleString()}`;
  return `$${v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

// 初始化頁面（深色模式按鈕圖示；loadUserInfo 由 base.html 統一呼叫，不在此重複）
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('dark-toggle');
  if (btn) {
    btn.innerHTML = document.documentElement.classList.contains('dark')
      ? '<i class="fas fa-sun"></i>'
      : '<i class="fas fa-moon"></i>';
  }
});
