# 全后端集成验证记录

第三轮已在修复真实 Playwright `EventGreenlet` 上下文传播缺口后完成：**380项全量通过**，Ruff及Alembic检查通过，测试期间96个后端文件与适配器配置前后指纹一致。随后只新增E02测试，其所在文件**4项定向通过**，产品源码未改；没有宣称381项全套重跑。前面的256项、378项结果和指纹保留为历史证据，不覆盖或混用版本。

日期：2026-10-05（Asia/Shanghai）。本轮专用数据库为 **`hr_research_integrated_test`**，与应用库、招聘并行测试库、舆情模块测试库隔离。Python 使用 `hr-research-dev` Conda 环境；未调用本机 Docker。

## 第一轮冻结验证

第一轮完成首轮全量、新增主应用集成用例，以及收到主工程冻结通知后的全量验证。当轮测试前后78个后端源码/测试/迁移/配置文件指纹一致，没有检测到测试期间的后端文件变化。

| 阶段 | 范围 | 结果 |
|---|---|---|
| 首轮全量 | `pytest backend/tests -q --tb=short` | 233 项通过，23.91 秒 |
| 首轮静态检查 | `ruff check backend` | 通过 |
| 新增主应用集成 | `backend/tests/test_application_integration.py` | 20 项通过；使用完整 `app.main` 与实际登录会话 |
| 最终冻结全量 | `pytest backend/tests -q --tb=short`，含最新维护测试和新增主应用用例 | **256 项通过，27.36 秒** |
| 最终静态检查 | `ruff check backend` | **通过** |
| 最终迁移一致性 | 专用库 `alembic check` | **No new upgrade operations detected**，head=`b82644ab8bac` |

## 最终版本范围与复核产物

- 冻结验证开始时间：`2026-10-05T03:07:52.232687+08:00`。
- 后端指纹复核时间：`2026-10-05T03:08:57.469621+08:00`。
- 版本范围：`backend/` 下78个 `.py/.toml/.ini` 文件，涵盖应用、所有测试、迁移及工程配置；不包含缓存和私有环境文件。
- 上述文件路径与 SHA-256 清单的聚合指纹：`0c54a4d72138558dc896500a9c1bf0f7ea4d3bd852e881edcc9dca99f3d03702`，前后相同。
- 当前适配器配置文件清单指纹：`1f6f93de1b12c3f8c195ee5fc3d394203896e47170d53612fbc1213aada43760`。
- 运行版本：Python `3.11.13`、pytest `9.1.1`、Alembic `1.20.0`、SQLAlchemy `2.0.54`、FastAPI `0.142.2`、Pydantic `2.13.5`。
- 本地复核产物：`.runtime/integration-final-junit.xml`、`integration-final-source-before.json`、`integration-final-source-after.json`、`integration-final-runtime.json`、`integration-final-schema.json`。这些运行产物未保存真实凭据。

最终整套测试包含所有模块用例；没有只挑选通过的子集或用裸 Router 测试替代主应用集成。此结果对应上述后端版本范围；前端及远程部署有独立验证记录。

## 数据库、模型和通知安全边界

- 通过私有配置在内存中派生专用测试数据库连接；没有把数据库密码、模型 Key 或会话密钥写入本文件或测试启动脚本。
- `TEST_DATABASE_URL` 显式选定 `hr_research_integrated_test`。全套测试只继承根 `conftest.py` 的 session fixture，安全检查要求数据库名以 `_test` 结束；没有子模块的独占数据库 fixture。
- 每次测试会话执行真实 Alembic `upgrade head`，每条数据库用例在专用库清理测试数据，不访问应用数据库。
- 根 fixture 覆写 `APP_ENV=test`、假模型地址/Key、假管理员密码、测试专用签名密钥及临时 Fernet Key，不复用实际应用密钥。
- ModelGateway 测试模式拒绝没有 MockTransport 的模型网络调用。通知层同样拒绝未模拟的 HTTP/SMTP 发送。测试中的模型响应、Webhook 和 SMTP 都是模拟依赖；没有真实模型调用或对外发送消息。
- 任务分派测试替换 Celery `apply_async` 和 Redis 探测；验证队列映射但不把测试任务投递到真实 broker。

## 主应用集成覆盖

新增用例文件为 `backend/tests/test_application_integration.py`。所有 HTTP 用例使用根 `client/signed_client`，加载 **`app.main.app`、真实 lifespan、数据库用户会话、主应用异常处理和 Origin 中间件**，不是单独挂载一个裸 Router。

已覆盖：

