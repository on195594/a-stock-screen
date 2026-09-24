# a-stock-screen

个人同业研究工具：`screen.py` 用当前 TuShare 基础信息、同日 PB 与连续三年 `roe_waa` 安排研究顺序；Flet Web 工作台用于离线浏览快照和保存个人研究记录。不是买入建议、收益模型或 Framework A 的延续。设计与实施边界见 [文档导航](docs/README.md)、[Flet 设计](docs/FLET_DESIGN.md)和[实施切片](docs/CODEX_IMPLEMENTATION.md)。

## Web 工作台命令 (S1)

| 项目命令 | 说明 |
|---|---|
| `make setup` | 用冻结锁文件安装独立环境依赖（需预先安装 `uv`） |
| `make demo` | 幂等初始化演示工作区并导入合成 fixture，启动动态 Web（默认 127.0.0.1:8550） |
| `make check` | 执行离线单元/服务/安全测试及格式、lint、类型检查（不含浏览器测试） |
| `make test-mobile` | 启动隔离临时 demo 执行移动端浏览器（390px）端到端自动化测试 |

> 提示：运行 `make test-mobile` 前若系统尚未安装 Chromium 内核，可执行：
> ```bash
> uv run python3 -m playwright install chromium
> ```

当前 S1 页面**不提供联网更新**：更新按钮禁用，S2 独立 worker 尚未实现；已有的离线变化/已阅逻辑不等于 S3 或真机验收。公网匿名登录页已在浏览器打开，生产 GitHub OAuth 和移动真机行为仍须实际验证。首页对同一次请求的已核验快照仅保留关注标的所需数据，下次请求重新核验；这不消除 Flet Web 首次加载运行时的耗时。工作区本地数据位于 `.local/`（demo）或配置的 `STATE_DIR`，不属于可清理的临时文件。

## VPS 部署（仅在本机执行）

此 VPS 使用 Docker Compose 运行 Web，`/home/lin/nginx` 中的 Docker Nginx 作 HTTPS/WSS 代理；应用当前只有 Web 容器，没有更新 worker。生产 Compose 未设置 `FLET_WEB_NO_CDN`，浏览器首屏仍可能请求外部公共 CDN；demo 则使用本地资源。代码更新后在本机运行 `./deploy.sh`：脚本从自身所在目录读取 `.env`，不拉取代码；先执行 `make check`，再构建并重建 Web 容器。站点配置有变化时备份到 `/home/lin/nginx/sites-enabled/stock.conf.bak.*`，经 `nginx -t` 通过才继续；最后平滑重载 Nginx，并检查回环地址的 Web 与 HTTPS 首页。构建或代理配置预检失败不会替换运行中的 Web 容器；重建后的健康检查失败会非零退出，**不会自动回滚 Web 镜像**，须人工检查。仅支持此 VPS 布局，需预先运行一次 `make setup` 并确保有 Docker 权限；不能在 demo 环境执行。提交 Git 不会自动部署；不要将 `.env` 或工作区数据提交到 Git。

## CLI 历史脚本使用

```bash
# 离线复看旧快照；也可同时与明确指定的旧快照比较
.venv/bin/python screen.py --input output/CURRENT_RUN/snapshot.json --compare output/OLDER_RUN/snapshot.json

# 显式联网刷新并生成报告（需提供 --tracker-root）
.venv/bin/python screen.py --tracker-root ../a-stock-tracker --anchor 600900.SH --refresh
```

## 离线验证

```bash
make check
```
