# 第三方前端库本地副本说明

本目录与 `../vendor/` 一起记录随仓库分发的第三方前端库来源与许可证，
目的是让界面在离线与内网环境同样完整可用（不再引用外部 CDN）。

| 库 | 版本 | 本地文件 | 上游获取地址 | 许可证 |
|---|---|---|---|---|
| Pico CSS | 2.1.1 | `../vendor/pico.min.css` | https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css | MIT（`pico.LICENSE.md`） |
| htmx | 2.0.4 | `../vendor/htmx.min.js` | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | 0BSD（`htmx.LICENSE.md`） |
| Chart.js | 4.4.0 | `../vendor/chart.umd.min.js` | https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js | MIT（`chartjs.LICENSE.md`） |

三者均为上游发布的原始构建产物，未做修改。升级时请同步替换 `vendor/`
内文件与本表的版本号，并重新取回对应许可证文本。
