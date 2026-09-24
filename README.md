# a-stock-screen

个人同业研究工具：`screen.py` 用当前 TuShare 基础信息、同日 PB 与连续三年 `roe_waa` 安排研究顺序；Flet Web 工作台用于查看快照、提交同业扫描和保存个人研究记录。不是买入建议、收益模型或 Framework A 的延续。设计与实施边界见 [文档导航](docs/README.md)、[Flet 设计](docs/FLET_DESIGN.md)和[实施切片](docs/CODEX_IMPLEMENTATION.md)。

## Web 工作台命令

| 项目命令 | 说明 |
|---|---|
| `make setup` | 用冻结锁文件安装独立环境依赖（需预先安装 `uv`） |
| `make demo` | 幂等初始化演示工作区并导入合成 fixture，启动动态 Web 与独立合成 worker（默认 127.0.0.1:8550） |
| `make check` | 执行离线单元/服务/安全测试及格式、lint、类型检查（不含浏览器测试） |
| `make test-mobile` | 启动隔离临时 demo 执行移动端浏览器（390px）端到端自动化测试 |

> 提示：运行 `make test-mobile` 前若系统尚未安装 Chromium 内核，可执行：
> ```bash
> uv run python3 -m playwright install chromium
> ```

**同业自动更新（按本人点击）**：登录后到“同业发现”，选择原关注清单里的参照公司，点击“查找同业”。Web 只登记任务，独立 worker 按已证明的上一交易日取 TuShare 数据（财务资料不以旧原始缓存冒充本次核查）、生成并核验快照；任务页显示真实状态，关闭页面后仍会继续，首页“最近同业更新”可找回任务。demo 使用合成 fixture，绝不连接 TuShare。不会定时选股；“关注资料更新”按钮仍未实现，旧的同业名次与个人备注不被任务覆盖。日历若尚未证明到昨日本次请求会拒绝，不会猜日期或静默使用旧日。公网匿名登录页已在浏览器打开；生产本人 GitHub OAuth、真机和真实 TuShare 更新仍须实际验证。工作区本地数据位于 `.local/`（demo）或 `STATE_DIR`，不是可清理的临时文件。

## VPS 部署（仅在本机执行）

此 VPS 当前运行的是**上一版 Web**，本次新增 worker/参照选择尚未部署。仓库的 Docker Compose 已定义 Web + 独立 worker；Docker Nginx 位于 `/home/lin/nginx`。生产部署前，本人须将 `.worker.env.example` 复制为仅本机使用的 `.worker.env`，填入 `TUSHARE_TOKEN`（不要将它交给 Web，也不要提交 Git）；已存在的 `.env` 仅供 Web 认证。Compose 只读挂载 tracker 的关注清单、已证明日历和原库；原库不被修改。先核对 `data/` 是 production 工作区、日历覆盖昨日和只读源路径可访问，再经明确授权在本机运行 `./deploy.sh`。脚本预检、`make check`、构建重建 Web、校验/平滑重载 Nginx、回环 HTTPS 检查，最后启动并检查 worker；失败非零退出，**不自动回滚已重建的 Web**。生产 Compose 仍可能加载公共 CDN；demo 使用本地资源。仅支持此 VPS 布局，需 Docker 权限和独立环境；不要在 demo 环境执行部署。提交 Git 不会自动部署；`.env`、`.worker.env`、工作区均不得提交。

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
