# 真实租约、三公司并发与跨来源状态验证

日期：2026-10-05（Asia/Shanghai）。新增文件仅为 `backend/tests/test_runtime_concurrency.py` 与本验证记录，未修改产品代码。

## 执行环境与边界

- Python：Conda `hr-research-dev`。
- PostgreSQL：新建专用 `hr_research_concurrency_test`，继承根测试 fixture 的 `_test` 检查和真实 Alembic 升级。
- Redis：真实项目 Redis `8.10.2`，独立逻辑数据库14；锁名中的公司/账号都是本轮随机UUID。只清理测试拥有的键，没有 `FLUSHDB` 或生产键修改。
- HTTP：运行完整 `app.main.app` 的 Uvicorn，仅绑定 `127.0.0.1` 随机端口；真实 lifespan、内部工作节点认证、数据库检查和接口路由均参与。
- Worker：受控线程调用实际 Celery 注册任务 `collect_company.run`、`analyze_company.run` 和实际 `dispatch_pending` 决策。没有向真实 broker 投递测试任务。
- 仅浏览器适配器的数据读取替换为该本地服务器的隔离资料接口；真实公司锁和账号锁均由 Redis 执行，没有用假锁证明串行。
- 分析阶段使用 `httpx.MockTransport`，3次模拟协议请求，**真实模型请求0、真实消息外发0**。不触碰真实招聘平台和登录会话，不调用本机 Docker。

## 1. 三家公司同时执行

同一批次创建三家不同公司的独立任务。甲、乙共用一个 BOSS 来源账号，丙使用另一个账号。三个 worker 线程通过 barrier 同时开始：

1. 三家公司各自竞争并取得自己的真实公司租约。
2. 两个不同账号的任务可以同时进入本地浏览器读取；测试观测 `max_total=2`。
3. 共用账号的另一个任务经真实内部 HTTP 接口得到 `waiting_account`，不能进入读取；每个账号的同时读取最大值始终为1。
4. 读取期间直接检查真实 Redis 中公司锁、账号锁存在，等待方不能释放占用方的租约。
5. 释放读取后，调用真实 `dispatch_pending` 处理账号等待恢复；只把最后的 broker enqueue 记录在测试内，随后运行实际 worker 完成等待任务。
6. 三个任务全部成功，保留三个来源任务记录。夹具故意给三家公司相同的 `source_job_id`，仍分别形成三条独立岗位记录，不跨公司去重或串数据。
7. 冻结公司身份、来源请求参数、岗位描述和招聘主体分别匹配对应公司。通过实际 HTTP `GET /jobs?company_id=...` 确认每家公司只有自己的岗位。
8. 结束后所有本轮公司/账号租约均已释放。

继续并发运行三个真实分析 worker 和现有分析图：模拟模型响应通过 barrier 同时进入，逐个检查模型输入只有对应公司的内容。三个报告引用的岗位都属于对应分析任务的公司。独立测试库记录3条模拟 `ModelCall`；这些不是付费或外部模型调用。

## 2. 同一公司的不同任务不重入

使用两个明确不同的幂等键创建同一公司的两个任务，避免被“相同在途请求复用”机制提前合并。

- 第一个任务持有真实公司租约并停留在本地浏览器读取中。
- 第二个 worker 返回，任务仍为 `queued`、`attempt=0`，没有进入浏览器。
- Redis中的占用方 token 没有被等待方替换或释放。
- 第一个任务结束后第二个任务可以正常执行；同一来源岗位仍只有一条，浏览器最大同时读取数为1。

初次调试曾将两个无幂等键、同范围请求当成两个任务，实际被现有在途请求去重复用了同一批次。修正的是测试前提，未把这一正常行为误报为公司锁失效。

## 3. 一个来源消失，另一个来源仍在

使用真实 ingestion 和完整主应用 API：

1. 官网与 BOSS 分别观察到内容一致的同一岗位。
2. 通过真实 `/companies/{id}/job-matches` 生成匹配候选，再通过合并 API 归入同一 canonical job。
3. 官网连续两次相同范围的完整采集未观察到该岗位；BOSS在对应期间继续观察到它。
4. 官网来源记录变为 `suspected_removed`，BOSS来源记录保持 `observed`。
5. `/jobs/{id}` 显示 canonical job 仍为 `observed`，两个来源状态分别保留；`/jobs?company_id=...` 只显示一个合并岗位。
6. 只有官网来源产生一条疑似下架变化事件，没有将企业整体岗位误判为停止招聘。

