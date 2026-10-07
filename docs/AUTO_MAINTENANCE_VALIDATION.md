# 自动维护闭环与验证

日期：2026-10-05。本文对应原计划 P8 的“发现采集异常 → Agent 修复 → 验证 → 恢复”，没有删除或缩减原计划要求来声明完成。

## 当前实际链路

1. `collect_platform` 开始时冻结已发布 manifest 和指纹。只在真实解析失败时附加脱敏 `repair_evidence` 与实际 `adapter_fingerprint`。
2. 内部浏览器接口在错误响应中白名单转交这两个字段。公司 Worker 先保留失败 SourceRun，再在同一事务内排自动维护事件。
3. 只有 `agent_enabled=true` **且** `auto_repair_enabled=true` 才允许进入自动修复。新增开关默认 false，不改变此前“只允许手动模型调用”的权限含义。关闭维护模型会一并关闭自动修复与自动发布。
4. 维护队列根据原始失败证据请求统一 ModelGateway。每个来源运行、适配指纹组合最多 2 次候选尝试，重复错误包装或不同采样不会新增预算。
5. 候选必须同时通过：严格 manifest schema、原有脱敏契约回归、当前失败证据。仅改变版本号不能通过；候选只能保留旧规则并增加被当前证据逐条证明的详情选择器或列表路径，不能夹带未证实的登录账号区域选择器、修改公司字段或扩大来源。
6. `auto_publish_enabled=true` 时，在同一数据库事务内发布并创建一次恢复任务；false 时保留 `awaiting_publish`，管理员可显式发布后恢复。
7. 恢复任务沿用原公司、账号、来源身份与过滤范围，`sources_snapshot` 只包含原失败来源，携带 `expected_adapter_fingerprint`，另有新的 TaskRun/CompanyRun/RunBatch。
8. 正常公司队列执行恢复；源指纹改变时在打开浏览器前停止。`prepare_recovery` 对事件进行行锁 CAS，只允许 `recovery_queued → recovering`，并拒绝 `attempt > 1`，通用 stale 重试不能额外发出第二轮采集请求。
9. 实际重新采集、入库成功后才标为 `recovered`。原失败任务与来源结果保留，不伪装成原任务成功。恢复再次出现解析失败时不再触发模型；若修复版本仍是当前版本，回滚到原已发布版本。

## 不付费修复的情况

- 登录失效、验证码/风控、账号锁、DNS/HTTP/超时等网络失败、公司主体不匹配。
- 管理员未启用自动修复，或者随后关闭开关。
- 解析错误没有可核验的脱敏 DOM/响应结构，或者现有规则已经能解析所提供证据。
- 失败版本已经变化，来源账号/网址/主体已经变化，公司或来源被停用。
- 事件排队后账号已过期或需要人工验证。它不会因为原错误是解析问题就继续付费。
- 原采集、维护任务或事件已取消；维护模型返回后、发布前会再次检查。

这些情况保留原等待状态或记录 `needs_engineering` 与具体原因，不生成“自动修复成功”报告。每日 GitHub 元数据检查仍不会因为提交 SHA 改变而盲目请求模型；自动模型调用来自已证明的采集解析事件。

## 证据边界

### 详情 DOM 改版

当前自动详情修复用于 BOSS/猎聘已经验证过列表公司身份、但详情职责选择器失效的情况。只保留带“岗位职责/职位描述/任职要求”等信号的有限职责子树，保留必要 class/id，删除脚本、注释、表单、输入、导航、页眉/页脚、侧栏和属性中的令牌。

电话、邮箱、可识别的联系人字段、Bearer/API 密钥模式进一步脱敏。最大 DOM 8KB、整个证据包不超过 12KB。页面没有可靠职责区域时不将整页或用户账号区发送给模型。

新选择器必须准确选中该职责子树，且每一条新增选择器都需单独通过验证。不能通过附带一个有效选择器来掩盖另一个未证明的账号/推荐区域选择器。

### JSON 列表路径改版

只处理“原公开岗位字段仍能核对公司和岗位，但列表被移到新路径”的变化。代码先有限搜索候选数组，利用原字段契约核实公司与岗位，再只投影该契约所需的公开字段；招聘者、电话、Cookie、安全导航参数和其他账号数据不进入证据包。

