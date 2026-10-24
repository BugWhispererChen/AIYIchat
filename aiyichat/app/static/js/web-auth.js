/* C 端鉴权请求封装：令牌缺失/过期一律回登录页。
 *
 * 令牌键优先读 <body data-token-key="...">，缺省为 aiyichat_web_token，
 * 与登录页、聊天页保持同一约定（改动需同步两侧模板）。
 */
(function () {
    'use strict';

    var DEFAULT_TOKEN_KEY = 'aiyichat_web_token';
    var TOKEN_KEY = document.body.dataset.tokenKey || DEFAULT_TOKEN_KEY;

    function authHeader(extra) {
        var token = localStorage.getItem(TOKEN_KEY);
        return Object.assign(
            token ? { Authorization: 'Bearer ' + token } : {},
            extra || {}
        );
    }

    function toLogin() {
        window.location.replace('/web/login');
    }

    /* 统一取回 {status, ok, data, detail}：非 2xx 时 detail 用后端中文提示 */
    async function call(path, options) {
        var opts = Object.assign({}, options || {});
        opts.headers = authHeader(opts.headers);
        var resp;
        try {
            resp = await fetch(path, opts);
        } catch (e) {
            return { status: 0, ok: false, data: null, detail: '网络异常，请稍后重试' };
        }
        if (resp.status === 401) { toLogin(); return { status: 401, ok: false, data: null, detail: '登录已过期' }; }
        var data = await resp.json().catch(function () { return {}; });
        return { status: resp.status, ok: resp.ok, data: data, detail: data.detail || '' };
    }

    async function logout() {
        var token = localStorage.getItem(TOKEN_KEY);
        if (token) {
            try { await fetch('/api/auth/logout', { method: 'POST', headers: authHeader() }); }
            catch (e) { /* 注销失败也要清本地令牌，否则账号会被过期令牌反复打回 */ }
        }
        localStorage.removeItem(TOKEN_KEY);
        toLogin();
    }

    /* 主题切换：统一走 theme.js 的 UiTheme（内部已做新旧键双写） */
    window.toggleTheme = function () {
        if (window.UiTheme) window.UiTheme.toggle();
    };

    window.WebAuth = { call: call, logout: logout, toLogin: toLogin, authHeader: authHeader, TOKEN_KEY: TOKEN_KEY };

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('logout-btn');
        if (btn) btn.addEventListener('click', function (e) { e.preventDefault(); WebAuth.logout(); });
    });
})();
