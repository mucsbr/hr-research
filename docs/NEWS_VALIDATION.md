# HR资讯模块实现与验证

后续已取得一条中国政府网 HR 相关资讯真实请求、解析和隔离 QA 入库成功证据，见 [CONTENT_COLLECTION_VALIDATION.md](CONTENT_COLLECTION_VALIDATION.md)。本文保留首轮验证历史，后续状态以补充记录为准。

记录时间：2026-10-05（Asia/Shanghai）。没有预填用户的行业、地区或官方来源，没有实际模型调用。

## 已实现

- 管理员维护 Feed 或公开网页/栏目来源，配置发布机构、来源类型、关联关注主题、地区、行业、主题、关键词和排除词。
- 手动创建任务并冻结来源配置；复用 `TaskRun(domain=news, kind=news_collection)` 与工作队列，入口 `app.news_service.execute_news_task(task_id)`。
- RSS/Atom 提取标题、链接、正文或摘要、发布时间；公开网页提取文章正文和页面发布日期。栏目页最多跟随同域一层文章链接。
- 单来源默认最多 10 条资讯、3 页，最大 30 条、5 页；单任务最多 20 个来源，所有请求复用公共 URL/DNS 地址限制及任务时间预算。
- 原文网址去重；足够长的标题＋正文完全一致时跨网址关联同一资讯，同时保存每个转载来源和快照。转载后正文发生实质差异时拆成独立记录。
- 日期分别记录：`published_at`（原文精确时间且有时区）、`published_date`（仅日期）、`policy_issued_date`（明确成文/印发日期）、`effective_date`（唯一明确生效日期）、`first_seen_at/last_seen_at`（采集时间）。未知日期保持空，存在多个生效日期时保留候选和不确定性，不选一个猜测。
- 明确区分 `policy_document`、`official_interpretation`、`media_interpretation`、`official_news`、`industry_news`、`source_article`。来源权威性基于管理员配置并记录依据，不宣称由模型核实法律效力。
- 摘要是原文摘录，`summary_type=verbatim_excerpt`；分类是规则分类。未把平台生成建议伪装成官方政策。适用对象仅从明确的“适用对象/适用范围：”原文中提取，不能确定则为空。
- 保存证据 JSON 和观察记录，支持按地区/行业/主题/来源筛选、搜索、已读、收藏和备注。
- 首次发现产生 `news.new` 或 `news.policy` 事务通知事件，稳定键 `news:item:<id>`；重复采集不重复投递。只有匹配用户启用的规则才入通知投递队列。
- 网络失败、需要登录、正文不可识别与部分来源失败均有独立状态，不当成“没有新资讯”。

## 接口契约

前缀 `/api/v1/news`，读取与任务接口需要平台登录，来源配置写入需要管理员。

| 接口 | 数据 |
|---|---|
| `GET /status` | 功能状态、已配置来源数、默认无模型调用和覆盖限制 |
| `GET/POST /sources`、`PATCH /sources/{id}` | `name, kind(feed/web), url, subject_id?, source_type(official/media/industry/other), publisher?, regions[], topics[], industries[], keywords[], exclusions[], enabled` |
| `POST /runs` | `source_ids[]`、`max_articles`、`max_pages`，返回任务 ID |
| `GET /runs`、`GET /runs/{id}` | 任务、逐来源状态、统计与错误 |
| `POST /runs/{id}/retry` | 明确建立新任务，保留原任务证据 |
| `GET /items` | 返回 `{items,total,limit,offset}`；按来源、来源类型、内容类型、地区、主题、行业、已读、收藏、关键词筛选 |
| `GET/PATCH /items/{id}` | 原文与多来源观察；更新 `is_read/is_favorite/notes` |

取消复用 `/api/v1/tasks/{id}/cancel`。所有日期在 API 中是 ISO 格式或 `null`，采集日期不能替代发布日期。

## 验证证据

- Conda：`hr-research-dev`；独立 PostgreSQL：`hr_research_sentiment_test`；通过真实 Alembic `upgrade head`，包含 `f470f2e35444` 资讯迁移。
- `backend/tests/sentiment/test_news.py` 有 **18 项**资讯测试；最新连同舆情、Bridge、招聘统计与对话测试共 **70 项通过**。涵盖日期分离、歧义生效时间、媒体解读、Feed实体拒绝、同域分页预算、来源/筛选/收藏接口、URL/内容去重与转载变化、通知去重、部分失败、取消、来源checkpoint恢复、过期工作进程退出和主动时间预算。
- Ruff 对全部本模块与舆情模块文件通过。
- 模型测试只有舆情部分使用 `httpx.MockTransport`；资讯部分断言 `ModelCall` 为空。没有真实外发通知。
- 2026-10-05 02:44，对中国政府网历史劳动合同法公开网址 `https://www.gov.cn/flfg/2007-06/29/content_669394.htm` 进行 **1 次有界请求**，当前网络返回连接错误 `ConnectError`，没有读取到正文。没有继续广量搜索或把该网址预填到用户来源。记录在 `.runtime/news-validation/official-page.json`。

## 当前限制与自主决定

- Feed 中的正文/摘要不自动扩展为全部外链全文，避免隐式放大请求。`content_extent` 明确区分正文和 Feed 摘要。
- 地区/行业标签优先来自明确来源配置；不会因为正文提到一个城市就断言政策管辖范围是该市。
- 不建设法律自动裁决，不猜测适用对象或生效日期，不自动生成法律结论。
- 目前真实公网官方页面在此网络环境中尚未完成成功提取验证；本模块的公开网页解析、落库、通知与接口链路有隔离测试证据，但这不代替对具体来源的运行验收。
- 前端与中央调度由主工程集成；本文件记录本模块的后端能力和验证范围。
- 每页读取前主动检查取消、执行代次和任务总时长并提交 heartbeat；线程工作节点也不依赖 Celery soft time limit。旧执行代次不写入新结果；恢复任务复用来源记录并跳过已提交成功/部分完成的来源，不重复采集或通知。
