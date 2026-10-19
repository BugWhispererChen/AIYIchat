/* 主题读写：统一键 aiyichat_theme，同时兼容历史键并双写。
 *
 * 后台历史上用 admin-theme，C 端用 aiyichat_web_theme，两处各自演化导致
 * 同一次登录下两侧深浅色不一致。这里以新键为准读取，读到旧键时回落，
 * 并把结果同时写回所有键，保证老标签页与本次改造前已存偏好的用户都不丢设置。
 *
 * 本文件必须在 <head> 中同步加载：首帧前落定 data-theme，避免深色闪烁。
 */
(function () {
    'use strict';

    var PRIMARY_KEY = 'aiyichat_theme';
    var LEGACY_KEYS = ['admin-theme', 'aiyichat_web_theme'];

    function readSaved() {
        try {
            var value = localStorage.getItem(PRIMARY_KEY);
            if (value === 'dark' || value === 'light') return value;
            for (var i = 0; i < LEGACY_KEYS.length; i += 1) {
                var legacy = localStorage.getItem(LEGACY_KEYS[i]);
                if (legacy === 'dark' || legacy === 'light') return legacy;
            }
        } catch (e) { /* 隐私模式下 localStorage 不可用，退回系统偏好 */ }
        return null;
    }

    function persist(theme) {
        try {
            localStorage.setItem(PRIMARY_KEY, theme);
            for (var i = 0; i < LEGACY_KEYS.length; i += 1) {
                localStorage.setItem(LEGACY_KEYS[i], theme);
            }
        } catch (e) { /* 忽略写入失败，界面本身仍可用 */ }
    }

    function prefersDark() {
        return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    }

    var initial = readSaved() || (prefersDark() ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', initial);

    window.UiTheme = {
        current: function () {
            return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        },
        set: function (theme) {
            document.documentElement.setAttribute('data-theme', theme);
            persist(theme);
            // 通知依赖 CSS 变量取色的组件（如 Chart.js 图表）重新取色渲染
            document.dispatchEvent(new CustomEvent('ui-theme-change', { detail: { theme: theme } }));
        },
        toggle: function () {
            var next = this.current() === 'dark' ? 'light' : 'dark';
            this.set(next);
            return next;
        }
    };
})();
