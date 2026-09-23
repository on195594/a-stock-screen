# a-stock-screen 开发约束

本文件用于本次Flet Web端工作台（兼容移动端浏览器）范围，替代旧版“只允许脚本/禁止工作区数据库/必须手动选比较文件”的限制。修改前读 `docs/FLET_DESIGN.md` 和当前任务对应的 `docs/CODEX_IMPLEMENTATION.md` 切片；它们是本仓库该功能的唯一维护设计，不要求相邻仓库存在才能开发demo。

## 产品与数据边界

- 单用户、Web端（兼容移动端浏览器）、Linux VPS、Flet动态Web。只做同业发现、个人研究记录和资料变化；不做交易、账户、回测、调权、模型、通知或定时选股。
- 复用 `screen.py` 的 `peer-screen-v1`。UI、个人暂停/已阅、watch更新不能改变算法资格、公式、前三或旧结果。
- 不恢复 Framework A。`a-stock-tracker` 只读（仅允许 SQLite `mode=ro`）；不改它的代码、WATCHLIST、旧实验、数据库或cron。不调用其有副作用的CLI/get_db。
- 原CLI保留：`--input`离线，`--refresh`显式联网。网页只有用户提交的更新任务可请求数据；登录OAuth网络与行情更新分开。
- 工作区只新增三张业务表；事实保存在JSON快照，个人状态保存在SQLite。不另建行情库、事件平台或内容寻址证据库。
- 凭据、`output/`、本地缓存和个人笔记不得提交。

## 开发方式

- 一次实施S1/S2/S3中的一个可用切片，不预建后续空模块，不规定PR数量，不创建新的spec/plan/review副本。
- 本仓库独立Python3.13环境和锁文件；不借用或升级tracker的生产venv。
- setup/demo/check在没有tracker、Token和生产数据时可运行；重复demo不清库，测试用独立临时库。fixture明确合成，production拒绝；锁库schema和workspace-mode不符拒绝。
- 当前工作区可能已前进；先检查，不reset、不覆盖无关改动，不自动部署或改系统服务。

## Flet与Codex

- 以锁定Flet1.0 API为准；MCP/CLI为可选开发工具，失败改查官方/安装签名，不阻断demo。不要混用0.28 API；先验证ASGI、异步事件、Web语义，再完成页面。
- 先普通页面构建函数＋异步回调，不加入额外响应式状态框架。核心函数不返回Flet控件。
- 事件循环不做同步网络、长sleep或长计算。页面只提交/查询任务；真正更新由独立worker执行，不能放进page.run_task。
- Page/Session/SharedPreferences不存永久研究记录。每页有自己的临时状态，不使用全局当前用户/可写连接；切页或退出后返回的异步结果不得重显私人视图。
- Flet MCP仅开发stdio，不随应用暴露公网；产品运行不依赖Codex或任何模型。

## 认证与持久化

- 生产GitHub OAuth用同标签页回跳＋服务端数字ID白名单；actor可撤销/到期。私人读写及任务先鉴权，提交/异步呈现前再查；匿名只见登录。保留官方state校验，ASGI透传lifespan、校验Host/WS Origin并关闭/upload。
- demo只允许loopback Host/Origin和合成数据，不信任转发头/读生产Token；生产缺web认证配置拒绝启动，不降为demo。web与worker按角色验配置，不互相要求对方secret。
- 不把Token/secret/授权码/私人笔记写入Git、日志、assets或浏览器存储。快照/备份不是静态文件。
- 首次加入revision=0，保存检查revision且重复加入不覆盖。只有“变化已阅”推进实际显示且该行可用的run；ack不得倒退，备注不推进。缺失新run与旧可用事实分别显示，不混成新完整资料。
- SQLite显式短事务；核验运行库WAL修复，不满足可显式用DELETE初始化新demo。request先查ID/别名再冻结范围和目标日；worker持进程锁。恢复验证job/intent/日期/范围再登记，partial不替换完整榜。
- 参数绑定SQL，schema初始化原子且幂等；URL仅校验后浏览器打开。快照相对路径与哈希校验，原始导入去重，replay/未知规则不冒充新观察；备份从备份库复制引用，恢复不自动执行旧queued/running任务。

## 验证与交付

本轮S1建立并维护：

```bash
make setup
make demo
make check
make test-mobile
```

在 `make check` 建立前，原脚本验证可直接在当前独立环境中执行：

```bash
uv run pytest test_screen.py -q
```

命令未建立时先实现，不能假装已执行。单测/服务测试使用临时SQLite与fixture，默认禁外部数据接口。保留原算法测试。

动态Web验收实际点击/输入/保存；不把原生flet test、canvas或skip当PASS。禁网测试放行本地HTTP/WS，资源安装先完成。test-mobile缺浏览器则非零；公网启用须验证所有者常用真机，其他平台未测如实记录。

完成反馈只包含：可用行为、改动文件、真实运行的测试及结果、仍未验证项和启动方法。没有新的用户任务，不继续扩建AI、交易或平台能力。