这里的 `observed` 表示对应来源最近仍被观察到，不宣称绝对实时在招；各来源的观察时间仍随记录返回。

## 4. 同一计划三家公司并发，一家终态失败

补充原 E02 的精确失败组合，不用 `waiting_account` 代替终态失败。完整主应用的真实 HTTP 登录会话创建包含三家公司的计划，再通过 `/plans/{id}/run` 创建三个独立底层任务。三家公司各用不同测试账号，barrier 同时启动三个实际 `collect_company` worker，真实 Redis 公司锁和账号锁全部参与。

- 三个任务同时进入本地资料读取，实际 `max_total=3`，每个账号并发最大为 1。
- 一家公司的本地资料端点实际返回 HTTP 503，经未改动的内部浏览器路由和 Worker 错误路径形成 `SourceRun.failed`、`error_code=browser_collection_failed` 及 `TaskRun.failed`。测试没有直接给任务写失败状态。
- 另外两家均完成采集和入库，主应用批次 API 返回 `completed_with_issues`，计数严格为 `succeeded=2 / failed=1`。
- 三个任务/来源记录分别保留；失败公司岗位数为 0，成功公司各 1，刻意相同的来源岗位编号仍隔离为两个 canonical job。实际岗位 API 的标题与对应公司身份相符。
- 结束后本轮 UUID 公司/账号锁全部释放；ModelCall 和 NotificationDelivery 均为 0。没有请求外部网站、模型或收件服务，也没有调用本机 Docker。

本次使用 Conda `hr-research-dev`、专用 PostgreSQL `hr_research_recruit_test`、真实 Redis 逻辑数据库 14、随机 localhost HTTP 端口。实际 Worker 通过受控线程调用，不向 broker 投递任务，不需要创建或消费生产队列。fixture 仅删除本轮 UUID 锁并终止自己启动的 HTTP 线程。

初次运行的唯一失败是测试将批次状态误写为 `partial`；核对既有 `batch_state` 后修正为真实契约 `completed_with_issues`。没有为此改动产品代码。

## 结果与限制

- 首轮定向验证：**3项通过，1.88秒**。E02 新增后该文件完整定向：**4项通过，2.36秒**。
- `ruff check backend/tests/test_runtime_concurrency.py`：通过。
- 本轮独立测试使用真实 PostgreSQL、Redis和内部HTTP；没有产品代码修改或真实平台访问。
- 浏览器适配器返回的是明确的本地夹具数据，因此结果证明的是运行时租约、任务隔离、接口集成和入库/报告上下文边界，不能替代 BOSS 等真实平台现场采集验收。
- 原3项随后纳入第二轮378项以及修复 Playwright 回调后的第三轮全量：**380项通过，107.32秒**，Ruff及Alembic检查通过；head=`9d924bc617c7`。E02 是这轮全量结束后新增的一项，因此准确口径为“380项全量 + 新增1项所在文件4项定向”，没有宣称381项全套重跑。
- 第三轮全量后端96文件聚合指纹 `4ed3a553ea6bb7275bc1615bdcf34dc55f27ad3f89951926893ffc44b8af8947`。E02定向后逐项SHA核对，仅 `backend/tests/test_runtime_concurrency.py` 改变，产品源码与适配器清单均未变；该测试文件SHA为 `fad476f641bfc7f17f65fbebb8c6df8aad4ee376a949a9394c517d519196a3b5`。
- E02产物：`.runtime/e02-runtime-junit.xml`、`.runtime/e02-runtime-source-validation.json`；全量版本边界见 `INTEGRATION_VALIDATION.md`。
- Playwright `EventGreenlet`/`ContextVar` 问题由独立回归修复：响应回调显式接收冻结manifest及repair capture，真实EventGreenlet的两个先红后绿用例已纳入380项全量。这里的本地浏览器资料夹具仍不用于证明真实第三方账号采集可用。
