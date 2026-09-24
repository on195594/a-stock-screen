# 文档导航

- [项目使用说明](../README.md)：实际入口、当前功能与限制。
- [开发约束](../AGENTS.md)：数据边界、鉴权、安全与验收要求。
- [Flet 设计（spec 1.1）](FLET_DESIGN.md)：研究信息、结果状态、导航、个人判断与三表安全合同；是目标，不代表已实现或生产验收。
- [实施路线图1.1](CODEX_IMPLEMENTATION.md)：在现有代码上按 **S1扫描看得懂 → S2持续研究 → S3真机与生产验收** 交付；含差量范围、用户任务U01—U06和安全测试A01—A12。

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

## 当前基线与下一步（2026-09-24，代码9a78ff3）

- **已有工程基础**：合成demo、同业扫描worker、公司事实、个人记录、部分比较/已阅逻辑及隔离测试。保留`make check`和390px Chromium流程，但它们不代表本轮新增用户任务已通过。
- **下一步只做S1差量**：实际指标与前三＋参照、依据/排除说明、空结果和partial/旧榜关系、任务结果与返回上下文。优先让结果比已有CLI报告更易理解，不再重建底层环境或worker。
- **S2待补**：固定关注更新、详情已阅→当前变化、首页下一步核查与暂停恢复。现有首页“更新资料”仍禁用；不能把同业重扫当作watch更新。
- **S3待验收**：本人OAuth、有限真实数据、常用真机、备份恢复与性能证据。运行容器不等于这些验收完成；部署状态需每次实时核对。

路线图1.1重排了S1/S2/S3的含义，旧版按工程模块划分的完成描述不再用作产品验收结论。本轮文档修订没有实现上述差量。

快速上手：在仓库根目录执行 `make setup`、`make demo`；检查运行 `make check`，浏览器移动尺寸验证用 `make test-mobile`（需要 Playwright Chromium）。演示与浏览器测试不需要 tracker、Token 或生产数据。`output/`、`.local/`、`data/` 是本地数据，不是仓库产物，不应清理为临时文件。
