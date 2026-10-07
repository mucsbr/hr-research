# 舆情模块实现与验证记录

后续采集链路、实体归属、多词轮询及真实来源验证已补充到 [CONTENT_COLLECTION_VALIDATION.md](CONTENT_COLLECTION_VALIDATION.md)。本文保留首轮实现与验证历史，后续状态以补充记录为准。

记录时间：2026-10-05，Asia/Shanghai。本文仅记录真实实现与已有证据，不把未登录/网络失败或测试替身当成平台采集成功。

## 1. 已实现链路

1. 创建自有公司、品牌、产品关注对象，维护名称、别名、关键词、排除词和启用状态。
2. 为对象配置公开网页、微博、小红书、抖音等来源，保存来源名称、网址与可选平台账号；微博/小红书/抖音未填网址时使用对象关键词生成对应搜索入口。
3. 人工创建采集任务；任务快照固定对象、来源、词表、日期范围和页面预算，每来源默认最多 2 页，最多 5 页，单任务最多 20 个来源，整体受任务时间预算约束。
4. 公共 HTTP 读取、可选浏览器渲染、显式绑定已保存的登录会话均有执行路径。绑定账号须取得共享账号锁，密码和 Cookie 不进入模型输入或原文证据。
5. 解析实际文章正文、JSON-LD、微博卡片、平台页面公开状态中的原文；不把导航、登录页或仅有链接的结果页当成帖子。
6. 支持人工 JSON 样本导入。每条数据保存原文网址、平台、公开发布时间（未知保持空）、首次发现和最近发现时间、内容哈希、互动字段、规则分类。
7. 以对象＋平台＋来源 ID/规范化网址去重。同一原文重复采集保留观察历史，不重复新增；不同网址的相同文字保持独立，避免抹掉独立客户案例。
8. 持久化提取后的证据 JSON、原文快照、来源采集批次。证据文件不含完整认证浏览器状态。已读、收藏、忽略和备注不会被后续采集覆盖。
9. 原文首次出现产生 `sentiment.new` 或 `sentiment.negative` 通知事件；同一原文只选一种，去重键为 `sentiment:item:<id>`。仅匹配用户已启用规则才进入通知投递队列，业务事务内不发送网络消息。
10. 分析必须显式触发，最多读取 8 条相关原文摘要；统计由数据库完成，模型只归纳与引用。一次模型请求通过公共预算网关，不自动重试或换模型。引用不存在的原文 ID 时拒绝保存报告。
11. 分析报告保留时间范围、来源最近采集状态、实际样本数、原文证据、模型调用 ID、覆盖限制。报告完成产生 `report.ready` 事件。

分类覆盖客户体验、产品服务、公司经营、雇主口碑、合作生态及其他。情绪是 `positive_clue/negative_clue/mixed_clue/unclassified` 的规则线索，始终带 `verification=unverified`，没有把情绪标签当作事实核实结果。

## 2. 核心接口和工作节点契约

所有业务接口需要平台登录，前缀 `/api/v1/sentiment`。

| 接口 | 用途 |
|---|---|
| `GET /status` | 独立采集、分析和 BettaFish 配置的实际状态 |
| `GET/POST /subjects`、`PATCH /subjects/{id}` | 关注对象与词表 |
| `GET/POST /sources`、`PATCH /sources/{id}` | 对象来源、平台账号、健康状态和最近来源任务 |
| `POST /runs` | `subject_id`、`mode=collect/analyze/bettafish`、`source_ids`、带时区的时间范围、`max_pages`、`render` |
| `GET /runs`、`GET /runs/{id}` | 任务、逐来源覆盖、错误与事件 |
| `POST /runs/{id}/retry` | 明确新建任务重试，原任务证据保留 |
| `POST /imports` | `{subject_id, items:[{platform,url,title,content,published_at?,author?,metrics?}], note?}` |
| `GET /items` | 分页 `{items,total,limit,offset}`；对象、平台、分类、已读/收藏/忽略、关键词过滤 |
| `GET/PATCH /items/{id}` | 证据历史、已读、收藏、忽略和备注 |
| `GET /reports`、`GET /reports/{id}` | 报告与证据 |
| `GET /reports/{id}/artifact` | 已生成 BettaFish HTML 下载，带 sandbox CSP 和 nosniff |

取消复用公共 `POST /api/v1/tasks/{id}/cancel`。工作入口为 `app.sentiment_service.execute_sentiment_task(task_id)`，由中央任务调度监听 `sentiment` 队列。任务域为 `sentiment`，类型为 `sentiment_collection`、`sentiment_analysis`、`sentiment_bettafish`；导入任务同步完成并保存为 `sentiment_import`。

## 3. BettaFish 真实上游适配依据

审阅固定版本：`b67928ff1ce515dd7672408221fb1e9500d0972d`。读取的是实际 Git 对象，未猜测 REST 接口：

