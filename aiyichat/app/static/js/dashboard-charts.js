/* 仪表盘图表：颜色一律从 CSS 变量取，深浅色切换时整盘重绘。
 * 数据由模板注入 window.DASHBOARD_CHARTS（见 admin/templates/dashboard.html）。 */
(function () {
    'use strict';

    var instances = {};

    function token(name, fallback) {
        var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return value || fallback;
    }

    function palette() {
        return {
            primary: token('--chart-primary', '#4f46e5'),
            primarySoft: token('--chart-primary-soft', 'rgba(79,70,229,.12)'),
            success: token('--chart-success', '#0f8a5f'),
            successSoft: token('--chart-success-soft', 'rgba(15,138,95,.12)'),
            warning: token('--chart-warning', '#c47708'),
            danger: token('--chart-danger', '#b42318'),
            info: token('--chart-info', '#8b5cf6'),
            accent: token('--chart-accent', '#e08a2b'),
            track: token('--chart-track', '#e6e1d6'),
            text: token('--chart-text', '#a39a8d')
        };
    }

    function axisOpts(p) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: p.text, font: { size: 11 } }, grid: { color: p.track, display: false } },
                y: { beginAtZero: true, ticks: { color: p.text, font: { size: 11 } }, grid: { color: p.track } }
            }
        };
    }

    /* 无数据时用居中提示替换画布容器：既避免空图表，也保持盒子高度不跳版 */
    function showEmpty(canvas, text, color) {
        var holder = document.createElement('div');
        holder.className = 'chart-blank';
        holder.style.color = color;
        holder.textContent = text;
        canvas.parentNode.replaceChild(holder, canvas);
    }

    function draw(id, config) {
        var el = document.getElementById(id);
        if (!el) return;
        if (instances[id]) { instances[id].destroy(); delete instances[id]; }
        instances[id] = new Chart(el, config);
    }

    function render() {
        if (typeof Chart === 'undefined') return;
        var data = window.DASHBOARD_CHARTS || {};
        var p = palette();

        Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
        Chart.defaults.color = p.text;

        var msg = data.msg || { labels: [], values: [] };
        var msgEl = document.getElementById('msgDailyChart');
        if (msgEl) {
            if (!msg.values.length) { showEmpty(msgEl, '暂无数据', p.text); }
            else {
                draw('msgDailyChart', {
                    type: 'line',
                    data: { labels: msg.labels, datasets: [{
                        data: msg.values, borderColor: p.primary,
                        backgroundColor: p.primarySoft, fill: true, tension: .35,
                        pointRadius: 0, pointHoverRadius: 4, borderWidth: 2
                    }] },
                    options: axisOpts(p)
                });
            }
        }

        var lat = data.latency || { labels: [], values: [] };
        var latEl = document.getElementById('latencyChart');
        if (latEl) {
            if (!lat.values.length) { showEmpty(latEl, '暂无数据', p.text); }
            else {
                draw('latencyChart', {
                    type: 'line',
                    data: { labels: lat.labels, datasets: [{
                        data: lat.values, borderColor: p.success,
                        backgroundColor: p.successSoft, fill: true, tension: .35,
                        pointRadius: 0, pointHoverRadius: 4, borderWidth: 2
                    }] },
                    options: axisOpts(p)
                });
            }
        }

        var emo = data.emotion || { labels: [], values: [] };
        var emoEl = document.getElementById('emotionChart');
        if (emoEl) {
            if (!emo.values.length) { showEmpty(emoEl, '暂无情绪数据', p.text); }
            else {
                draw('emotionChart', {
                    type: 'doughnut',
                    data: { labels: emo.labels, datasets: [{
                        data: emo.values,
                        backgroundColor: [p.danger, p.warning, p.primary, p.success, p.accent, p.track],
                        borderWidth: 0, hoverOffset: 6
                    }] },
                    options: {
                        responsive: true, maintainAspectRatio: false, cutout: '62%',
                        plugins: { legend: { position: 'bottom', labels: { color: p.text, boxWidth: 10 } } }
                    }
                });
            }
        }
    }

    function boot() {
        if (typeof Chart === 'undefined') { window.addEventListener('load', render); return; }
        render();
    }

    document.addEventListener('ui-theme-change', render);
    document.addEventListener('DOMContentLoaded', boot);
})();
