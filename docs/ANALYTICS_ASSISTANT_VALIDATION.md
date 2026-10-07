# 招聘趋势、跨公司比较与最小对话入口

记录时间：2026-10-05（Asia/Shanghai）。这些新增功能不依赖默认模型调用，测试未访问真实 LLM。

## 招聘统计与比较

`GET /api/v1/recruitment/metrics?company_id=<id>&days=30` 返回：

- 当前公司已观察岗位数、来源记录数、历史记录数、可比较岗位数。
- 城市和职能分布；未分类/未标注如实保留。
- 逐来源状态、最新尝试时间、最近成功时间、覆盖和范围哈希。
- 每日 `JobChange` 序列，`baseline_seen` 与 `first_seen` 分开，覆盖不明的日期用 `null`，不补造 0。
- 范围变化标记与 `trend_comparable`；计数是来源观察事件，岗位合并状态为 `merged` 的实体不重复计入。
- 结构化统计由程序计算；未覆盖来源、失败来源、不同筛选范围均附限制。

没有成功来源时 `observed_jobs=null`，保留历史记录数作为背景。部分来源有样本时可以显示样本数，但 `comparable_jobs=null`；完整成功且实际为零才可显示零。没有采集的日期不会被连接成零变化趋势。

`POST /api/v1/recruitment/comparisons` 输入 `{company_ids:[...],days:30,idempotency_key?}`，需要 2—20 家公司，创建独立 `TaskRun(kind=company_comparison, company_id=null)`。工作入口为 `app.analytics_service.execute_comparison_task(task_id)`。

任务创建时保存公司名、时间窗口及各公司**结构化指标快照**，包含有限证据链接，不包含原始全量 JD。工作节点只汇总这个快照；排队期间更名、停用来源、归档公司或合并岗位不会改变原任务的分析依据。重试调用 `create_comparison(db, old_company_ids, days=old_days)`，重新取得当前时点快照，原报告不覆盖。

只有各公司最新完整范围、来源类型和筛选条件一致时，才生成直接数量差；否则 `difference_from_first=null`，报告状态为部分可用并说明限制。数量差比较的是对应来源最新观察到的岗位数，**不代表时间窗口内的净扩招人数**。报告保存到公共 `analysis_reports`，产生 `report.ready` 通知事件。默认模型调用数为 0。

## 对话契约

- `GET /api/v1/assistant/capabilities` 提供实际支持模式、示例、限制。
- `POST /api/v1/assistant/messages` 输入 `{message:"..."}`，返回 `unsupported`、`clarification`、`query_result` 或 `action_draft`。
- `POST /api/v1/assistant/actions/execute` 输入 `{action_token:"..."}`，执行当前用户的有效签名动作。

已支持示例：

```text
添加公司：星河科技、远山科技
更新招聘：星河科技；城市=上海；关键词=研发；来源=官网,BOSS
查询岗位：星河科技；城市=上海；关键词=工程师
比较招聘：星河科技、远山科技；天数=30
每天监测：星河科技；时间=09:00；城市=上海；关键词=研发
暂停计划：星河科技招聘监测
生成分析：星河科技
```

也支持计划中几个明确表达：“现在更新 A 的招聘”“A 最近一个月主要招什么人”“比较 A、B 最近一个月的招聘变化”“以后每天关注 A 在上海的研发岗位”“暂停 A 的监测”。这是公开说明的有限中文语法，不声称通用对话理解。未匹配、未知公司、同名歧义或不支持的附加条件不会调用模型、采集或修改配置。

写操作生成15分钟有效、绑定用户和动作参数的签名草稿，界面可以显示目标和筛选范围，再显式执行。返回 `model_calls=0` 只表示本次解析未调用模型。“生成分析”标明 `model_usage=one_bounded_analysis`、`estimated_model_calls=1`；采集/每日计划标明 `model_usage=conditional_website_adaptation`、`estimated_model_calls=null`，因为新官网或页面改版时采集器可能申请受预算限制的适配调用。其他动作标明 `model_usage=none`。

执行复用：

- 公司添加：`bulk_companies`，同名对象不自动合并。
- 采集：`create_batch`，每家公司独立子任务，缺少来源显示待配置。
- 单公司分析：`create_analysis`，不创造另一个队列。
- 比较：`create_comparison`，独立比较任务。
- 创建/暂停计划：调用现有计划 CRUD，统一 `Asia/Shanghai` 和既有调度器。
- 岗位查询：直接读取已有岗位及覆盖，不暗中采集；城市、JD关键词和来源过滤复用现有字段/筛选语义。最多扫描1000个近期更新岗位并返回100个，达到扫描上限时总匹配数保持未知。

一次动作执行先写入持久化 `AuditEvent` 执行凭据，再调用既有服务。重复执行已完成动作返回原结果；同一动作并发或服务提交后结果尚未记录时，不重复写入。后者明确显示“需要核对执行结果”，保留执行中凭据供审计，避免计划或任务被重复创建。

暂停公司监测时，如果涉及多个计划或共用计划还包含其他公司，返回候选要求明确计划，不擅自暂停其他公司的监测。用户可以在指令中使用公司/计划 ID 消除同名歧义。

## 验证

使用 `hr-research-dev` 和显式专用 `hr_research_sentiment_test`，继承全项目 `*_test` 数据库安全 fixture 和真实 Alembic 迁移。

- `test_analytics.py`：9 项，覆盖缺失数据空值、基线分离、部分失败、合并排除、范围变化、独立零模型比较、幂等、创建后配置/归档/合并变化不污染快照、API窗口校验。
- `test_assistant.py`：11 项，覆盖有限语法、未匹配零调用、草稿无副作用、显式执行幂等、逐公司任务、同名歧义、直接查询、计划复用、共用计划暂停歧义、独立比较、签名篡改和不支持筛选拒绝。
- 连同舆情/Bridge/资讯，共70项测试通过；Ruff通过。没有真实 LLM 调用、外发消息或本机 Docker 操作。

没有新增数据库表。中央任务派发、API路由注册和前端页面由主工程接入。
