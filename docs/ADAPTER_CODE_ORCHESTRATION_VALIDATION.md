# 自动代码维护编排：实现与定向验证

验证日期：2026-10-05。此文档对应代码故障事件、生产程序目录、发布后单来源恢复的定向验证；不代替最终全栈或远程 Docker 验收。

## 当前已实现边界

- `backend/app/adapter_code_catalog.py` 维护服务端生产基线与独立真值；生产源码来自 `adapters/programs/adapter.py`，不是 `adapters/code/` 中的 QA 示例。
- BOSS、猎聘、智联、前程无忧、官网均具有列表、详情、导航的登记回归。官网额外包含原生 JSON-LD、已保存 CSS 计划，以及腾讯固定公开接口的原始列表、原始详情、已验证详情、API 分页。智联包含 `verified_details[].jobDesc`；前程无忧包含第一方观察链接对应的 tracking rows。
- 模型只接收安全、定长、有摘要的第一方资料。客户端或模型提供的 `expected` 不进入真值；无法独立确定字段语义时保留工程入口，不调用模型猜测真值。
- 官网页面包含已核验岗位标题时，职责 oracle 选择唯一、至少 30 字且具有明确职责/要求语义的原文区域。若存在两个独立长职责区域则拒绝，不将共同父节点误当成唯一真值。
- 腾讯岗位编号来自原始 `PostId`，详情请求与响应编号必须一致；实际 `ComName` 作为招聘主体保留，不把子公司改写成“腾讯”，也不凭模型补充公司关系。HTML 标量必须先按原始 Claim 校验，再由第一方提取文字。

## 编排与权限

`adapter_code_orchestration.py` 对外提供：

```python
record_code_failure(...)
queue_code_from_failure(...)
queue_code_from_upstream(...)  # queue_code_from_check 为兼容别名
tick_code_maintenance(...)
code_tick_due(proposal, registry)  # 纯函数，不查数据库
on_code_published(...)
prepare_code_recovery(...)
finish_code_recovery(...)
reconcile_code_recoveries(...)
```

`code_agent_enabled`、`code_auto_publish_enabled`、`code_upstream_enabled` 各自默认关闭。旧 JSON 规则维护权限不隐含可执行代码权限；代码维护启用时可以独立于旧 JSON 开关创建事件。

JSON 与代码维护复用同一原始故障事件、维护任务与最多两个候选槽位。模型结果未知、未完成或基础设施失败时，不生成另一个候选来掩盖未知结果。固定上游仅使用登记仓库/路径及固定提交差异，按目标版本与生产代次去重。

候选验证通过后，自动发布关闭时停在 `awaiting_publish`；管理员可显式发布。真实、有效的管理员显式发布可授权该事件的一次恢复，而不需要同时打开自动发布开关。管理员身份在恢复前重新核对。

## 原来源恢复与发布并发

发布和恢复创建处于同一事务。恢复配置仅包含原失败来源，删除原任务 `adapter_versions` 和旧 `resume_from_task_id`，写入本次 release ID、generation 与完整 fingerprint。一个事件最多创建一个恢复任务；不把恢复失败再次升级为新付费维护事件。

发布和 prepare 采用 registry → incident → 原 Task → Binding → Company → Subject → Account 的校验锁顺序。后半段使用 `skip_locked`，避免与 Worker 已持 Task → Binding 的事务形成等待环。已取得的来源行锁保持到事务提交，来源编辑不能从“检查完成到发布提交”之间穿过。

发布阶段临时 `source_configuration_busy` 或 `code_event_busy` 回滚当前发布事务并保留原候选阶段，下轮只重试发布，不增加模型调用。实际来源变化在下轮预检中拒绝。

恢复 prepare 阶段保留严格的一次认领与 Task attempt fencing：如果恰好遇到来源锁竞争，本轮可以记录明确失败；不会倒退 attempt、自动再次抓取、重新付费，也不会因这种竞争回滚生产代码。此处暂未实现复杂等待重排状态，是本轮主动保留的限制。

