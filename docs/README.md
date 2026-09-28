# 文档导航

- [项目使用说明](../README.md)：实际入口、当前功能、维护命令与限制。
- [开发约束](../AGENTS.md)：数据边界、鉴权、安全与交付要求。
- [设计与实施边界](FLET_DESIGN.md)：唯一维护的合同/计划；§1说明当前能力与限制，§8只保留下一阶段和验收底线，历史实施流水查Git。
- [下一阶段任务](FLET_DESIGN.md#next-stage)：一次只实施一个获准切片；下一项为S3-R1可靠性与可恢复反馈。
- [验收矩阵](FLET_DESIGN.md#acceptance)：用户任务U01—U08、安全与数据合同A01—A12；代码通过不等于部署或本人验收。

## 代码布局

| 路径 | 用途 |
|---|---|
| `screen.py` / `test_screen.py` | 原peer-screen-v1 CLI与算法测试 |
| `workspace.py` / `manage.py` | 三表工作区、快照导入、demo与维护命令；完整榜选择统一走`services.get_peer_discover` |
| `services.py` / `auth.py` / `app.py` | 私人查询/记录、鉴权、Flet Web/ASGI |
| `worker.py` / `watch.py` | 独立按需peer/watch任务与固定代码事实采集/验证 |
| `maintenance.py` / `test_maintenance.py` | 只读源库备份、新目录隔离恢复及测试 |
| `test_workspace.py` / `test_services.py` / `test_app.py` / `test_worker.py` | 离线工作区、服务、Web与任务恢复测试 |
| `test_mobile.py` / `tests/fixtures/` | 动态Web端到端浏览器流程与合成快照 |
| `Makefile` / `pyproject.toml` / `uv.lock` | 运行入口与独立依赖锁定 |
| `deploy.sh` / `test_deploy.py` | 部署脚本与隔离流程测试；执行需明确授权 |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Docker Compose与Nginx部署配置 |

测试脚本保留在根目录，`tests/`仅存合成fixture。日常使用见项目README；设计、任务、验收与研究取舍只在设计文档维护，不在导航重复记进度。

快速上手：`make setup`、`make demo`；代码检查`make check`，移动尺寸浏览器验证`make test-mobile`（需要Playwright Chromium，不代替真机）。demo/测试不依赖tracker、Token或生产数据。`output/`、`.local/`、`data/`是本地数据，不应作为临时文件清理。
