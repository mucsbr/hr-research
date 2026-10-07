# 代码维护发布、恢复与回滚：独立 E2E 验证

日期：2026-10-05。最近实际执行的四场景 E2E 为 **701 阶段的 `a1c51a3e` / 源 `8f6bf3db…`**，场景全部通过，模型为 Mock。当前线上已是 [最终 711 版本](FINAL_711_DEPLOYMENT.md) / 源 `15bd8bc…`，本轮没有重跑这些 E2E。唯一真实模型试验 [因输出截断未通过](LIVE_CODE_REPAIR_VALIDATION.md)，不能用本文件的历史 Mock 结果代替真实模型质量验收。

新增 driver：`scripts/verify_code_maintenance_e2e.py`。验证期间未修改冻结产品源码；最终通过后依照授权更新本项目主栈与 Runner 控制网络。

## 701 冻结版本：同源真实执行通过（历史）

新增公司名称搜索客户端以及统一的原生详情 URL 归属门禁后，主工程重新完成 701 项回归，使用全新 `a1c51a3efa2a4f0fbe97e40de909dd94` 运行四场景，不复用历史任务或数据库。

| 项目 | 最新结果 |
| --- | --- |
| 后端/适配器源码 SHA | `8f6bf3dbd7de297ce86c0b02d6309a73356f93c3f627aadb7755038ef4a5782b` |
| A 发布与恢复 | release `3844a4c4-5e3e-4820-8a1f-48509583baf9`；SourceRun `c36e909d-5ddd-4842-bd91-c06275bb2f2c`；实际新增 1 条 observation |
| B 发布与恢复 | release `13fddd9c-80b7-4f3d-a146-83589cd0e208`；SourceRun `c2387c57-6a35-4565-ae6d-d8c9542558c4`；实际新增 1 条 observation |
| 坏候选 | 两次真实候选执行、签名回执重验和第一方 oracle 拒绝；B 指针/代次不变、无恢复任务 |
| 回滚后再采集 | SourceRun `cf660bbc-1ff1-49df-941b-bbe2f44a25e9`；实际使用旧 bundle/lock/BS4 4.13.4，再新增 1 条 observation |
| 影子边界 | 三轮正式表行数与全行摘要未变化 |
| 调用 | Mock 4 次、真实 Worker → 内部 HTTP 6 次；真实 LLM、通知、平台登录均为 0 |
| 环境隔离 | QA 前后原有 18 个容器 ID/镜像/StartedAt 不变；QA 已 down，数据库卷和证据保留 |

完整证据在 `.runtime/code-maintenance-e2e-a1c51a3e/`，远端 `/home/dabing/hr-code-e2e-20261005-a1c51a3e/`。同一源码还通过 `.runtime/search-api-e2e-0a79bbb7/` 的实际 FastAPI → SearXNG → 专用库候选/Audit 验收。

最新主栈 backend 镜像为 `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157`，镜像内源码实际复算与上表 SHA 完全一致。主栈使用三份 Compose，迁移保持 `7c8ba429a6ea`、Alembic check 无漂移；Runner/Search 网和页面结果见 [部署记录](CODE_RUNNER_DEPLOYMENT.md)。本轮主库公司、来源绑定全行摘要未变化，没有导入 QA 目标。

## 前一冻结版本：真实执行通过（历史）

程序详情采集新增最终 URL 岗位归属检查后，主工程完成 644 项回归，再以全新 run_id `dd39b8ca3fba48c5a43c842f9e49b428` 执行；没有续用旧数据库或旧任务。