恢复成功必须同时满足：

1. 原来源确有成功或部分成功的结果，且有真实 `JobObservation` 写入。
2. 结果中的 code execution 引用匹配本次 release、generation、完整 fingerprint、bundle、lock、runtime。
3. 每个远程 run ID 对应本任务、本来源、本 Binding、本 attempt 的 accepted ledger，且请求、输入、bundle、operation 摘要一致。
4. 通过真实第一方 Runner Client 重新验签，并核对依赖环境摘要；至少包含列表和详情操作。

单纯伪造 `counts.observed` 或 `code_execution.verified` 不能宣告恢复成功。取消任务即使尚未进入 Worker、或只设置 `status=cancelled`，补偿扫描也会结算事件；取消不需要连接 Runner。

## 本轮定向验证

环境为新建 Conda 环境 `hr-research-dev`；专用本机 PostgreSQL 数据库为 `hr_research_code_orchestration_test`。未复用应用数据库或其他代理的测试库。未调用本机 Docker，未在本机导入或执行候选源码。

以下 4 个文件合计 **77 passed，6.75s**，随后关联文件 Ruff 结果为通过（同一轮执行中 Ruff 先运行）：

```text
backend/tests/test_adapter_program.py
backend/tests/test_adapter_code_catalog.py
backend/tests/test_adapter_code_orchestration.py
backend/tests/test_official_code_worker.py
```

测试范围包括：

- 服务端 oracle 忽略客户端 expected、原始字段与官方身份边界、职责区域歧义拒绝。
- 真实 `create_batch` 来源快照、代码权限独立启用、旧 JSON/新代码共享候选预算。
- 原始公司别名、来源 adapter、URL、停用状态变更阻断。
- 自动发布、显式管理员发布、单来源恢复配置与幂等性。
- 真正 HMAC 签名结果经第一方 Client 验签，ledger 与真实 `ingest`/Observation 联合判定恢复；缺少 Observation 的相同签名结果不能成功。
- 取消、等待登录、等待验证、失败等终态的补偿结算。
- 两个真实 SQLAlchemy Session 证明原 Task、Binding、Company、Subject 行在发布提交前不能并发写入；对方已持 Task/Binding 时不会死锁或穿过发布。
- 暂时锁竞争后复用同一已验证候选发布，模型调用数仍为一。
- 真实 Worker 从本地受控官网原始采集失败创建代码事件与候选，全程仅两次原页面读取、零模型调用。

Runner 使用签名传输模拟，模型使用 MockTransport；这些测试不声称候选在隔离容器里实际运行。真实远程组件验证另见 [ADAPTER_PROGRAM_VALIDATION.md](ADAPTER_PROGRAM_VALIDATION.md) 与 [CODE_RUNNER_VALIDATION.md](CODE_RUNNER_VALIDATION.md)。完整 API → Worker → Broker → 发布 → 恢复的远程 E2E 由独立验收文档记录。

## 本轮自主决策，供事后审计

1. 用生产基线目录和服务端独立真值建立候选，不直接把 QA 演示源码发布为产品适配器。
2. 以明确职责语义与唯一原文区域建立 DOM 真值，门槛为 30 字；多区域或新字段语义不可证明时不做付费推断。
3. 三个代码权限与旧规则权限隔离，默认全部关闭；有效管理员显式发布保留人工恢复授权路径。
4. 发布锁竞争可以重试同一候选；恢复认领的锁竞争暂不放宽一次执行与 fencing，不为了可用性倒退 Task attempt。
5. “恢复成功”要求真实业务 Observation 与完整验签 ledger，不能仅以报告计数或远程容器退出成功为依据。

以上均为实施方在既有需求范围内选择的具体实现，未要求用户先做小范围业务试点，也未把测试模拟结果写成真实远程验收。
