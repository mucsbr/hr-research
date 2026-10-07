# 适配器维护实现与验证

已补上实际采集失败驱动的自动修复、发布与一次恢复，完整 Worker/内部接口验证见 [AUTO_MAINTENANCE_VALIDATION.md](AUTO_MAINTENANCE_VALIDATION.md)。

日期：2026-10-05。实现范围为 BOSS、猎聘、智联、前程无忧四种平台的受限 JSON 采集契约。官网 CSS 规则的已有再适配流程保持独立。

## 实际闭环

1. 注册当前内置 manifest，保存规范 JSON、版本、SHA-256 指纹和发布记录；默认关闭每日上游检查与维护模型。
2. 手动提交或显式调用维护模型生成候选 JSON，保存为独立 candidate release，不覆盖活动配置。
3. 严格检查允许字段、路径深度、响应路径模式与 CSS 选择器；拒绝额外执行命令、Python/Shell 字段、全页/混合全页选择器与复杂正则。
4. 在 ContextVar 隔离上下文里执行内置脱敏字段契约，包括岗位名称/薪资/地点/学历、公司 ID 不匹配排除、导航秘密不留存、职责选择器与未知响应。影子验证不读取真实站点，不写岗位、观察或通知。
5. 验证通过后，发布操作在数据库事务和注册表行锁下切换 active release；发布前检查候选内容指纹与所基于的活动版本仍一致。
6. 回滚只能指向已经发布过、指纹及验证仍一致的历史版本。版本、验证失败、发布与回滚记录保留。

普通采集通过 `platform_collectors.contract(kind)` 读取已发布配置。`collect_platform` 在入口读取一次，并在整次采集内冻结该 JSON；响应、HTML 列表与详情解析不会中途切换刚发布的新版本。实际指纹通过 `CollectionResult.adapter_fingerprint` 返回，由中央 Worker 放入 SourceRun 的版本与比较范围。

数据库未配置时可以使用明确的内置默认；纯解析单测可以通过 `use_manifest` 显式注入配置。有数据库但版本库不可用、活动指针无效、指向其他平台或内容被修改时会明确失败，绝不静默回退。

## API

所有接口需要管理员权限，前缀 `/api/v1/adapter-maintenance`。

| 方法与路径 | 用途 |
|---|---|
| `GET /adapters` | 内置/已登记平台、活动版本、检查与模型开关、是否存在固定上游 |
| `POST /register` | 幂等注册四种内置固定版本 |
| `PATCH /adapters/{kind}/settings` | 设置 `check_enabled`、`agent_enabled`；默认均为 false |
| `POST /adapters/{kind}/check` | 明确触发一次只读上游检查，返回维护任务 ID |
| `GET /adapters/{kind}/releases` | 版本及验证记录 |
| `POST /adapters/{kind}/candidates` | 提交 `{manifest: ...}` JSON 候选 |
| `POST /releases/{id}/validate` | 执行隔离影子契约测试 |
| `POST /releases/{id}/publish` | 原子发布已经验证的候选 |
| `POST /releases/{id}/rollback` | 回滚到曾经发布过的版本 |
| `POST /adapters/{kind}/generate` | 显式调用已启用的维护模型；参数见下文 |
| `GET /runs`、`GET /runs/{id}` | 维护任务、结果与失败原因 |

`generate` 输入为 `failure_description`、`max_candidates`（默认 1，最大 2）、`auto_publish`（默认 false）。平台开关未启用时不会调用模型。`auto_publish=true` 时，最多两次生成尝试后自动完成验证和发布；验证失败、模型失败、活动基线变化时保持旧版本。返回明确的 `published` 与 `published_release_id`。

生成过程仅使用统一 `ModelGateway`，有独立 TaskRun、网关预算与调用记录；没有隐藏重试或模型切换。模型不能生成或执行 Python、Shell、浏览器任意脚本，也不能控制网络目标。

维护 TaskRun 设置开始时间、心跳与 attempt，进程中断可由中央 stale 扫描标记失败；模型调用前、返回后和发布前都会检查取消或停止状态。取消后不再发布候选。

## 调度契约与上游范围

- `enqueue_due_checks(db)`：只为用户已启用、到期的 registry 创建 queued 维护记录，下一次推迟一天，返回任务 ID 列表；调用方提交事务。
- `execute_check(run_id)`：幂等认领 queued 记录，执行上游检查并保存结果；由中央维护队列处理。
- 当前唯一固定 GitHub 上游是 BOSS 内置参考仓库 `eatmoreduck/boss-zhipin-scraper`。只读取固定 REST 元数据路径，不下载或执行仓库代码，不发 Issue/PR。
- 猎聘、智联、前程无忧没有登记固定 GitHub 上游，返回 `not_configured`，不搜索随机开源仓库或执行未知 CLI。
- 仅观察到上游提交 SHA 改变，不等于采集契约发生了可自动修复的变化。没有可验证 JSON/schema 变化证据时记录 `needs_engineering` 并保持当前版本；每日检查不会因此盲目调用 LLM。
- 模型自动发布能力是显式请求下的闭环；目前没有足够证据自动把任意上游代码改动映射为安全配置，该限制保持可见。

## 数据模型与中央集成

- `maintenance_models.py`：`AdapterRegistry`、`AdapterRelease`、`AdapterMaintenanceRun`。
- `adapter_maintenance.py`：注册、安全 schema、影子验证、活动配置、发布/回滚、检查与模型候选。
- `maintenance_api.py`：管理员接口。
- 主任务负责中央路由、Beat/维护 Worker 和迁移 `b82644ab8bac`。活动版本的循环 FK 采用先建 registry/release，再创建命名约束的迁移顺序。
- 版本范围指纹由实际执行结果返回；运行中的公司任务不会因为适配器刚发布而混用两个版本。

## 验证证据

- `test_adapter_maintenance.py` 共 26 项：四平台固定注册、默认关闭、危险 JSON/CSS/正则拒绝、影子不写正式数据、发布/回滚、坏字段失败、持久失败记录、指纹变更拒绝、并行版本冲突、DB 故障无回退、显式无 DB 注入、采集中途版本冻结、定时默认不联网、固定上游变更保持现版、未知上游不联网、模型最多两次、默认只生成候选、显式自动发布成功与失败保留旧版、跨平台活动指针拒绝、生成时取消或任务停止均阻止发布，以及心跳恢复字段。
- 联跑维护、BOSS/猎聘契约、新渠道、公司管理四个测试文件：**78 passed in 3.12s**。使用独立 `hr_research_recruit_test` 数据库和实际最新版迁移。
- 运行 Ruff 检查通过。
- 模型使用 `httpx.MockTransport`；上游检查使用受控 Fetcher fixture。本轮真实 LLM 请求 0，真实 GitHub 请求 0，没有本机 Docker 调用或重启主服务。
- 内置影子 fixture 证明既有契约兼容与安全边界，不证明任何新网站布局的真实可用性。新站点变化仍需补充有界公开/登录后证据，不能仅凭绿色测试宣称全平台采集成功。