如果字段含义本身改变、无法独立建立公司/岗位真值，或者响应入口不再可访问，状态为 `needs_engineering`。目前没有冒充能自动理解任意未知 schema。

## Worker/API 集成契约

新增实现：

- `backend/app/repair_evidence.py`：有限证据捕获与脱敏。
- `backend/app/auto_maintenance_models.py`：`AdapterRepairIncident`。
- `backend/app/auto_maintenance.py`：排队、双重验证、发布恢复、完成与中断处理。
- `backend/tests/test_auto_maintenance.py`：服务、内部接口与真实公司 Worker 的组合验证。

主任务完成中央迁移、路由、内部错误字段和队列接线；该子任务未编辑 `workers.py`、`browser_internal.py`、`main.py` 或中央迁移。

| Hook | 调用位置与作用 |
|---|---|
| `queue_repair_incident(db, task_id, source_run_id, binding_snapshot, error_code, evidence, base_fingerprint)` | SourceRun 失败持久化后调用；只排事件，不联网、不调用模型 |
| `execute_repair_incident(id)` | 维护队列处理 queued 事件，最多两次候选 |
| `prepare_recovery(db, task_id)` | 恢复 SourceRun 建立后、真实采集前调用并 commit，完成唯一认领 |
| `finish_recovery(db, task_id)` | 公司 Worker 最终状态写入时调用，依据实际来源结果判定恢复 |
| `reconcile_repair_incidents(db)` | 维护调度器处理已被 stale 扫描终止的任务，不自动重发付费请求 |

维护 API 前缀仍为 `/api/v1/adapter-maintenance`：

- `PATCH /adapters/{kind}/settings` 增加 `auto_repair_enabled`、`auto_publish_enabled`。
- `GET /incidents`、`GET /incidents/{id}` 查看事件与脱敏证据。
- `POST /incidents/{id}/cancel` 停止后续生成、发布和恢复。
- `POST /incidents/{id}/publish` 发布已验证候选并排一次恢复。
- 通用 `POST /releases/{id}/publish` 遇到关联的等待发布自动候选时也转入同一个恢复闭环，避免绕过事件关联。

## 验证结果

使用 Conda `hr-research-dev`、专用 `hr_research_recruit_test` PostgreSQL 数据库和实际最新迁移。模型全部为 `httpx.MockTransport`，没有任何真实模型请求；没有本机 Docker 调用或重启主服务。

合并运行以下 4 个文件：

```text
python -m pytest -q \
  tests/test_auto_maintenance.py \
  tests/test_adapter_maintenance.py \
  tests/test_platform_collectors.py \
  tests/test_extended_platforms.py
```

结果：**87 passed in 3.96s**。Ruff 检查通过。

其中自动维护 25 项覆盖：

- 实际详情改版失败捕获、脱敏、Agent 候选、双重验证、发布、新任务、再次采集、入库与恢复完成。
- 独立的真实 `_execute_company` 集成：经实际内部 HTTP 路由返回失败证据，由 Worker 自动排事件，维护后再次进入真实 Worker/内部路由并入库。不是只直接调用服务拼接“成功”。外部平台浏览器、Redis 租约和模型传输为受控替身，业务 Worker、路由、数据库、状态与 ingest 均使用真实实现。
- 登录/验证/网络/身份错误不排模型事件；开关关闭和证据不足不调用模型。
- 同一来源事件去重、最多两次候选、无效候选不恢复。
- JSON 路径移动、公司校验与敏感字段剔除；未证明的附加选择器拒绝。
- 自动发布关闭时等待人工发布；通用发布入口保持恢复关联。
- 取消、版本冲突、账号随后过期、恢复 CAS、第二次尝试拒绝、恢复失败禁止再次修复并回滚。
- 原失败任务独立保留，恢复只运行原失败来源，实际版本指纹改变后才用于新的采集范围。

测试中曾暴露跨 Session 注册表缓存导致恢复误判版本冲突，已改为恢复/回滚前读取最新活动版本，并在完整链路中重新验证。

这些证据证明受支持、可核验的改版场景存在完整自动恢复路径；不代表已经对真实招聘平台发起自动付费修复，也不代表登录后任意布局变化都可自动处理。
