# a-stock-screen

单用户A股研究工作台：**发现同业候选 → 关注 → 更新事实 → 比较变化 → 继续核查**。保留peer-screen-v1的PB/三年ROE研究排序，不给买入评分，不做交易、账户、通知或自动选股。

- [设计与实施边界](docs/FLET_DESIGN.md)：有效合同、下一阶段及验收标准。
- [开发约束](AGENTS.md) · [代码导航](docs/README.md)

## 本地演示与检查

需要独立Python 3.13环境、uv；不需要tracker、Token或生产数据。

```bash
make setup
uv run python -m playwright install chromium
make demo
# 浏览器打开 http://127.0.0.1:8550；Ctrl+C结束demo后再检查
make check
make test-mobile
```

重复demo保留`.local/demo`，测试用临时库。demo只允许loopback Host/Origin；手机不能直接访问VPS的127.0.0.1，可通过受控SSH转发，不放宽demo到公网。移动尺寸Chromium测试不代表本人真机验收。

## 日常使用

1. **同业发现**：选允许的参照、提交扫描，看前三＋参照的PB/年度ROE、范围及缺口；可一键关注，不要求笔记。粗行业、最多50家且偏向大市值；低PB/高ROE不是买入结论。
2. **我的关注**：按异常、公司状态、指标变化、仅日期、无变化分组；已填下一步显示摘要。暂停不计待阅，可恢复为观察，不删笔记或改变排名。
3. **更新资料**：只在本人点击时冻结1—50家未暂停公司和目标日，由独立worker执行；关页面不取消。固定关注更新不重排同业，失败/partial与旧可用资料分别显示。
4. **公司详情**：当前事实与真实变化优先；“查看全部字段与来源”展开分组只读对照。首次无空旧值，未知不等于无风险。当前已阅仍在“可选笔记、状态与已阅”内；保存笔记不推进已阅，缺口/基准异常会阻止确认。
5. **笔记与退出**：理由、下一步、外部https链接均可空；链接目前只有保存/编辑，没有打开入口。未保存离开会提示，强制刷新/关闭可能丢草稿；冲突时重开核对，不能覆盖其他页面的新版本。
6. **重新扫描/清理**：任务页重扫需确认才提交；回执不确定重试复用请求ID。删除研究记录需确认且不可撤销；清理失败任务只隐藏任务，不删快照或消除事实缺口。

目标日取上海今天之前最近已证明交易日；日历仅缺周末可放行，缺工作日证明仍拒绝，不猜日期。重新获取成功不保证日期或数值变化。PB同业中位数只比较同一完整快照、同口径合格样本；财务每次显式任务重新核查。

本人OAuth、真实watch/发行人原文核对、常用真机、性能及异机恢复仍待验收；曾有输入误点和间歇空白，不能把重跑成功当根因修复。下一项是[可靠性收口](docs/FLET_DESIGN.md#next-stage)，不预建AI或第二套前端。中文字体同源提供，许可见`assets/fonts/`；Flet仍可能尝试可选CDN，测试会阻断外部请求。

## 显式备份与隔离恢复

```bash
uv run python manage.py backup --source .local/demo --destination .local/backups/demo-001 --mode demo
uv run python manage.py restore --source .local/backups/demo-001 --destination .local/restores/demo-001 --mode demo
```

目标必须全新且与源分离；生产使用`--mode production`。CLI只读源库、一致备份后从副本库复制引用快照并校验，恢复未完成任务为interrupted，不联网、不启动服务、不替换正式目录。笔记/ack保留，任何失败副本都不认可；当前目录同步异常处理仍待补强，mode标记与哈希不能单独证明断电安全。本机副本不等于灾备，异机传输、定时维护、删除旧备份及正式替换另需授权。

## VPS部署

仅支持仓库现有Compose + Nginx布局（默认`/home/lin/nginx`），需Docker权限；提交Git不会部署。

- 从示例准备本机`.env`（Web的GitHub OAuth/数字ID白名单）和`.worker.env`（worker的TuShare Token），不要交叉分配或提交凭据。生产缺配置拒绝启动，不降级demo。
- 核对production工作区`data/`、只读tracker路径、日历证明范围；Compose只读挂载清单/日历/原库，不修改tracker。
- **明确授权后**运行`./deploy.sh`：预检与检查→构建重建Web→校验/重载Nginx→HTTPS检查→启动检查worker。失败非零退出，但不自动回滚已重建Web；勿在demo环境执行。
- 实际部署版本需核查；匿名页面/容器存活不等于本人登录、真实数据或真机通过。工作区、凭据、日志与备份不得经静态目录公开或提交Git。

## 原CLI保留

```bash
# 离线复看；旧对照文件须明确指定
uv run python screen.py --input output/CURRENT_RUN/snapshot.json --compare output/OLDER_RUN/snapshot.json
# 显式联网
uv run python screen.py --tracker-root ../a-stock-tracker --anchor 600900.SH --refresh
```

`.local/`、`data/`、`output/`及备份是本地资料，不是可以顺手清理的临时文件。