| 项目 | 最终值 |
| --- | --- |
| 源码 SHA-256 | `48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96` |
| driver SHA-256 | `529386e10880825bd59ecb8c424cd229f256770fab83f9d1bf3d4b7d18070c9c` |
| QA 项目 | `hr-code-e2e-20261005-dd39b8ca` |
| QA 迁移 | `7c8ba429a6ea` |
| A 发布与实际恢复 | release `a12b7b7e-76f7-4f2e-bc41-0c1687ddcbc3`；SourceRun `44838328-f538-403b-bafb-68125e123b76`；BS4 4.13.4 |
| B 发布与实际恢复 | release `f852b469-a5db-4090-80c7-744a9dee94d5`；SourceRun `271ec293-c816-4f81-8115-c276632da557`；BS4 4.13.5 |
| 坏候选 | 两次真实隔离执行、签名重验和第一方 oracle 拒绝；原版本和 generation 保持 B，没有恢复任务 |
| 回滚后真实采集 | SourceRun `4dd5018e-0b73-4329-b1f5-c63f93e1b4d9`，使用 A bundle/旧 lock/BS4 4.13.4，再新增 observation |
| 影子不落业务表 | 三轮正式表行数与全行摘要全部不变 |
| 调用与隔离 | 真实 Worker → 内部 HTTP 6 次；Mock 模型 4 次；真实 LLM、通知、平台登录均为 0 |

A/B 的 bundle 和 lock 与首轮所列相同，但最终轮分别新建了真实提案、验证运行、发布、恢复及回滚记录。每次恢复和回滚后采集均新增 1 条 observation。QA API/Worker 的源码指纹与冻结清单一致，所有候选只在独立 Runner 中执行。运行前后的原 16 个容器保持相同 ID、镜像与启动时间。

最终 QA 三个容器及数据网络已移除，PostgreSQL 卷和文件保留用于审计。本机完整证据位于 `.runtime/code-maintenance-e2e-dd39b8ca/`；远端位于 `/home/dabing/hr-code-e2e-20261005-dd39b8ca/`。

随后主栈新 backend 镜像 `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89` 内的 `backend/app`、迁移与 `adapters` 文件实际复算结果与上表源码 SHA 完全一致。主栈迁移、API/Worker 控制网络、匿名拒绝、队列 smoke、只读维护 UI 的结果见 [控制网络与最终部署记录](CODE_RUNNER_DEPLOYMENT.md)。主栈没有导入 QA 提案、岗位或登录状态。

## 首轮真实执行结果（历史快照）

- 独立 QA 项目：`hr-code-e2e-20261005-ab6b2f03`；run_id：`ab6b2f03ce144bcb8ce742bdfd87d555`。
- 源码 SHA-256：`e9ffa12df89f27237843c544d3e4889db8f02324efa15cac6b5c1e7ba78f168a`。
- driver SHA-256：`529386e10880825bd59ecb8c424cd229f256770fab83f9d1bf3d4b7d18070c9c`。
- QA 数据库迁移：`7c8ba429a6ea`；PG、Redis、supervisor/API/Worker 均为本轮独立实例。
- 真实 Runner runtime：`sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362`。
- QA 运行在已有不可变 backend 镜像 `sha256:c1e39759b7ca0e60b6f7b8e25e5b2bd76953d0bfa2a3fcbcaedf181c516e8650` 的依赖层上，以只读挂载加载冻结源码；两者 requirements.lock 摘要完全一致。此项是代码业务链路验证，后续新业务镜像和主栈控制网络另行验证。

| 场景 | 真实结果 |
| --- | --- |
| 原生失败 → 程序 A | 自动建提案、4 个 Broker case 通过、CAS generation=1、实际恢复入库，新增 1 条 observation |
| 程序 A 失败 → 程序 B | 真实程序失败证据、4 个 Broker case 通过、CAS generation=2、实际恢复入库，新增 1 条 observation |
| 坏候选 | 两次真实候选均经签名回执重验；故障 case 失败、回归 case 通过；耗尽预算后 B 指针和 generation 不变，没有恢复任务 |
| 回滚后采集 | CAS 回滚 A 后独立新任务完成，真实使用 A bundle/lock 和 `beautifulsoup4==4.13.4`，新增 1 条 observation |

三轮影子阶段的正式业务表全行摘要均未变化。整轮真实内部 HTTP 6 次、Mock 模型 4 次；真实 LLM、通知、平台登录均为 0。候选只在独立 Runner 中执行。原有 16 个运行容器的 ID、镜像和 StartedAt 全部不变。

