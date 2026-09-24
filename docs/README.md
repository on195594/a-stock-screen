# 文档导航

- [项目使用说明](../README.md)：实际入口、当前功能与限制。
- [开发约束](../AGENTS.md)：数据边界、鉴权、安全与验收要求。
- [Flet 设计](FLET_DESIGN.md)：目标架构与三表存储合同；是设计，不代表生产验收。
- [实施切片](CODEX_IMPLEMENTATION.md)：S1/S2/S3 的原定任务和验收条件；其中的「待执行」表述是编写时状态，以当前代码和测试为准。

## 当前代码布局

| 路径 | 用途 |
|---|---|
| `screen.py` / `test_screen.py` | 原有 peer-screen-v1 CLI 与算法测试 |
| `workspace.py` / `manage.py` | 三表工作区、快照导入、demo 初始化 |
| `services.py` / `auth.py` / `app.py` / `worker.py` | 查询与个人记录、鉴权、Flet Web/ASGI、独立按需同业更新 worker |
| `test_workspace.py` / `test_services.py` / `test_app.py` / `test_worker.py` | 离线工作区、服务、Web 与任务恢复测试 |
| `test_mobile.py` / `tests/fixtures/` | 浏览器端到端脚本与合成快照 |
| `Makefile` / `pyproject.toml` / `uv.lock` | 运行与依赖锁定 |
| `deploy.sh` / `test_deploy.py` | 本机一键部署、隔离的部署流程测试；用法见[项目说明](../README.md) |
| `Dockerfile` / `docker-compose.yml` / `docker/` | 本机 Docker Compose + Nginx 部署配置；匿名入口可达，不等于生产 OAuth 或移动真机验收 |

测试脚本保留在根目录，避免破坏 `AGENTS.md` 和现有 `make check` 的入口；`tests/` 仅存合成 fixture。

## 实现状态（以当前工作区为准）

- **S1 已有代码**：离线合成 demo、同业发现、公司详情、个人记录及已阅；`make check` 覆盖算法、工作区和应用安全，`make test-mobile` 覆盖 390px Chromium 流程。设置与返回按钮已有浏览器点击回归；首页同请求只复用关注行，跨请求重验快照。公网匿名登录页可达，真机与生产 OAuth 仍须单独验证。
- **S2 同业按需更新已有代码，未部署/未验收生产链路**：选原关注参照、点击提交、独立 worker、任务页与恢复均可在合成 demo 操作；生产真实 TuShare 与 OAuth 未实测。首页“关注资料更新”仍禁用，未实现 watch 类任务。CLI `--refresh` 仍可显式联网。
- **S3 未验收**：已有离线快照比较与已阅逻辑，但不等于完整变化审查和真机验证。

快速上手：在仓库根目录执行 `make setup`、`make demo`；检查运行 `make check`，浏览器移动尺寸验证用 `make test-mobile`（需要 Playwright Chromium）。演示与浏览器测试不需要 tracker、Token 或生产数据。`output/`、`.local/`、`data/` 是本地数据，不是仓库产物，不应清理为临时文件。