1. 公司、岗位、计划、数据源账号、报告、用户、审计、舆情、资讯、对话、通知、适配器维护、入口发现配置等实际路由注册。
2. 未登录访问以上私有模块返回 401；健康与就绪接口按设计可公开访问。
3. 实际登录 Cookie 可以访问已注册模块；查询不生成模型调用或通知投递。
4. 普通用户可以读取业务数据，但不能维护用户、创建受限资讯来源、登记适配器或创建来源登录账号。
5. Origin 中间件覆盖新增舆情、资讯、对话和维护写接口；外站写入返回 403。
6. 全局校验错误不回显 Webhook 密钥或对话动作 Token。
7. 公共 `/tasks/{id}` 详情按舆情/资讯域正确返回来源与事件。
8. 公共重试入口保留 `sentiment_collection`、`news_collection`、`company_analysis`、`company_collection` 和 `company_comparison` 的正确类型与上下文，不把无 company_id 的任务错误转换为公司采集。
9. 舆情人工导入任务拒绝通过通用重试变成采集任务。
10. 比较重试重新生成当前时点结构化快照，保持独立任务，不创建 `CompanyRun` 或模型调用。
11. 排队/运行任务的重试拒绝，以及招聘、舆情、资讯公共取消规则。
12. 任务轮询列表不传大型 `metrics_snapshot`；单任务详情仍保留完整冻结证据。
13. 浏览器内部采集接口不能仅凭普通用户登录访问；缺失/错误工作节点 Token 返回 403，正确 Token 仍校验任务与来源存在性。
14. 主应用没有重复的 HTTP 方法＋路径注册；所需 Celery 任务均已注册。
15. 招聘采集→`recruitment`、单公司分析/比较→`analysis`、舆情→`sentiment`、资讯→`news` 的队列分派；取消任务不分派。

## 发现与处理

- 首轮全量没有出现独占 fixture、迁移、业务路由或任务类型回归。
- 新增集成用例初次运行有一处测试代码自身的 `import app.workers` 遮蔽 FastAPI `app` 变量，已修正；不是产品缺陷。
- 主工程此前修正的适配器维护外键创建/删除顺序已在本轮新库迁移中通过。

## 验证范围限制

本记录证明本地完整后端应用在真实 PostgreSQL 测试库中的接口、事务、注册和执行分派行为。真实招聘/社交账号的外部登录、目标网站实时可达性、真实邮件或机器人投递、实际模型质量及远程 Docker 部署，分别由对应验证记录说明；不会用本轮测试替代这些外部验收。

## 第二轮冻结验证基线（官网检查点、执行隔离与真实并发）

收到主工程第二次冻结通知后，沿用专用 `hr_research_integrated_test`，完整运行了当前全部后端测试，包括新增真实 Redis 并发用例和官网检查点的真实 Celery 子进程用例。没有重启已有验证进程或额外调用真实 LLM/外部平台。

| 检查 | 实际结果 |
|---|---|
| `pytest backend/tests -q --tb=short` | **378 passed**；0失败、0错误、0跳过 |
| 控制台测试时间 | **106.61秒** |
| JUnit统计时间 | **106.590秒**，开始于 `2026-10-05T04:53:03.144577+08:00` |
| `ruff check backend` | **通过** |
| 专用库 `alembic check` | **No new upgrade operations detected** |
| Alembic head | **`9d924bc617c7`** |
| 后端源码前后核对 | **96个文件一致，无变更** |
| 适配器配置前后核对 | **一致，无变更** |

源码记录范围和指纹：

- 开始记录：`2026-10-05T04:53:01.756244+08:00`。
- 结束复核：`2026-10-05T04:57:15.178806+08:00`。
- `backend/` 下 `.py/.toml/.ini` 文件共96个（不含缓存、私有环境文件）；聚合 SHA-256：`bf1cba593f33f367440df6f06e01952581a907a0acbd882267f37adb131dd93e`。
- 适配器配置清单聚合 SHA-256：`1f6f93de1b12c3f8c195ee5fc3d394203896e47170d53612fbc1213aada43760`。
- `.runtime/integration-round2-source-before.json` 与 `integration-round2-source-after.json` 保存路径＋SHA清单；两者的 `changed_since_start` 与 `adapters_changed_since_start` 均为空。
- 完整测试结果：`.runtime/integration-round2-junit.xml`；迁移结果：`integration-round2-schema.json`。

本轮覆盖的关键新增行为：

- 官网逐页/详情检查点接入真实 worker；HTTP中断后恢复，不重读已完成步骤。
- `active_execution` 与内部 HTTP `expected_attempt` 阻止旧执行提交成功、失败或最终状态。
- 来源局部提交保持幂等，保留真实 `observed_at`；较旧缓存不能覆盖较新观察或推断岗位下架。
- 预算分段恢复逐步增加观察，之前已提交资料不重复计数。
- API重试关联恢复前驱；详情返回检查点元数据。
- 三家公司真实租约并发、共用账号串行、不同账号并行、公司上下文及入库隔离；真实分析图并发使用MockTransport，未访问外部模型。
- 一个来源疑似下架而另一来源仍观察时，真实岗位API保留canonical岗位与两个来源状态。

Redis和子进程清理已核对：