A release：`a96361e9-b121-4e08-b88d-e04ca6819dca`，bundle：`06575e824f9b1e38cd9bce396eedf07e09b5328fd66c0d393ff6faad70cb0f10`，依赖 `beautifulsoup4==4.13.4`。B release：`e70f30e5-0073-452b-819d-104aea089de5`，bundle：`cc46505a3519a3a5ae3a529ba8af4a19fc72f1a078bf30cc0012c441bda0c7ea`，依赖 `beautifulsoup4==4.13.5`。回滚后 SourceRun：`c7af0c78-1f77-491e-8816-606c48dc018d`。

本机证据目录：`.runtime/code-maintenance-e2e-ab6b2f03/`，包含冻结清单、完整 summary、运行摘要与模型/内部 HTTP/网站 transport 日志。远端证据：`/home/dabing/hr-code-e2e-20261005-ab6b2f03/`。本轮 QA 三个容器及专用数据网络已移除；QA PostgreSQL 卷和文件保留用于审计，未执行删除卷。主业务镜像、主数据库和主控制网部署保持未变。

## 要证明的链路

```text
真实原生采集失败
  → 第一方 failure oracle / 自动提案
  → Mock 模型输出真正源码/依赖变更
  → 真实 Broker 隔离验证
  → 第一方比较 / 影子不落正式业务表
  → Registry generation CAS 发布
  → 新恢复 Task 经真实 Celery Worker 和内部 HTTP
  → 真实浏览器读取 fixture / 新 program / 第一方校验
  → ingest / JobObservation / recovered
```

另验不兼容代码候选不改变活动指针，以及手动 CAS 回滚后**下一次实际采集**确实使用旧 bundle、旧依赖环境并入库。不会只检查“回滚按钮返回成功”或伪造 SourceRun 计数。

## 真实组件与仅有的两个替身

| 组件 | 方式 |
| --- | --- |
| PostgreSQL | 专用 QA 数据库，使用冻结产品迁移；不建 SQLite 替代业务库，不写现有主库 |
| Redis / Celery | 专用 Redis，独立真实 Celery Worker；调用真实 dispatch_pending/dispatch_maintenance 任务 |
| API / 内部 HTTP | 独立真实 FastAPI 进程；Worker 通过真实 TCP 请求 `/api/internal/browser/collect`，保留 task/attempt/HTTP 状态证据 |
| 浏览器 | 真实 Playwright/Chromium、真实 DOM、响应回调与第一方采集逻辑 |
| 外部网站 | 唯一替换 DNS/HTTP 传输：严格限定的 `www.zhipin.com` fixture 路径由 route.fulfill 提供；没有外部岗位请求、真实账号或真实登录 |
| 模型 | 仅 Worker 里的 `adapter_code_service.ModelGateway` 换成受控响应；真实 key 为空、daily limit=0，Mock 调用单独留痕，整轮最多 4 次 |
| 代码候选/依赖 | 只在真实 Broker 容器执行，使用真正批准 wheel、锁和签名回执；driver/API/Worker 不 import/exec 候选 |
| 校验、发布、恢复、入库 | 第一方实际实现，不注入预先成功的输出、expected、release 指针或 recovered 状态 |

可信 supervisor、API、Worker 可作为**独立 QA 控制容器中的三个真实进程**运行；它们不进入现有主栈。该控制容器通过专用 bridge 访问 Broker，PostgreSQL/Redis 在独立 QA 数据网络中。此方式验证业务链路；生产主栈真实 API/Worker 的控制网接入仍另用 `verify_control_network.py` 验证，不混为一项证据。

## 四个顺序场景

