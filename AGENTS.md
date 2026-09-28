# a-stock-screen 开发约束

先读[设计合同](docs/FLET_DESIGN.md)及§8对应任务/验收，再查源码与调用方。设计和计划只维护该文档，README写用法、导航只索引；不另建spec/plan/review副本或实施流水。

## 核心边界

- 单用户、Linux VPS、Flet动态Web兼容手机；只做同业发现、个人研究记录与资料变化。保留原CLI和peer-screen-v1资格/公式/并列/前三/历史结果，不做交易、账户、回测、调权、通知或AI平台。
- tracker完全只读：SQLite仅mode=ro，不改代码、WATCHLIST、数据库、cron或旧实验，不调用有副作用的CLI/get_db，不恢复Framework A。
- 仍只有三张业务表；事实JSON、个人状态SQLite。凭据、工作区、私人笔记/备份不进Git、日志、assets或浏览器存储。
- 只有用户显式提交的更新可联网取行情；OAuth网络单列。Web只提交/查询，独立worker执行，不能用page.run_task替代；事件循环不阻塞。

## 改动方式

- 先检查状态，不reset或覆盖无关改动；一次一个获准切片，不预建模块，不换框架/加依赖来回避问题。相邻仓库不是demo前提，使用本仓库Python 3.13环境和锁文件。
- 普通页面构建＋异步回调，以锁定Flet 1.0 API为准；不混用0.28。MCP仅本地开发，产品不依赖模型或公网MCP。
- 不弱化鉴权、generation/断线/退出保护、幂等请求、revision/updated_at冲突、已阅防倒退、快照路径/哈希或故障恢复。界面重排不得改变算法或持久状态语义。
- 生产OAuth保留官方state、数字ID白名单、actor撤销/到期；私人读写/异步呈现复核授权。ASGI保留lifespan、Host/WS Origin校验、关闭/upload；demo仅loopback/合成数据。生产缺配置拒绝，不降级demo；web/worker按角色验配置。
- SQLite参数绑定、显式短事务、schema/mode锁定；核验运行库WAL修复，新demo可显式DELETE。备份从副本库复制引用，恢复不执行旧任务，不把设计文档当迁移脚本。
- 未明确授权不提交、推送、部署、重启、迁移、删除/传输数据或改系统服务；一个动作不授权相邻动作。

## 验证与交付

```bash
make setup
make demo
make check
make test-mobile
```

代码改动先窄测再运行check与浏览器流程；保留原算法/安全反例。测试用独立临时库和合成fixture，默认禁真实数据接口、放行本地HTTP/WS；不清demo/生产数据。资源先安装，缺浏览器非零退出；真实点击/输入/保存，不以原生Flet测试、canvas、截图或skip算Web PASS。

真机/真实接口/生产部署分别记录；失败、未测及剩余风险如实报告，不用重跑绿灯抹去问题。检查diff、diff --check及状态。交付只报可用行为、文件、真实测试、未验证项和启动方式；没有新任务就停止。
