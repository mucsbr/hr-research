# 定时摘要通知实现与验证

## 使用方式与窗口口径

通知规则现在可以显式选择“即时提醒”或“每日定时摘要”。已有规则继续使用即时模式，系统不会自动将旧规则转换成摘要，也不会创建默认订阅或自动发送空日报。

每日摘要固定按 `Asia/Shanghai` 自然日分组：`[当日 00:00, 次日 00:00)`，在次日用户设置的 `HH:MM` 生成 outbox。例如，10 月 5 日纳入规则的事件，在 10 月 6 日 09:00 汇总。范围依据事件纳入订阅的时间，不是文章发布日期；原业务事件时间和纳入摘要时间分别保留。

只捕获规则启用之后匹配的新事件，不扫描历史事件补发。没有匹配事件的日期保持安静。每日摘要包含全部匹配事件，不使用即时提醒的 cooldown；启用之前、未命中关键词/对象/事件类型的内容不会事后补入。

界面增加：

- 规则编辑中的提醒方式与北京时间发送时间。
- “待汇总日报”标签页，展示规则、通道、窗口、事件数与预计汇总时间。
- 投递详情抽屉，展示摘要分片范围、事件标题、摘要与原文链接。

即时、摘要和发送失败沿用同一个投递历史。普通用户可查看，新增/修改规则仍要求管理员权限。

## 持久化与发送流程

`emit_event` 在原业务事务内创建 `NotificationEvent`。即时规则保持原 outbox 行为；每日摘要规则同时写入 `NotificationDigestItem`，与业务数据一起提交或回滚。

摘要项持久保存 rule/channel、通道配置版本戳、源事件 ID、北京时间窗口、预计时间及状态。`unique(rule_id, source_event_id)` 防止同一规则重复捕获同一事件。第一次创建或启用规则不会扫描旧事件，因此不存在首次启用后补发历史资料的问题。

`app.notification_digest.queue_due_digests(limit=20, at=None) -> int` 只读写数据库，不访问网站、模型或收件服务器。每次处理至多 20 个规则/窗口轮次，每轮至多 500 条事件。调度采用规则行锁与 `SKIP LOCKED`；摘要事件、outbox 和摘要项状态在同一事务内提交。超过一次工作预算的事件保留 pending，下一轮继续，不丢弃。

较长摘要分片，每片最多 10 个事件并按约 6 KB 正文预算切分；单条较长原文链接仍完整保留。每片注明第几条至第几条、该窗口总数和原文链接，避免把分片误认为整日总量。分片事件的 dedupe key 来自固定摘要项 ID 集合，重复调度不会产生第二次 outbox。

中央 Worker 已接入原 `app.deliver_notifications`：先调用 `queue_due_digests()`，再调用 `process_outbox()`；复用原每 15 秒的 notifications 队列调度，没有新增竞争的 Beat。投递使用现有邮件/通用 Webhook/飞书实现，通用 JSON 带 `schema_version: 1`，摘要含结构化 `digest.items`。定时摘要不使用 LLM。

## 修改配置与投递风险

- 修改规则的筛选、模式、时间或通道，以及停用规则，会取消尚未汇总的摘要项及该规则已生成但未发送的摘要 outbox。只改规则名称不取消内容。取消原因与规则操作保留记录，后续新事件采用新配置。
- 修改通道配置会取消该通道的 pending 摘要项与 queued/retrying 投递；不会取消其他通道对同一业务事件的投递。
- 通道版本戳在摘要生成及发送前分别核对，避免旧资料悄悄转发到新 URL/收件人。正在发送的请求无法撤回，继续使用认领时已经读取的旧目标配置。
- 通道版本已经变化的旧摘要，人工重试接口也会明确拒绝，避免把“重发”隐式变成“转发至新目标”。
- 摘要沿用现有投递策略：明确连接失败或 429 可有限重试；读写超时、进程中断或不确定接收结果进入 unknown，不自动重复。人工重试 unknown 仍需确认重复风险，事件 ID/Idempotency-Key 保持一致。
- 长期运行的采集事务会重新读取规则与通道的当前配置，避免 ORM 旧对象缓存继续按已修改规则发出通知。

## 数据库与接口

新增模型字段：`NotificationRule.delivery_mode / digest_time / timezone`；新增表 `notification_digest_items`。迁移由主工程追加到新文件 `520dc9cf3d04_scheduled_notification_digest.py`，既有行通过临时默认值回填为 `immediate / 09:00 / Asia/Shanghai` 后移除服务端默认，没有改写任何已执行旧迁移。本轮独立库最终迁移头为 `3c96c0157f34`。

接口增量：

- 规则 POST/PUT 接受 `delivery_mode: immediate | daily_digest`、`digest_time: HH:MM`、`timezone: Asia/Shanghai`。
- `GET /api/v1/notification-digest-pending`：待汇总窗口。
- `GET /api/v1/notification-deliveries/{id}`：投递正文与摘要证据。
- 投递列表仅返回摘要元信息，完整事件和链接在详情按需读取；Webhook 地址、签名密钥和通道版本戳不向详情暴露。

## 实际验证

环境：项目 Conda `hr-research-dev`（Python 3.11.13），独立 PostgreSQL 16.15 数据库 `hr_research_digest_qa_test`。测试使用 MockTransport 和原有模拟 SMTP，没有真实外发、真实平台登录或 LLM 请求。本轮没有在本机调用 Docker，也没有操作远端部署。

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check backend/app/notification*.py backend/tests/test_notification_digest.py
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python .runtime/run_digest_qa.py
```

结果：**49 passed**（19 项摘要验证、30 项原通知回归），Ruff 通过。测试包含：

- 北京时间跨午夜边界、次日发送时间、尚未到时不生成及重复扫描不重复。
- 业务事务回滚、源事件去重、无规则/停用规则/首次启用不补旧历史。
- 条件过滤、每日模式不受即时冷却丢事件。
- 两个摘要调度器并发只创建一条 outbox。
- 多轮有界处理覆盖全部事件、连续分片范围、长链接完整保留。
- 规则改模式/改时间取消旧窗口，长期事务重新读取新配置。
- 通道版本变更阻止旧摘要投递、API 修改目标主动取消、其他通道保持不变。
- 摘要重试复用事件标识，读超时进入 unknown 后不自动重投。
- API 时间/时区验证、pending 窗口与详情证据、秘密不出现在响应。

独立库完整迁移及 `alembic check` 通过。前端 `npm run build` 通过（含类型检查）。界面功能在本轮通过构建和 API 回归验证；集成冻结后的浏览器走查及远端部署应另行记录，不能沿用旧部署截图宣称新增摘要已在旧容器中运行。
