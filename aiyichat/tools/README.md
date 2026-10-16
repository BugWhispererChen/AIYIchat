# 工具与脚本目录

本目录收录构建、种子数据与测试类脚本。

## 目录结构

```
tools/
├── seed_companion_profiles.py   # 预置角色种子数据（幂等 upsert；被 app/bootstrap.py 运行时导入，不可移动）
├── deploy_package/              # Linux 部署包构建与可用性测试
├── windows_service/             # Windows 服务安装包构建（PyInstaller + Inno Setup）
├── eval/                        # LLM 回复质量离线评估
├── archive/                     # 已失效/一次性脚本留档，不参与测试与打包（见 archive/README.md）
└── test/                        # pytest 自动化测试套件
```

## 测试

```bash
python tools/test/run_tests.py      # 运行 tools/test/ 下全部 pytest 用例
```

## 部署

Linux 部署包的构建、校验与可用性测试流程见 `deploy_package/README.md`；
面向部署者的使用文档是仓库上一级的 `aiyichat/README.md`（随部署包交付为包根 `README.md`）。