- `test_runtime_concurrency.py` 使用真实项目Redis逻辑数据库14；实际连接参数已确认 `db=14`。用例在结束前断言其公司/账号租约释放，fixture最终只删除保存的本轮UUID键集合。
- `test_real_celery_worker_resumes_http_failure_then_ingests_once` 使用当前项目broker，但只启动监听 `checkpoint-qa-<uuid>` 的独立worker，禁用mingle/gossip/heartbeat；生产队列不会被该worker消费。
- 该用例结束时先终止并等待其worker退出，再删除该UUID队列和其测试公司UUID锁。未执行 `FLUSHDB`、`FLUSHALL` 或按宽泛模式删除共享Redis数据。
- 本地Uvicorn测试服务器在fixture结束时退出并断言线程停止。

本轮测试没有失败项。上述证据对应当时冻结的后端与适配器配置范围；没有以模拟响应替代真实账号/网站可用性验证，外部来源现状仍分别见相关验证文档。

### 测试结束后的新增审计项

主工程随后报告：真实 Playwright `response` 回调运行在独立 `EventGreenlet` 中，不继承调用方的 `ContextVar`，因此原来的 manifest冻结与repair evidence capture可能丢失。这是本轮测试未覆盖到的真实浏览器事件上下文缺口，不能因378项通过就认为已解决。

该问题随后在 `platform_collectors.py`、`repair_evidence.py` 修复，并增加真实浏览器回归。第二轮已完成的结果保留为**修复前基线**；修复后的独立验证见下一节，没有重启或冒充覆盖已经结束的第二轮测试进程。

## 第三轮冻结验证（EventGreenlet修复后）

修复将冻结manifest与repair capture显式传入真实Playwright响应回调，避免依赖跨greenlet的ContextVar继承。两个真实 `EventGreenlet` 回归先复现缺口，再验证修复。主工程冻结上述改动后，独立执行完整后端测试，不沿用第二轮结果。

| 检查 | 实际结果 |
|---|---|
| `pytest backend/tests -q --tb=short` | **380 passed**；0失败、0错误、0跳过 |
| 控制台测试时间 | **107.32秒** |
| JUnit统计时间 | **107.303秒**，开始于 `2026-10-05T05:12:36.258127+08:00` |
| `ruff check backend` | **通过** |
| 专用库 `alembic check` | **No new upgrade operations detected** |
| Alembic head | **`9d924bc617c7`** |
| 测试数据库 | **`hr_research_integrated_test`** |
| 后端源码前后核对 | **96个文件一致，无变更** |
| 适配器配置前后核对 | **一致，无变更** |

源码与证据范围：

- 开始记录：`2026-10-05T05:12:34.656179+08:00`；结束复核：`2026-10-05T05:16:36.027861+08:00`。
- 96个后端 `.py/.toml/.ini` 文件聚合 SHA-256：`4ed3a553ea6bb7275bc1615bdcf34dc55f27ad3f89951926893ffc44b8af8947`。
- 适配器配置清单聚合 SHA-256：`1f6f93de1b12c3f8c195ee5fc3d394203896e47170d53612fbc1213aada43760`。
- 前后路径与SHA清单：`.runtime/integration-round3-source-before.json`、`.runtime/integration-round3-source-after.json`，其中 `changed_since_start` 和 `adapters_changed_since_start` 均为空。
- 完整测试结果：`.runtime/integration-round3-junit.xml`；迁移核验：`.runtime/integration-round3-schema.json`。

这轮保留第二轮的真实Redis、PostgreSQL、localhost HTTP及独立Celery子进程覆盖，并纳入真实 `EventGreenlet` 回调回归；模型、邮件和Webhook仍为受控模拟，没有对外调用或发送。没有调用本机Docker。

### 第三轮后的E02定向补充

第三轮结束后，只在 `backend/tests/test_runtime_concurrency.py` 增加1项更精确的E02用例：真实主API创建三公司计划，3个实际Worker和真实Redis租约同时运行，一家公司本地HTTP端点返回503，经既有错误路径形成终态 `failed`；另两家成功，真实批次API返回 `completed_with_issues`、成功/失败计数2/1。

- 新增后的整个运行时并发测试文件：**4 passed in 2.36s**；Ruff通过。
- 该次定向使用专用 `hr_research_recruit_test`、真实Redis逻辑数据库14和随机localhost HTTP端口；无外部网站、真实模型或通知请求。
- 逐项SHA复核仅上述测试文件变化；产品源码和适配器配置均与第三轮冻结版本一致。
- 新测试文件 SHA-256：`fad476f641bfc7f17f65fbebb8c6df8aad4ee376a949a9394c517d519196a3b5`。
- 证据：`.runtime/e02-runtime-junit.xml`、`.runtime/e02-runtime-source-validation.json`；详细执行边界见 [RUNTIME_CONCURRENCY_VALIDATION.md](RUNTIME_CONCURRENCY_VALIDATION.md)。

准确验收口径是“**380项全量通过，随后新增1项所在文件4项定向通过，产品源码未变**”。未再次运行381项全套测试，不把不同执行拼接成一次全量结果。真实腾讯第二次有界观察另见 [RECRUITMENT_REAL_OBSERVATION.md](RECRUITMENT_REAL_OBSERVATION.md)，不会将外部采集并入模拟测试数量。