1. **建立可回滚旧代码历史**：原生解析器读取仅有 `.jd-v1` 的真实 DOM 后失败；代码维护自动建提案。Mock 修改生产 `adapters/programs/adapter.py` 的详情解析分支，新增 `.jd-v1`，并锁定 `beautifulsoup4==4.13.4`。真实验证、自动 CAS 发布和新恢复任务完成，形成旧程序 A 的真实发布历史。
2. **旧程序失败后升级**：fixture 改为 `.jd-v2`；实际程序 A 失败。Mock 在 A 上追加 `.jd-v2` 并改为 `beautifulsoup4==4.13.5`。真实验证、发布 B、恢复采集、第一方校验、入库后，事件必须达到 recovered。
3. **坏候选保持旧版**：fixture 改为未支持的 `.jd-v3`；Mock 返回有效 Python、但并未修复问题的源码修改，让它实际进入 Broker 验证并失败。最多两个候选；活动 release_id/generation 必须仍为 B，不创建恢复任务，正式业务数据保持不变。
4. **手动回滚后再采集**：带 B 的 expected_active_release_id / expected_generation 调回滚 API，切回曾正式发布的 A；网站 fixture 恢复 `.jd-v1`，新建独立真实采集任务。必须验证 SourceRun 的 bundle/lock/fingerprint 指向 A，依赖为 4.13.4，实际 Broker 回执经第一方验证，新增 JobObservation 且描述来自当前 fixture。

这样不需要 driver 手工伪造一个“已发布旧版”。旧版和新版都通过真实验证与发布链产生。

坏候选负对照还必须用满两次候选预算，逐条重验两轮真实 Broker 签名回执，并重算第一方 oracle 报告：原始故障 case 失败、原有回归 case 通过。只有 `failed` 展示状态不构成通过；基础设施故障、未知结果、签名错误或生成失败都会停止验收。

## 影子数据边界

代码模型和自动发布是独立开关。每一轮先开 code_agent_enabled、关 code_auto_publish_enabled，旧 JSON 修复的 agent_enabled/auto_repair_enabled 关闭。

Mock 模型在响应前等待一个测试屏障，仅模拟模型延迟。原采集失败提交后，driver 先记录正式表的**行数和全行摘要**，才释放模型响应。候选到 validated / awaiting_publish 后再次比较：

- jobs、job_source_records、job_versions、job_observations、job_change_events、raw_documents；
- notification_events、notification_deliveries。

必须完全相同。提案、验证回执、TaskEvent、审计记录可正常增长。通过后，driver 只通过真实设置 API 开启 code_auto_publish_enabled，由正常维护调度完成 CAS 发布与恢复，不直接调用 ingest 或写 recovered。

## 已对齐接口

- 自动编排：`record_code_failure`、`queue_code_from_failure`、`tick_code_maintenance`、`prepare_code_recovery`、`finish_code_recovery`，位于 `app.adapter_code_orchestration`。具体 Worker/collector hooks 仍以主工程冻结版本为准。
- 生成/验证：`app.adapter_code_service.generate_code_candidate`、`advance_code_validation`。
- 复合发布：`publish_code_proposal(..., expected_active_release_id, expected_generation)`。
- 手动回滚：`POST /api/v1/adapter-maintenance/releases/{id}/rollback`，携带两个 CAS 参数。
- 恢复证据：`SourceRun.counts.code_execution` 的 release_id、release_generation、fingerprint、bundle_sha256、lock_sha256、runtime_sha256、verified、run_ids；再逐项验证 `AdapterProgramExecution` 保存的原始签名结果。
- 详情 runtime 的 invoke 返回 `(doc, navigation)`；driver 不替换该路径或把它当成单 doc。

failure oracle 由第一方从失败 DOM/公开字段独立生成；driver 不给自动提案写 expected。Mock 仅提交有限源码替换与依赖版本。

## 冻结门禁

默认 `--role prepare` 只读源码并写准备清单，**不连数据库、Redis、网站或 Broker**。它检查编排函数以及 Worker、internal HTTP、collector 的关键 hooks，并计算实际源码 SHA-256。

```sh
# 本机仅项目 Conda；这一步不是执行 E2E。
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python scripts/verify_code_maintenance_e2e.py \
  --role prepare \
  --freeze-manifest .runtime/code-maintenance-e2e/prepared.json \
  --runtime-digest sha256:经live检查的Runner镜像ID
```

只有根确认 collector/hooks 已冻结，且 pending_hooks 为空后，才用 `--freeze` 生成 frozen 清单。API、Worker、supervisor 每次启动都会重新核对源码及 driver 自身指纹；不一致则拒绝运行，不用旧测试 expected 测另一版本。

