# 个人投资研究工作台：Flet Web端设计（兼容移动端浏览器）

> 设计版本：1.0 · 2026-09-23
> 适用项目：`on195594/a-stock-screen`；核对基线 `9cfd9a2f09dea94f3d4cb11f0e7e7f726c2f0090`。
> 使用者：所有者本人；主要终端：Web端，兼容移动端浏览器；部署：一台 Linux VPS。
> 本文是待实施设计，不是已完成的软件或生产验收。配套执行顺序见 [CODEX_IMPLEMENTATION.md](CODEX_IMPLEMENTATION.md)。

## 渐进式设计导引（Progressive Disclosure）

本文档为完整架构与合同设计。为避免一次性加载过多信息，可按任务需要渐进式查阅对应模块：

| 层级 | 关注主题 | 关键章节 | 适用场景 |
|---|---|---|---|
| **第 1 层：定位与红线** | 产品定位、明确不做、现有约束关系 | [§1 决策与范围](#1-决策与范围) · [§8 完成标准与停止范围](#8-完成标准与停止范围) | 启动前对齐红线，防止过度设计 |
| **第 2 层：交互与线框** | Web端与移动端信息架构、两主页三二级页线框 | [§2 Web端交互与 UI（兼容移动端）](#2-web端交互与-ui兼容移动端) | S1 页面布局与导航构建 |
| **第 3 层：系统架构与持久化** | Web+Worker 进程模型、SQLite 3张表、快照导入 | [§3 架构和现有代码接入](#3-架构和现有代码接入) · [§4 存储合同](#4-存储合同) · [§5 更新与恢复](#5-更新数据含义和失败恢复) | S1 工作区与存储初始化 / S2 任务 |
| **第 4 层：认证与运维** | GitHub OAuth、Caddy 代理、系统服务 | [§6 认证、Web与移动端重连与云端部署](#6-认证web与移动端重连与云端部署) · [§7 Codex 与 AI 兼容](#7-codex-与-ai-兼容) | S1 鉴权接入 / S3 VPS 部署准备 |
| **第 5 层：底层参考合同** | SQLite 初始 DDL、核对依据标准 | [附录 A：最小 DDL](#附录-a最小-ddl设计合同实施时由初始化函数创建) · [附录 B：已核对依据](#附录-b已核对依据) | `workspace.py` 编码与严谨验收 |

---

## 1. 决策与范围

### 1.1 产品要完成的一件事

**Web端（兼容移动端浏览器）打开后，知道关注对象有什么变化、自己上次研究到哪里，并能继续作出研究判断。**

首页不是行情终端，也不是每日买入榜。保留同业发现；新增持久化的个人观察与移动界面。排名只安排研究顺序，不能显示为低估结论、买点、风险已排除或上涨概率。

| 决策 | 本轮确定的方案 |
|---|---|
| 界面 | Flet 动态 Web，Python 在 VPS 运行；不用静态 Pyodide，不打包原生 App |
| 交互 | 两个主入口：我的研究、同业发现；公司详情与任务页为二级页面 |
| 核心 | 复用 `screen.py`，保持 `peer-screen-v1` 的公式与范围；不复制算法 |
| 持久化 | 一个 SQLite 工作区、三张业务表；筛选事实仍保存为普通 JSON 快照 |
| 更新 | 本人点击后入队；一个独立串行 worker 执行；不依赖页面在线 |
| 认证 | Flet GitHub OAuth；服务端固定 GitHub 数字 ID 白名单；无注册和角色管理 |
| 部署 | Caddy HTTPS → 单个 Flet/ASGI 网页进程；systemd 管理网页和 worker |
| AI | Codex 开发、Flet MCP 查 API；产品不调用 Codex/LLM，不接交易工具 |

Flet 官方于 2026-09-15 发布 1.0；动态 Web 以 ASGI 运行。实现时以独立环境中实际安装、通过验证的 1.0 系列精确版本生成锁文件，不把框架发布声明当作本项目已通过Web端及移动端浏览器验证。[F1][F2]

### 1.2 明确不做

不做账户/持仓管理、自动交易、回测、自动调权、新闻抓取、分红或监管证据审批系统；不新增排行榜因子。没有 Django、React、Redis、Celery、向量库、模型总调度、MCP 公网服务或微服务。本轮也不做定时选股、推送、离线编辑同步、任意文件上传和私人文件公开下载。

后台 worker 只是执行**用户已经提交的任务**，不是自动发起投资研究。备份维护不等于定时选股。三张表的DDL可在S1一次初始化；不因此提前实现S2的worker或S3的比较界面。

### 1.3 与现有约束的准确关系

当前 AGENTS 要求只用 CLI、手动指定比较快照、不得新增数据库，并依赖相邻 tracker 环境。[R1][R2] 本轮在 **a-stock-screen** 内明确放宽这四项：允许 Flet 网页、兼容快照自动比较、个人状态库和独立开发环境。保留旧 CLI；`--input` 继续离线，`--refresh` 继续是 CLI 的联网授权。

网页上的“更新资料/查找同业”是新的显式联网授权入口。OAuth 登录所需网络与行情更新分开；打开首页、切页、保存备注和标记已阅不能触发行情请求。

**不修改 tracker 的代码、WATCHLIST、数据库、旧评分、旧 manifest 或 cron；不恢复 Framework A。**旧仓库规范仅在引用旧资产时适用；新功能以本仓库的本文和 AGENTS 为准，不要求 Codex 克隆第二个仓库才能开发演示。

## 2. Web端交互与 UI（兼容移动端）

### 2.1 信息结构

| 路由意图 | 页面 | 主要行为 |
|---|---|---|
| `/` | 我的研究 | 看未阅事实变化、数据异常、自己的研究清单 |
| `/discover` | 同业发现 | 选择参照，发起同业扫描，查看前三和参照位置 |
| `/company/<code>` | 公司详情 | 查看事实/来源，保存状态和一句理由，标记变化已阅 |
| `/jobs/<id>` | 更新进度 | 看已受理任务的真实阶段、结果或错误 |
| `/settings` | 设置 | 数据状态、登录身份、退出、必要的诊断摘要 |

这里的路由是应用行为合同；Flet 的路由/事件 API 名称必须在锁定版本中确认。首版用少量普通页面构建函数和异步事件处理，不另造前端状态框架。

底部导航仅“我的研究、同业发现”。设置在右上角；公司页有明确返回操作，并保留来源页、参照公司和滚动上下文。直接进入公司路由也必须先鉴权。

### 2.2 首次使用与日常使用

首次：登录 → 尚无研究记录 → 查看已导入快照或选择参照扫描 → 打开公司 → 加入研究/观察。服务器配置不由Web端/移动端用户反复填写。

日常：登录会话有效时打开即显示本地资料 → 看变化 → 保存判断/标记已阅 → 结束。点击更新才发起网络任务。没有变化时应明确写“本工具覆盖的字段暂无未阅变化”，不能写“公司没有风险”。

### 2.3 页面线框（均为合成示意，非真实股票或绩效）

**我的研究：**

```text
┌──────────────────────────────┐
│ 我的研究                 设置 │
│ 估值截至：09-22               │
│ 财务：各公司采用年度见详情     │
│ 1 家资料未完整更新             │
│                  [更新资料]   │
├──────────────────────────────┤
│ 需要复看 · 2                  │
│                              │
│ 示例公司甲             研究中 │
│ 采用的年报数据有变化           │
│ 上次：核查盈利改善能否持续     │
│ [查看变化]                    │
│                              │
│ 示例公司乙               观察 │
│ 本次财务更新失败               │
│ 仍展示上次资料                 │
│ [查看资料]                    │
├──────────────────────────────┤
│ 我的清单                      │
│ 研究中 · 观察                  │
│ [显示暂不研究]                │
├──────────────────────────────┤
│ 我的研究         同业发现     │
└──────────────────────────────┘
```

日期不一致时显示“各公司数据日不同”，由卡片给出日期；不能用一个最新日期替所有公司背书。卡片优先级：数据错误单独提示；事实未阅变化；研究中；观察。已暂停的公司不在默认提醒区，但可随时恢复。

**同业发现：**

```text
参照公司：[名称/代码选择器]
[查找同业]

最近一次结果：日期 / 行业 / 范围限制
参与与排除：枚举数 / 截取数 / 合格数 / 原因

示例公司丙       同业研究次序 1
ROE 均值 ……%    PB ……
与参照：指标差异，注明采用年度
我的状态：尚未研究
[查看依据] [加入观察]

其余前三……
参照公司：即使未进前三，仍单列
[完整比较] [缺失与排除原因]
```

选择器首版只展示现有 CLI 支持的原关注参照集合；金融或无法确定行业的对象明确标注不适用。不能做一个假装支持任意股票的搜索框。池外公司可以加入观察，不需要加入 tracker 的 WATCHLIST。

前三保持算法原结果；个人标记“暂不研究”不删除其算法名次，也不把第四名伪装成第三名。完整排名在二级区域以紧凑卡片/可换行列表呈现，不强迫移动端浏览器横向滚动宽表。

**公司详情：**

```text
[返回]  示例公司甲
状态：研究中       资料：部分未更新

本次变化
  与你上次已阅资料比较的事实
  若是成员/规则/缺失变化，分别说明

关键比较
  三年 ROE 逐项、均值、PB 及各自日期
  最近一次同业名次、参照、扫描日期
  （单独更新资料不生成新的同业名次）

我的研究
状态：[研究中 / 观察 / 暂不研究]
理由：[一句话，可空]
下一步：[可空]
原笔记链接：[可空]
[保存我的记录]
[这次变化已阅]

[展开来源、年度与数据缺口]
```

“保存记录”和“已阅”严格分开。打开详情不自动推进已阅基准；修改备注不自动将变化标成已阅。退出有未保存编辑时提示保存/放弃/继续编辑。未提交草稿只保留在当前页面内；浏览器强制回收可能丢失草稿，不能显示“已保存”。本轮不做离线草稿同步。

**任务页：**受理后显示“服务器已接收，可稍后回来”；进度用真实阶段及 `已处理 7/18`，不用虚假时间估算。完成后进入结果；失败时给出“哪些未更新、上一份资料日期、重试按钮”。顶部小任务提示使离开任务页后仍能找到未完成任务。

### 2.4 状态和视觉约定

三条独立状态轴：数据（可比较/部分未更新/失败）；个人（研究中/观察/暂不研究）；阅读（有未阅变化/已阅）。任何绿色提示都不代表投资安全。

建议首版：浅底、深色正文、蓝色主操作；错误/警告同时使用文字和图标，不只用颜色。16px 正文，12–14px 辅助信息，48px 主按钮，页面边距16px，卡片间距12px；360/390/430 CSS 像素验证，最大正文宽度640px。以上为设计值，不是框架默认或实测结果。

底部导航和保存按钮避让系统安全区与软键盘；中文输入过程中不重建整个表单；轮询任务不能清空输入、抢焦点或重置滚动。按钮、图标和文本框提供可访问的名称。Flet Web 的语义树可能需要显式启用，浏览器定位策略必须实际验证。[F5]

冷启动显示应用名称和“正在连接”，不能先闪现私人数据或旧假成功。断线后尽快显示连接提示并阻止新操作；浏览器被系统冻结时不能保证立即更新提示。重连后先校验身份，再读 SQLite；保存响应不明确时提示“保存状态待确认”并重读，不把本地点击算成成功，不自动重放写操作。旧的页面查询在切页/退出后返回时丢弃结果，不能重建已关闭的私人视图。

## 3. 架构和现有代码接入

### 3.1 单机、同一代码、两个进程

```text
Web端（兼容移动端浏览器） ──HTTPS/WSS── Caddy
                             │ 只代理 127.0.0.1
                             ▼
                    Flet/ASGI 网页进程
                     │ 私人状态/任务提交
                     ▼
               workspace.sqlite3
                     ▲           │
          普通串行 worker        │ run 引用
             │                   ▼
             ├── 只读 tracker    JSON 快照/报告
             └── 显式 TuShare
```

网页进程不执行慢财务抓取。worker 不引用 Flet Page，也不向浏览器推业务状态。UI 只轮询 SQLite 中的任务摘要；断线/锁屏不影响已受理任务。网页进程固定一个 ASGI worker，避免多进程 Flet 会话分配问题；数据 worker 固定一个。

Flet 1.0 的迁移说明强调事件循环内阻塞调用会冻结 UI；有些动态网页示例仍含旧线程说明。本设计不依赖这一差异：UI 异步事件只作短操作，必要的阻塞本地 I/O 用 `asyncio.to_thread`，完整网络任务交给独立进程。[F1][F3]

### 3.2 文件职责：按交付需要增加，不预建空模块

```text
a-stock-screen/
├── screen.py                  保留算法与原 CLI；做小范围公共函数提取
├── app.py                     Flet 启动、路由、页面和事件
├── auth.py                    GitHub OAuth 与 owner 校验
├── services.py                用例：首页、详情、保存、已阅、任务提交
├── workspace.py               三表、短事务、快照索引与校验
├── worker.py                  顺序执行任务与中断恢复
├── manage.py                  init/import/demo/backup 等受控本地维护入口
├── pyproject.toml / uv.lock    独立、锁定的环境
├── Makefile
├── assets/                    仅公开图标/主题资源
├── tests/fixtures/             合成数据；原 test_screen.py 保留
└── deploy/                    Caddy 与两个 systemd unit 示例
```

不增加通用 repository、插件、工厂、事件总线或完整 API 层。`services.py` 返回普通 dict/类型化对象，不返回 Flet 控件。现有纯计算不依赖 UI/认证/数据库。网页和worker共享的配置必须由各自启动入口显式读取；导入模块不启动服务、不创建数据库、不读取 `.env` 或联网。

| 现有函数 | 复用或小改动 |
|---|---|
| `read_watchlist` / `load_reference` | 生产继续安全解析原集合；demo 从 fixture 读取，不执行旧模块 |
| `load_peers` / `rank_peers` | 保留范围和公式；新状态不得影响其输入资格或排名 |
| `select_annual_roes` | 保留逐年与版本检查；新适配器补来源/日期校验，不让未知来源通过，不改变评分公式 |
| `fetch_financials` / `load_inputs` | 提取按证券获取事实的普通函数；补核查时间和进度回调；不重构 tracker |
| `compare_snapshots` | 同业扫描比较继续复用；个人事实比较另按已阅基准，不能跨上下文比较名次 |
| `render_report` | peer 类型导出复用；watch 只显示事实，不调用排名渲染冒充新排名 |
| `save_output` | 原 CLI 兼容；网页 worker 使用确定 job_id 的独立目录后再登记数据库；不直接调用会另建随机目录的旧发布函数 |

以上映射来自当前源码；尤其 `load_inputs` 目前在本地三年选择成功后就不再请求财务，并在循环前记录 `screened_at`，需在新调用链修正核查语义和完成时刻。[R3]

### 3.3 最小业务接口（项目拟实现合同，非 Flet API）

```text
get_home(actor) -> home_context
get_company_context(actor, code, include_personal_notes=False) -> context
save_watch(actor, code, source_run_id, fields, expected_revision) -> watch_item
mark_seen(actor, code, displayed_run_id, expected_revision) -> watch_item
request_peer_update(actor, anchor_code, request_id) -> job
request_watch_update(actor, request_id) -> job
get_job(actor, job_id) -> public_job_status
```

`actor` 来自当前 Page 的服务端授权上下文，含owner、有效期和撤销标记，不接受浏览器传来的user_id或布尔值自证。服务入口先鉴权，写事务前再确认有效；退出会使旧回调失效。worker走内部函数，不伪造网页actor。返回值只包含允许展示的字段，不返回Token、绝对路径、SQL、完整异常链；公司详情需要本人备注时由已授权UI显式传 `include_personal_notes=True`。

`fields` 只接收status/reason/next_check/note_url；reason与next_check各至多1000字符，URL至多2048字符且仅允许无用户名密码的https地址，均按纯文本呈现。code需正规化并已存在于可查看的run；不能借表单新建任意外部请求。调用方提供的name/owner/ack字段不直接落库。

新增关注使用 `expected_revision=0`，已有项使用页面读取的revision。重复加入返回已有项，不覆盖理由；保存与已阅合同见4.4。参数或版本冲突给出可理解提示，不能静默截断。

`request_id` 为一次点击意图的随机UUID。重复点击在收到明确结果前复用同ID；服务先查原ID/别名，再解析当前关注清单。用户意图与入队时冻结的实际范围分开保存，合同见5.4。成功提交任务事务之前不能显示“已受理”。

## 4. 存储合同

### 4.1 分清事实、个人判断和页面状态

| 内容 | 唯一归属 |
|---|---|
| 旧行情/财务库 | tracker；新程序只读，不更改 schema 或数据 |
| 当次实际指标、来源、范围、排名 | run 的普通 JSON 快照 |
| 个人状态、理由、下一步、已阅基准 | `workspace.sqlite3` |
| 任务及快照路径索引 | 同一工作区 SQLite |
| 当前页面、未提交草稿、临时进度显示 | 每个 Flet Page 自己的状态，不使用全局共享“当前用户” |
| Token、OAuth secret | VPS 配置；不入库、不入快照、不入浏览器存储或 Git |

不把 Flet Session 当作业务持久化；不建立第二套行情/财务缓存数据库。新来源核查结果留在下一份普通快照中。

### 4.2 三张业务表

| 表 | 必要字段与语义 |
|---|---|
| `screen_runs` | `run_id`、peer/watch、参照/规则、实际资料形成时刻、估值日、完整性、相对快照路径与文件哈希、登记时间 |
| `watch_items` | code、name、原参照、research/observe/paused、reason、next_check、note_url、加入时 run、已阅 run/时间、revision |
| `update_jobs` | job_id、request_id及合并请求别名、peer/watch、固定 payload、active 去重键、状态、进度、时间、错误摘要、result_run_id |

具体最小 DDL 在附录 A。UUID、日期和 JSON 内容由 Python 校验；不要指望数据库 CHECK 自动证明所有金融含义。个人状态字段不复制到原始筛选结果。

### 4.3 快照、导入与索引

网页结果存放 `STATE_DIR/snapshots/<job_id>/snapshot.json`，`run_id=job_id`。peer继续保存旧格式的范围、逐行输入和 `peer-screen-v1` 结果；watch明确为 `kind=watch`、`anchor=null`、`rule=null`、`results=null`，不传给peer排名器。新增元数据只由网页适配器处理，不让旧CLI误读watch快照；旧CLI行为不需要为网页格式扩展。

每份新快照的 `workspace_meta` 至少保存 `kind/run_id/job_id/intent_hash/health/expected_codes`；watch的expected_codes是冻结关注集合，peer的是本次截取集合。捕获时间采用带时区UTC；估值日采用市场日期。`captured_at` 表示本次资料整理结束，不表示各字段都刚取得；逐字段日期仍独立展示。

新行复用既有 `code/pb/valuation_date/annual_roes/roe_mean/...`，另保存 `valuation_status`、`financial_status`、`financial_checked_at`、`eligibility_reasons` 和 `facts_usable`。两种status使用 `ok/reused/missing/conflict/failed/not_requested`，含义由5.3固定；`facts_usable`由校验计算，不能相信导入文件自报。watch也读取非正PB、非正ROE或已知ST公司的可得事实，只是不排名；不要复用旧“已排除就不读年报”的短路来吞掉关注资料。

已有peer快照仅通过**显式本地导入**进入工作区：读取一次原始字节并校验，复制到 `snapshots/imports/<sha256>/snapshot.json`，不改原文件；相同字节重复导入返回原run。索引时间可为现在，captured_at取原screened_at并转为规范UTC。不能把导入时刻当作新观察。复制时也使用临时文件/原子完成；缺文件、哈希不符或越界路径均拒绝读取，不自动修复或重抓。

导入先检查唯一代码、完整枚举和截取信息、规则及cap/top_n/550天限制、同日PB、逐年数值/版本/来源，再用现有纯函数复算并核对保存结果。缺字段、未知规则或带 `replayed_from` 的副本可登记为unverified，只供历史查看，不作为当前事实、排名或初始已阅基准。含fixture标识或fixture来源的输入只可进入demo工作区。连最小参照身份、合法screened_at或可解析rows都缺失时拒绝导入；未知规则但基本外壳完整才可登记unverified。导入时间解析为真实时间，拒绝无时区或未来screened_at，不靠字典顺序或文件mtime选“最新”。导入run_id由内容哈希稳定派生，导入不关联update_job。

查找规则：同一参照的当前榜只选规则兼容、health=complete的原始peer运行，先按valuation_date再按captured_at降序；同业比较取此前一份兼容运行。个人事实只读包含该code的已登记原始peer/watch行，以实际capture时间排序；较新run若估值日倒退、行不可用或文件损坏，显示异常并保留上一份可用行，不能悄悄替代。相同时刻用run_id稳定排序，不以indexed_at覆盖旧判断。首次使用尚无历史基准只提示“尚无对照”。

SQLite保存相对路径和完整文件SHA-256（仅用于误改检查），不建立内容寻址证据平台。所有新路径必须解析在STATE_DIR之内；STATE_DIR不能落在代码assets、tracker或其子目录。禁止符号链接/路径穿越；网页不接收任意导入路径。数据库索引是找文件的入口，事实仍来自校验后的JSON。

### 4.4 个人记录的并发与已阅

保存使用短事务及 `WHERE code=? AND revision=?`，成功后revision加一。先比较提交内容与当前记录：完全一致的重试返回当前值；内容不同且revision过期则报冲突，保留页面输入。源run必须真实包含该code，首次added_run_id之后不修改；name从源事实取得。个人状态绝不写入历史快照。

加入关注时，只有页面实际展示了完整事实才将其设为初始ack；从列表直接点击“加入观察”尚未看逐项资料时ack为空。`mark_seen`中的displayed_run_id由服务端页面呈现时捕获，不在点击时重新查latest。服务校验证券存在、该行facts_usable、当前授权和revision；旧标签页不能把ack倒退到更早的资料。同一run重复已阅返回当前值，不产生假更新。已阅不等于风险核查完成。

`get_company_context`区分 `latest_attempt_job_id`、`latest_attempt_run_id`、`usable_fact_run_id`、`ack_run_id` 和 `last_peer_run_id`。失败job可能没有run；按冻结watch代码或已知peer范围关联错误，未形成peer范围的全局失败只显示在相应参照/任务上，不能假定每家公司都发生了变化。新尝试失败时，缺口来自新尝试，数值来自明确注明旧日期的usable行；不能拼成“本次完整”。这种情况下“本次变化已阅”禁用，只允许明确查看旧资料。没有ack时显示“首次待阅”，不能显示零变化；没有任何可用事实时只显示缺口。

未阅比较针对PB及其数据日期、采用年报/版本和逐年ROE；仅acquired_at/checked_at或文件生成时间改变不算新的财务事实。PB小幅数值变化仍在明细可见，但不必占首页；年报/修订变化优先展示。来源/范围改变要单独解释。最新值变回已阅值时，本轮只报告净变化，不承诺逐次事件通知；全部历史run仍可追溯，不另建事件系统。

“同业发现”的前三进出只比较两次兼容peer扫描；watch更新没有新名次，个人页显示最近peer名次及其旧日期。暂停只影响默认呈现，恢复保留备注；掉出前三、前50或以后退市都不能删除个人记录。未知/不再上市的当前状态显示待核查，不拿缺行情解释为经营恶化。

### 4.5 SQLite 运行方式

独立本地磁盘；短连接，不跨线程共享。显式事务统一采用 `sqlite3.connect(..., isolation_level=None)`，由BEGIN/COMMIT/ROLLBACK控制；不要把Python隐式事务与显式BEGIN混用。每个连接设置foreign_keys=ON、synchronous=FULL、busy_timeout=5000；WAL仅在初始化时设置并核验。超时显示稍后重试，不删除锁文件或无限等待。[S1][S2]

本轮不做ORM。`manage.py init` 是唯一建表入口：新库在事务内建立完整DDL并设user_version=1；重复运行核验schema和模式后无操作。网页/worker只连接已初始化库，版本或结构不符则停止，不能用CREATE IF NOT EXISTS掩盖半建库。不要在导入app时执行初始化。`.workspace-mode`记录demo/production并在每次启动和本地维护时核对，两个模式不能共用状态目录。

S1检查 `sqlite3.sqlite_version`，不能仅凭Python版本推断SQLite补丁状态。启用WAL需使用已包含官方WAL-reset修复的运行库（3.51.3+，或有明确修复依据的回移植，如3.44.6/3.50.7）；未满足时先明确报错或在**新建工作区初始化时**显式选择DELETE日志模式，不能静默更改生产库。此问题只影响运行环境选择，不要求迁移数据库或新增依赖平台。[S1]

## 5. 更新、数据含义和失败恢复

### 5.1 两种更新，不混成一条重排名链

**peer：查找同业。**参照来自当前允许集合；沿用参照＋最多49家同业、同日总市值截取、连续三年正均值与正PB、平均名次、前三规则。不添加因子或对照实验。

**watch：更新关注资料。**入队时固定“研究中/观察”的代码列表；同一请求去重证券，只取这些公司的事实，不扩大同行池、不跨行业排名。首版单任务最多50家公司；超出给出明确提示，不静默截断。空清单不建任务。暂停对象不默认更新，可恢复后更新。确认公司当前身份时使用基础信息；不再上市/行业改变的对象保留个人记录并标注，不重新套用原行业排名。

来源复用：优先复用已知完整、最近成功核查过的原记录或快照；不以均值字段反推出原记录。不具备来源或有效日期时标明缺口；不得用旧分数/LLM补数。

### 5.2 默认数据日和财务核查

为降低Web与移动端操作复杂度，首版**默认使用小于今天（Asia/Shanghai）的最近已证明交易日**；任务提交时只读已有日历，证明其覆盖至上一自然日并验证来源/范围后，将具体target_date冻结进payload。若日历不足，拒绝本次提交并说明旧资料仍可用，不联网猜日期。任务排队跨日也不改target_date；页面明确写“默认上一已完成交易日”，不声称盘中/当日实时。旧 CLI 的显式 `--date` 保留为高级功能，网页首版不增加日期选择器。日历不能证明目标日期时，不按工作日猜测，任务失败并继续展示旧结果。已有日历过期或来源范围不足必须提示，不把陈旧列表的最大日期标成“最新”。

估值请求须实际返回统一目标日。没有数据不在后台逐日猜测改日；用户看到的是失败与旧资料。三年年报期末、公告日期、提供方版本和数值分别保存；不把缓存更新时间当公告日期。

财务采用一个简单初值：**成功核查来源距今不超过7个自然日可复用；未知或超过7天则显式更新任务重新核查。**候选来源为原始peer/watch运行或具备完整成功请求证据的tracker记录；不得从混合缓存反推出“最新”。financial_checked_at是接口已成功返回、证券和覆盖检查完成的时间；同值返回也推进核查时间，缺失/冲突/失败不推进。与它分开保存各年报来源/公告/版本/原始取得时间，重复核查不改首次来源时间。旧缓存仅有acquired_at不能自动升级成“已核查最新”。

适配器核对返回ts_code等于请求代码、行情日期等于target_date、证券无重复冲突。财务窗口需能解释最近三个连续年度；更新标志或来源冲突、截断、接口失败不能任意取最后一行。按当前资料选择仍不是历史PIT回测。金融/未知行业排除与peer-screen-v1一致，不放松范围以凑候选。

最终 `screened_at/captured_at` 在本次所需读取结束后记录；所有取得时间不得晚于它。运行时间用UTC带时区保存；交易日/公告可用日期判断用Asia/Shanghai，传给旧日期选择函数前先转换时区，不能截取UTC字符串的前10位替代中国市场日期。七天和上一交易日是透明运行约定，不是投资有效性参数。

### 5.3 不完整不等于公司变差

字段获取与筛选资格分别判定：

| 情形 | 获取/事实语义 | 排名与运行语义 |
|---|---|---|
| 有本次合法返回或7天内经核查记录 | ok/reused；保留真实字段日期 | 按旧规则判断资格 |
| 非正均值、合法非正PB、已知ST | 已知业务事实，不是网络故障 | 排除出peer候选；watch仍展示可取得事实 |
| 缺年份、来源未知、版本冲突 | missing/conflict | 不参加本次排名，不用零补数 |
| 接口/权限/超时失败 | failed | 本次相应字段为null并列明错误，旧值另按旧run展示 |
| 已知资格排除后不需要的额外字段 | not_requested，注明原因 | 不称接口失败，也不称完整公司资料 |

`facts_usable`只在该行PB和连续三年ROE数值、日期、来源均通过核验时为true；数值可以非正，ST也不必为false。它表示能对照这些事实，不表示候选合格。缺一项则false，已阅不能把不完整的新run变成完整基准。

`health=complete`需范围证据完整，且每家公司要么取得必须字段，要么由已知事实明确排除；不能把无法取得/来源未知/多版本冲突也算“可解释排除”来宣布complete。缺必需字段或局部请求失败是partial。unverified只用于旧格式、未知规则等历史查看，不能进入当前事实或榜单。

partial可以保存诊断，但不替换上一份complete同业榜、不宣布正式前三进出；若纯函数产生临时排序，UI明确称“部分样本诊断”，不能放进正常发现卡。其他公司失败不阻止查看本公司可用新事实。无任何完整榜时明确展示诊断而非假成功。

job的succeeded只表示合法结果已登记；结果partial时UI显示“完成，部分资料未更新”，不显示“全部成功”。无法证明范围/目标日、协议校验失败或所有请求均因技术错误失败时job=failed，不发布当前榜；全部公司被有效业务条件排除则可以是complete的零候选，不混为技术失败。

### 5.4 任务去重和执行

请求payload是固定普通JSON，包含 `intent`（peer+anchor，或watch更新意图）、`codes`（watch冻结清单）、`target_date`、规则/范围参数和 `intent_hash`。不得保存Token或客户端文件路径。所有代码规范化去重；执行日期及数据访问计划在队列中可查看，但用户不能借payload添加任意接口。

- 在同一短 `BEGIN IMMEDIATE` 中先查request_id及别名；相同ID、相同用户意图返回旧job，不因关注清单已变重新计算范围；同ID但意图不同报冲突。只有新ID才读取当前清单和日历、冻结范围。它是点击意图幂等，不是把每次重新打开页面也当作同一意图。
- 新ID的active范围键由kind、冻结代码或anchor、target_date及规则参数规范化后取哈希。已有queued/running同键job时返回它，并把新ID记入该行request_aliases_json。别名查询、全局重复检查和合并都在同一写事务中；重试别名即使job已终结仍返回原job。部分唯一索引只保证active范围唯一，别名唯一仍需服务函数检查。
- 服务端Page在同一次提交响应未明确前保留同一request_id；新登录或Page丢失后不暗中重发，先展示已受理任务供查看，用户明确再次更新才产生新ID。终态失败的“重试”也是新ID。最多允许3个queued任务；重复请求先返回原job，不被容量限制误拒绝。
- worker持有STATE_DIR下单实例文件锁直到进程退出，不用锁文件“存在”判断存活，也不删除仍被持有的锁。领取最早queued任务，事务提交后才联网。令牌/配置不全的worker拒绝启动，不取任务后逐个消耗成失败。
- 一个worker顺序处理；单次接口超时30秒，瞬时网络错误最多再试一次，权限/格式错误不重试。总体任务预算默认30分钟，使用单调时钟且每次请求受剩余预算约束；到期留诊断并终结，不无限占running。限流错误依提供方错误明确报告，不能为赶进度放大并发。
- SIGTERM时停止领取新任务；当前请求在有限超时内结束，若未发布则标interrupted退出。强制终止由5.5启动恢复接管。failed/interrupted不自动重试；未执行的用户queued任务可在重启后继续。
- processed/total表示实际已处理对象；updated_at随进度刷新。UI只在当前可见且连接的任务页每3秒查摘要；退出、切页、断线取消本页轮询。任务列表仍能从SQLite找回，长时间无进度写“进度暂未更新”，不捏造worker仍健康。

### 5.5 发布顺序和进程崩溃

1. worker在已持独占锁的条件下，写 `snapshots/<job_id>/` 内临时文件，JSON禁止NaN；完成结构、job/intent/证券范围/目标日和结果核验后flush/fsync文件，原子改名并fsync父目录。最终文件已存在时校验并复用或报冲突，绝不以replace覆盖它。临时文件不能被UI当成结果。
2. 一个短数据库事务登记 `run_id=job_id`、哈希和health，同时把job置succeeded并关联result_run_id。事务失败回滚；UI只读已登记run。重复登记检查身份/哈希一致后无操作，不重复建run。
3. 原peer Markdown按需导出；失败不使已保存事实消失，但界面不能声称报告已生成。watch不调用peer报告器。schema/规则不受支持的输入拒绝发布为当前结果。
4. worker重启取得独占锁后，只恢复旧running任务：有合法最终文件且其job、intent_hash、类型、范围和日期全部匹配则补登记；无最终文件则标interrupted；畸形/错job文件则标failed并保留诊断，不覆盖不重新联网。恢复允许partial结果，但UI仍按partial展示。已经succeeded的job也必须指向合法run和文件，丢失时报存储错误而非重算。

这只覆盖当前单机的可恢复写入顺序，不引入分布式事务。测试在临时目录注入“临时写失败/文件完成后退出/DB提交后重跑”即可，不在生产做断电试验。

## 6. 认证、Web与移动端重连与云端部署

### 6.1 默认认证策略与Web/移动端回跳

使用Flet官方GitHub OAuth；只请求读取身份所需最小scope，禁止repo/public_repo等仓库权限。只有on_login成功、服务端取得的 `page.auth.user.id` 规范化为数字字符串并匹配GITHUB_ALLOWED_USER_ID后，才创建owner授权上下文；OAuth成功但ID不符仍清空授权并拒绝。[F4]

Web端与移动端浏览器首选**同标签页授权**，采用锁定版本支持的 `on_open_authorization_url`＋`redirect_to_page=True`，UrlLauncher使用`_self`；不要默认弹窗，也不要把异步launch调用丢成未await协程。回调固定为PUBLIC_BASE_URL的 `/oauth_callback`，不从Host或前端return_url拼接。登录前只在服务端保存经路由白名单校验的返回意图。参数名称已有官方依据，但await/事件签名仍须对安装版核验。[F4]

官方OAuth state与原Flet会话绑定；回调失败、超时或服务重启导致原会话丢失时，显示登录失败/重新登录，不靠callback查询参数重建owner。设置FLET_OAUTH_STATE_TIMEOUT=600、FLET_SESSION_TIMEOUT=3600作为明确初值；后者用于框架会话保留，不等于业务授权时长。需验证正常移动端浏览器授权跳出返回后会话仍能恢复。[F8]

首版不做密码/注册/JWT，不向客户端或数据库保存长期OAuth Token。业务授权自本次成功登录起最多8小时，由服务端单调时钟检查；框架会话先失效时更早重登。短时断线可复用框架仍存活且已验证的会话，重连本身不能重新开始8小时倒计时。新Page、刷新后无法恢复身份或服务重启后重新OAuth；记录和已受理任务不受影响。

这是一项明确的易用性取舍，不承诺刷新/新标签页一定免登录。官方Session Storage不是永久存储；不得为追求演示顺畅，把`is_logged_in`或授权Token写入SharedPreferences。[F9]

### 6.2 服务端授权边界

匿名仅能加载公开Flet外壳与登录控件；个人清单、快照、备注、任务摘要和写操作均通过owner校验。服务内部检查和UI检查都不可省略；worker入口不是网页调用接口。禁止全局共享当前Page/actor，也不把Flet会话ID本身当作身份。

每页用一个可撤销的服务端授权上下文和页面generation。退出/过期先撤销actor、取消轮询，再清空私人控件和调用官方logout。授权失效后的旧异步读取即使完成，也不能再次渲染或推进写入；本地I/O返回后重查授权和generation。所有者在有效授权下已经提交并开始提交的事务可以完成，退出不能回滚历史动作；未提交任务不得凭旧回调新建。会话过期需要定时清屏，不依赖用户下一次点击；浏览器离线/冻结时不承诺远程擦掉已显示内容。

只在WebSocket握手校验Origin与PUBLIC_BASE_URL的scheme/host/port严格匹配，缺失或null Origin拒绝；HTTP做Host校验，但OAuth回调GET不可被“必须携带本站Origin”的错误规则挡住。拒绝未知代理头，生产只信任localhost反向代理；Host/Origin中间件需同时处理http/websocket并透传lifespan，不能仅写HTTP装饰器。OAuth state校验交给官方流程，不跳过、不输出授权码。[F2][F4]

原Flet运行时可能注册 `/upload`，本轮在ASGI层明确关闭该路径的所有方法，不配置upload_dir，不把“不放上传按钮”当作关闭上传。[F2][F8] assets仅公开图标/主题，STATE_DIR及tracker目录不能挂静态；也不提供私人快照下载路由。用户文本按纯文本处理，外部https链接只在浏览器打开，不服务端请求，拒绝javascript/data/file及带凭据URL。

应用、Uvicorn和Caddy都不得记录OAuth回调查询串、access/refresh token、私人正文或SQL载荷。关闭默认access log不足以防止异常链泄露；网页错误只返回短代码和脱敏摘要，日志也做脱敏，不打印 `page.auth`、原始提供方异常链或配置全集。退出只退出当前应用会话，不声称自动退出其他设备或撤销GitHub账号登录。

### 6.3 部署形态

建议路径：

```text
/opt/a-stock-screen/current/       固定已验证提交及专用 .venv
/var/lib/a-stock-screen/           workspace.sqlite3 / snapshots / worker.lock
/etc/a-stock-screen/web.env        OAuth配置，不含TuShare Token
/etc/a-stock-screen/worker.env     TuShare配置；不含OAuth secret
```

两个独立EnvironmentFile可用同一低权限服务账户，这只是避免误传secret，不声称提供OS级凭据隔离；确有隔离需求再拆账户。文件由root保管（如0600），systemd读取后只注入对应进程。代码/venv只读，运行目录可写，Umask=0077；生产不用--reload。tracker 路径只授予读取所需权限，绝不为方便 chmod 整个主目录。若原库以WAL运行，需验证只读访问所需sidecar权限，不能用不真实的immutable=1绕过正在变化的库。无权限时，原始财务复用属于可选路径，应在显式更新中改用既定TuShare来源或显示缺口；不chmod旧目录、不要求重构tracker。原关注字面量/日历则由部署者提供可读路径，缺它们只阻止相关更新，不阻止查看已有工作区。

Caddy 代理到 localhost；保留 HTTPS/WSS 与 OAuth 回调，不把 Flet 端口直接开放公网。Caddy 原生支持 WebSocket 代理，实际域名配置要验证握手和重连。[D1] 网页使用一个 Uvicorn 进程；worker 用一个 `python worker.py`。具体Flet ASGI导出入口按锁定版本的示例验证，ASGI包装器必须透传startup/shutdown以保证OAuth和会话正常生命周期。[F2]

Flet动态Web的引擎/字体资源需要可达，不能“Python不联网”就宣称浏览器离线可用。首版使用FLET_WEB_NO_CDN=true，并在setup阶段准备和验证所需包内Web资源；浏览器测试拒绝外部行情/OAuth请求，只允许本地服务及已经显式准备的公开资源。缺资源应修复安装或说明未验证，不把空白canvas当应用成功。部署需实际核查中文字体以及Web端与移动端浏览器首屏。[F8]

仅生成 systemd/Caddy 示例和检查命令；Codex不安装系统服务、不覆盖现有Caddy、不打开防火墙、不读取生产secret，除非另获明确部署授权。

### 6.4 配置清单（均为拟实现配置）

| 配置 | demo | production |
|---|---|---|
| APP_MODE | demo，仅合成数据 | production，显式指定，无自动回退 |
| STATE_DIR | 被忽略的.local/demo，模式标记一致 | 已初始化的独立持久化目录 |
| PUBLIC_BASE_URL | 明确的localhost/127.0.0.1 HTTP地址 | HTTPS域名，不含路径/查询/凭据 |
| TRACKER_ROOT | 不存在也可运行 | 更新时使用只读参照/日历/可选原始库 |
| GITHUB_CLIENT_ID/SECRET/ALLOWED_USER_ID | 不使用 | 仅web必填；ID由所有者确认 |
| TUSHARE_TOKEN | 不读取 | 仅worker必填，不回退读取tracker/.env |
| FLET_OAUTH_STATE_TIMEOUT / FLET_SESSION_TIMEOUT | 框架默认或固定测试值 | 600 / 3600秒；与业务8小时分开 |
| FLET_WEB_NO_CDN | true | true；准备所需公共Web资源 |

web/worker/本地维护按角色校验配置，不要求worker具有OAuth secret，也不要求只读web具有TuShare Token。启动检查在显式lifespan/main执行；测试导入模块不读取真实环境或生产路径。

`make demo`只绑定loopback且不信任转发头，只接受loopback Host/Origin；禁止通过公网代理暴露demo。模式选择只能来自启动配置，不从URL/header切换。demo忽略生产Token且仅允许fixture provider和demo工作区；production拒绝fixture标识和demo目录。生产缺认证/工作区配置拒绝启动；缺tracker读取权限显示更新不可用，不以无法更新为由丢失已有私人记录。后台缺Token则拒绝启动worker。

### 6.5 备份、升级与恢复

使用sqlite3.Connection.backup()取得一致性备份，随后从**备份库**枚举引用并复制不可变快照与.workspace-mode；不得在复制阶段重新查仍更新中的原库。核验每个文件哈希和外键，缺文件就宣布备份失败；不能只复制WAL模式下的主DB。个人规模保留最近7份完整本地备份，VPS外加密备份由现有运维手段完成，不自建存储服务。[S2]

恢复在全新临时STATE_DIR、禁网且web/worker未启动时进行。核对schema、模式和引用后，将备份中的queued/running标成interrupted，不能让恢复演练重新联网执行旧任务；正式恢复也须由用户重新提交未完成更新。复制到正式位置前停应用进程，禁止覆盖正在写入的工作区。原库及备份本身不修改。

部署固定提交，不在服务目录由Codex边改边运行。首版schema固定；后续升级涉及schema时先备份，并明确兼容版本。回滚应用不会回滚tracker，也不把整个数据目录恢复为旧版本来掩盖新资料；先停止新任务、保留工作区和快照。

## 7. Codex 与 AI 兼容

### 7.1 单仓开发闭合

只克隆a-stock-screen即可：独立 Python3.13 环境、锁文件、合成快照、演示状态、无Token测试。Codex在安装阶段获取依赖；实现与测试默认断开外部行情、模型和生产库。安装依赖可联网不等于测试可调用真实服务。[A3]

四个入口在S1建立：`make setup`、`make demo`、`make check`、`make test-mobile`。后续更新它们，不再要求记住相邻项目虚拟环境路径。本文之外不新建多份spec/plan；必要决策写进这两份维护文档。

根AGENTS保持短，列明范围、命令、真实数据保护和当前切片。Codex按官方规则读取AGENTS；文档中的实现完成与测试通过必须来自真实执行，不能因为“已有脚本命令”就写PASS。[A1]

### 7.2 Flet MCP 与版本纪律

开发环境安装 `flet-mcp` 和相容的Flet CLI；使用stdio，不开公网MCP。锁定版本后查询控件和API；MCP不可用时读官方文档/已安装签名，不阻塞业务测试，也不能猜方法。[F6][A2]

不要混用0.28教程：1.0存在 `ft.Button`、`await page.push_route(...)`、服务式剪贴板/SharedPreferences等变化；`SafeArea`的旧参数语义也有改变。这里只指出风险，Codex必须以锁定版本检查，不能复制未经验证的UI示例。[F3]

首版不使用复杂声明式状态框架；命令式控件＋明确异步回调足够。普通UI轮询可以是page级任务，但真正的数据任务不能是`page.run_task()`。

### 7.3 AI可读，但不让AI接管

`get_company_context` 提供结构化事实、来源、日期、缺口、已计算排序和变化。默认 `include_personal_notes=False`；未来使用者明确允许才附本人笔记。本轮不接模型、不上传资料、不提供任意shell/代码执行入口。

文本资料只作数据，不能把公告、备注或未来模型输出里的指令当系统操作。UI、CLI、将来AI读取同一服务函数；排名保持确定性、个人判断由本人提交。

## 8. 完成标准与停止范围

本轮最终结果：Web端（兼容移动端浏览器）登录后能查看现有事实、保存个人判断、提交可恢复更新、看自上次已阅以来的变化。实现分三次可用交付，具体任务见执行文档；不设90天工程计划或固定PR数量。

必须实际验证：中文输入和软键盘；360/390/430宽度；浏览器返回；锁屏/网络切换后重连；错误账号不得读取；个人保存重启不丢；任务重复提交与进程中断。原有算法测试继续通过，新增少量业务/存储/安全反例。

Flet官方 `flet test` 文档目前列出原生平台目标，不能替代动态Web浏览器验收；Web自动化需要实际启用并检查语义定位，不能只检查Flutter外壳“有canvas”。无法运行真机时写“待真机验证”，不能以截图或合成fixture冒充已验证移动端体验。[F5][F7]

## 附录 A：最小 DDL（设计合同，实施时由初始化函数创建）

所有写入先在Python层校验；下列SQL不包含真实数据或用户配置。日志模式在初始化前按4.5核验；显式选择DELETE的新建环境仅替换journal_mode这一行，其余合同不变。以下建表及user_version需原子执行；错误后整体回滚。

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;

BEGIN IMMEDIATE;
CREATE TABLE screen_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('peer', 'watch')),
    anchor_code TEXT,
    rule_id TEXT,
    captured_at TEXT NOT NULL,
    valuation_date TEXT,
    health TEXT NOT NULL CHECK (health IN ('complete', 'partial', 'unverified')),
    snapshot_path TEXT NOT NULL UNIQUE,
    snapshot_sha256 TEXT NOT NULL UNIQUE CHECK (length(snapshot_sha256) = 64),
    indexed_at TEXT NOT NULL,
    CHECK ((kind = 'peer' AND anchor_code IS NOT NULL AND rule_id IS NOT NULL)
        OR (kind = 'watch' AND anchor_code IS NULL AND rule_id IS NULL))
);
CREATE INDEX idx_runs_context ON screen_runs(kind, anchor_code, captured_at);

CREATE TABLE watch_items (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    anchor_code TEXT,
    status TEXT NOT NULL CHECK (status IN ('research', 'observe', 'paused')),
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 1000),
    next_check TEXT NOT NULL DEFAULT '' CHECK (length(next_check) <= 1000),
    note_url TEXT CHECK (note_url IS NULL OR length(note_url) <= 2048),
    added_run_id TEXT NOT NULL REFERENCES screen_runs(run_id),
    ack_run_id TEXT REFERENCES screen_runs(run_id),
    ack_at TEXT,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    CHECK ((ack_run_id IS NULL AND ack_at IS NULL)
        OR (ack_run_id IS NOT NULL AND ack_at IS NOT NULL))
);

CREATE TABLE update_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_aliases_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(request_aliases_json) AND json_type(request_aliases_json) = 'array'),
    kind TEXT NOT NULL CHECK (kind IN ('peer', 'watch')),
    payload_json TEXT NOT NULL
        CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
    ),
    phase TEXT NOT NULL DEFAULT 'queued',
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed >= 0),
    total INTEGER CHECK (total IS NULL OR total >= processed),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    error_summary TEXT,
    result_run_id TEXT REFERENCES screen_runs(run_id),
    CHECK ((status = 'succeeded' AND result_run_id IS NOT NULL)
        OR (status != 'succeeded' AND result_run_id IS NULL)),
    CHECK ((status IN ('queued', 'running') AND finished_at IS NULL)
        OR (status IN ('succeeded', 'failed', 'interrupted') AND finished_at IS NOT NULL)),
    CHECK (status != 'running' OR started_at IS NOT NULL)
);
CREATE UNIQUE INDEX idx_active_job_dedupe ON update_jobs(dedupe_key)
    WHERE status IN ('queued', 'running');