- [ReportEngine/agent.py](https://github.com/666ghj/BettaFish/blob/b67928ff1ce515dd7672408221fb1e9500d0972d/ReportEngine/agent.py)：`ReportAgent.generate_report(query, reports, forum_logs, custom_template, save_report, stream_handler)`；返回 `html_content` 及任务文件信息。
- [ReportEngine/flask_interface.py](https://github.com/666ghj/BettaFish/blob/b67928ff1ce515dd7672408221fb1e9500d0972d/ReportEngine/flask_interface.py)：`current_task`、`report_agent` 与进程级任务锁。
- [ReportEngine/llms/base.py](https://github.com/666ghj/BettaFish/blob/b67928ff1ce515dd7672408221fb1e9500d0972d/ReportEngine/llms/base.py)：`invoke`、`stream_invoke`、`stream_invoke_to_string` 和 `get_model_info`。
- [ReportEngine/utils/config.py](https://github.com/666ghj/BettaFish/blob/b67928ff1ce515dd7672408221fb1e9500d0972d/ReportEngine/utils/config.py)：输出目录、模板、重试及模型配置。

当前 Bridge 复用 ReportEngine：输入本平台已有的原文与覆盖数据，通过真实模块入口生成报告。没有假称它运行了 Query/Media/Insight 三引擎的新一轮网络采集；传入 reports 的第三个位置只是已采集证据容器，前两个明确为空。

实现方式：

- 固定 Git 版本，并拒绝已跟踪源码修改的仓库。
- 每个任务独立工作目录；绝不按“最新文件”找报告。
- 数据库会话级锁使同一实例串行执行。
- 独立 Python 子进程，以 `-I` 避免项目同名模块污染标准库。
- 子进程不继承 API Key、数据库连接、代理凭据或上游 `.env`。
- 上游 LLMClient 替换为 IPC 适配器；每一次请求由父进程 ModelGateway 记录预算和调用结果。子进程常规 Python socket 网络连接被禁用；该措施是受信固定源码的执行约束，不声称替代操作系统级对恶意代码的沙箱。
- 预算耗尽、模型调用异常、取消或超时时停止进程，不自动提高预算，不自动重试未知付费请求。
- 报告正文在 API 中以纯文本展示；HTML仅作为受限下载产物。

配置字段：`BETTAFISH_REPO_DIR`、`BETTAFISH_PYTHON`、`BETTAFISH_REVISION`、`BETTAFISH_LICENSE_CONFIRMED`、`BETTAFISH_TIMEOUT_SECONDS`。默认未配置；授权标记默认 `false`。健康状态使用 `not_configured/invalid_configuration/version_mismatch/license_pending/configured_unverified`，配置存在不等于真实报告已成功运行。

授权：此前已经记录根 GPL-2.0、README 非商业表述及 MediaCrawler 非商业学习许可证之间的适用范围问题。没有因为内部项目而自行声明已授权，也没有擅自开启确认标记。独立公开采集、导入、平台分析仍可运行。

## 4. 未登录真实页面检查

2026-10-05 02:35（Asia/Shanghai），每个平台最多两次页面请求；没有登录、验证码绕过、模型调用或广量搜索。只使用“腾讯云”作为公开页面可读性测试关键词，没有把它写成用户竞品或关注偏好。

| 来源 | 第一次：匿名公共 HTTP | 第二次：授权 SOCKS5 路径 | 结论 |
|---|---|---|---|
| 微博搜索 | 重定向到 `passport.weibo.com`，标题 `Sina Visitor System`，页面可取但没有原文 | 未再请求 | 登录/访客会话门槛，`waiting_login`；不能报告 0 条舆情 |
| 小红书搜索 | 当前网络的公网 DNS 校验失败 | TLS 握手失败 | 当前环境网络未通，不能断言登录后可采或平台没有内容 |
| 抖音搜索 | TLS/连接错误 | TLS 握手失败 | 当前环境网络未通，不能断言登录后可采或平台没有内容 |

本地复核资料在 `.runtime/sentiment-validation/anonymous-pages.json` 和 `anonymous-proxy-pages.json`（运行产物，不提交用户环境路径和凭据）。网络失败保留 `null` 样本数，不转换成 0 个帖子。

## 5. 自动化验证

Conda 环境：`hr-research-dev`。本轮通过 `TEST_DATABASE_URL` 明确选择专用 PostgreSQL 数据库 `hr_research_sentiment_test`；测试继承项目级仅允许 `*_test` 数据库的安全 fixture，支持一次运行全量测试。真实执行 Alembic `upgrade head` 后运行，未使用主应用数据库或并行招聘测试库。

测试文件：`backend/tests/sentiment/`。本轮 **32 项测试通过**，Ruff 通过，涵盖：

- 接口鉴权、对象关键词与来源域名校验。
- 登录/验证/删除页面识别，未知时间不补造。
- 导入重复、独立案例、版本证据、已读收藏保留与通知去重。
- 实际来源部分失败、完整失败、页面预算、时间过滤、对象隔离与取消。
- 使用 `httpx.MockTransport` 的一次分析、预算拒绝、无证据零调用、非法引用拒绝。
- 真实子进程执行的 BettaFish 协议测试：两个任务产物不串、环境无凭据、子进程网络禁用、每次模型请求经预算网关、授权门禁、版本变化拒绝、取消终止。

BettaFish 子进程测试使用最小上游接口 fixture，证明的是桥接协议和运行隔离。真实上游源码入口已核查，但**没有真实调用模型生成整份 BettaFish 报告，也没有宣称生产依赖全部就绪**。默认低预算可能不足以完成上游多阶段长报告，此时明确失败并保留调用记录；不会自动放大成本。

## 6. 自主决策与当前限制

- 首版采用“公开页面/人工导入＋可选登录浏览器”可执行路径，未将专有爬虫许可问题转化为停掉整个平台。
- 使用明确的页面预算和样本覆盖，不尝试自动无限分页，也不拿采集条数解释全平台声量。
- 未自动执行视频下载、口播转写或图片 OCR；相关限制随来源记录和报告展示。
- 定时调度、前端页面和公共通知投递由主工程集成；本模块提供手动任务、统一任务记录和事务事件接口。
- 登录会话契约复用本平台保存的 Playwright storage state；未宣称它可以直接传给 MindSpider/MediaCrawler。
- 尚需实际有效账号与网络可达环境，才能验证各社交平台登录后的采集覆盖；此处不把该外部条件掩盖成接口成功。