模型 patch 使用生产源码中的唯一详情解析锚点。第一方 failure oracle 冻结后还需核对其规范 selector 与 `.jd-v1/.jd-v2` 一致；若需调整，只改 Mock 的源码提案，不改 oracle 预期。

## 独立环境配置

由授权远端的独立部署流程生成私有 QA 配置，**不复制开发机 `.env`、主库密码或主应用数据卷**。Runner token 仅在远端私有文件之间处理，不写入日志或结果。

| 配置 | 必要边界 |
| --- | --- |
| HR_CODE_E2E_RUN_ID | 新的 32 位十六进制 QA run_id |
| DATABASE_URL | 主机必须为 `code-e2e-postgres`，库名必须精确为 `hr_code_qa_<run_id前12位>` |
| REDIS_URL | 主机必须为 `code-e2e-redis`，独立实例 |
| APP_ENV | development；真实 Runner 网络需可用，同时数据库/预算门禁严格限制到 QA |
| APP_URL / BROWSER_SERVICE_URL | `http://127.0.0.1:18091`，仅独立 QA 容器内进程使用 |
| STORAGE_DIR / BROWSER_PROFILES_DIR | 必须在带当前 run_id 的独立 state-dir 下 |
| BROWSER_CACHE_DIR | 固定业务镜像提供的 `/opt/playwright` |
| APP_SECRET / ENCRYPTION_KEY / ADMIN_* | 仅 QA 使用的新随机值；空浏览器状态仅为明确的测试账号，不代表真实登录 |
| ADAPTER_RUNNER_* | 真实专用 bridge URL、私有 token、live 核对后的 runtime digest |
| LLM_API_KEY / LLM_DAILY_REQUEST_LIMIT | 空 / 0；不配置真实模型连接 |
| SMTP_HOST | 空；不建立真实通知通道 |

先对专用库执行冻结产品的真实 Alembic 迁移。driver 检查新代码维护/执行表存在，并在该库写入唯一 QA marker，绑定 run_id 和源码 SHA；不符合身份的数据库会拒绝写入。脚本没有 DROP DATABASE、TRUNCATE 或清空主表动作。

冻结后可在可信 QA 控制容器中执行：

```sh
python -B /qa/scripts/verify_code_maintenance_e2e.py \
  --role supervise \
  --freeze-manifest /qa-plan/frozen.json \
  --state-dir /qa-state/本轮run_id
```

首轮和最终修复版均已按上述方式执行并通过，各自使用新的 run_id 和冻结清单。后续源码变更同样需要独立新身份。独立 PG/Redis/控制容器的启动与清理限定本轮 QA 项目，不修改现有主服务或 Docker 全局配置。

## 证据与失败行为

- `state.json`：原 batch/task/incident/proposal/release 引用、提交幂等键、阶段状态、影子表摘要。
- `mock-model.jsonl`：Mock 目的、关联 task、输入/补丁摘要；不记录秘密，不调用 ModelGateway 网络，整轮最多 4 次。
- `website-transport.jsonl`：已 fulfillment/拒绝的固定 fixture 路径，外部网络始终 false。
- `internal-http.jsonl`：真实内部 HTTP 的 task_id、expected_attempt 和响应状态。
- `processes.json`：可信 supervisor、API、Worker 的不同 PID。
- `summary.json`：只有全部断言通过才写 passed；包括实际 SourceRun 证据、Broker run_ids、依赖、持久 observation 数、回滚后新任务结果。

提交意图先落盘；恢复时按原幂等 key/DB 记录找回任务，查不到就停为 incomplete，不盲目重交。发布或回滚响应丢失时先检查实际指针，不自动创建另一版本。错误不会被当作“不兼容候选通过负对照”；未知执行与基础设施错误应停下保留原证据。

等待期间遇到 `needs_engineering`、`unknown`、`conflict`、配置阻塞、登录/验证要求或非预期终态，会立即保存 `wait_failure`（阶段、对象 ID、状态、错误码），不继续等到通用超时。原采集预期失败、坏候选预期校验失败仍按对应阶段的验收条件处理。

最终和历史源码的结果分别记录在文首，主业务新镜像、迁移与控制网络有独立部署证据，不沿用旧源码结论。