CREATE INDEX idx_jobs_queue ON update_jobs(status, requested_at);
PRAGMA user_version = 1;
COMMIT;
```

表中run引用是否包含对应证券、是否为页面曾显示的资料、数据日期是否正确，以及request别名是否唯一，必须由services检查，不能仅凭外键/CHECK存在。应用应先测试SQLite JSON函数可用。启动核验schema，不自动重复执行CREATE/覆盖user_version。时间全部规范化为UTC微秒ISO格式后入库；旧快照原文不改写。

## 附录 B：已核对依据

以下链接只支持现有代码/平台行为，不证明本设计已实现或筛选有效；核对日为2026-09-23。

- [R1] [a-stock-screen 基线](https://github.com/on195594/a-stock-screen/tree/9cfd9a2f09dea94f3d4cb11f0e7e7f726c2f0090)。
- [R2] [当前 AGENTS](https://github.com/on195594/a-stock-screen/blob/9cfd9a2f09dea94f3d4cb11f0e7e7f726c2f0090/AGENTS.md)。
- [R3] [现有 screen.py](https://github.com/on195594/a-stock-screen/blob/9cfd9a2f09dea94f3d4cb11f0e7e7f726c2f0090/screen.py)。
- [F1] [Flet 1.0 发布](https://flet.dev/blog/flet-1-0/)。
- [F2] [Flet 动态 Web/ASGI](https://flet.dev/docs/publish/web/dynamic-website/)。
- [F3] [Flet 1.0 迁移与事件模型](https://flet.dev/docs/updates/migrate-to-1-0/)。
- [F4] [Flet OAuth](https://flet.dev/docs/cookbook/authentication/)。
- [F5] [Flet Web 可访问性](https://flet.dev/docs/cookbook/accessibility/)。
- [F6] [Flet MCP](https://flet.dev/docs/cookbook/flet-mcp/)。
- [F7] [Flet 集成测试目标](https://flet.dev/docs/getting-started/integration-testing/)。
- [F8] [Flet 环境变量：会话、OAuth、Web资源与上传](https://flet.dev/docs/reference/environment-variables/)。
- [F9] [Flet Session Storage 的持久化边界](https://flet.dev/docs/cookbook/session-storage/)。
- [A1] [Codex AGENTS.md](https://developers.openai.com/codex/guides/agents-md/)。
- [A2] [Codex MCP](https://developers.openai.com/codex/mcp/)。
- [A3] [Codex 开发环境](https://developers.openai.com/codex/cloud/environments/)。
- [S1] [SQLite WAL](https://sqlite.org/wal.html)。
- [S2] [Python sqlite3 备份接口](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)。
- [D1] [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
