# Python代码包自动维护：生命周期与验证记录

日期：2026-10-05（Asia/Shanghai）。本记录对应P8.4代码/依赖候选，区别于此前仅修改JSON字段路径的修复。记录按实际完成阶段更新，不把协议夹具当作真实远端代码执行。

## 当前边界

本阶段新增候选数据模型、固定上游差异采样和平台侧生命周期服务。代码包由真实Python文本、精确依赖请求和远端解析的依赖锁组成；平台只做文本/AST/schema/摘要检查，不在API或Worker进程中import、exec或安装候选依赖。实际构建、运行和依赖安装均由独立runner负责。

本机Python使用 `hr-research-dev` Conda环境；没有调用本机Docker、真实模型或邮件/Webhook。候选测试使用独立 `hr_research_code_test`，不复用其他QA数据库。

## 文件与职责

| 文件 | 职责 |
|---|---|
| `backend/app/adapter_code_models.py` | 持久候选、预算/状态/历史、不可变代码包元数据 |
| `backend/app/adapter_code_upstream.py` | 固定仓库、提交和文件范围的只读采样 |
| `backend/app/adapter_code_service.py` | 生成、远端请求持久化、可信预期比较、发布前复核、取消与中断处理 |
| `backend/app/adapter_code_package.py` | 中央代码包/依赖schema、内容摘要及实现变化检查 |
| `backend/app/adapter_runner_client.py` | 中央独立runner协议、配置/运行环境检查、签名与隔离回执验证 |

中央API、采集调用、统一发布CAS、数据库迁移和真实远端runner部署另行接线。本文只对已经实际执行的验证作结论。

## 固定上游读取

- 当前固定代码参考为BOSS的 `eatmoreduck/boss-zhipin-scraper`，不接受动态仓库地址或任意用户提供的下载端点。
- 旧、新提交都必须是40位SHA；比较必须是以固定旧提交为merge base的前向变化。
- 只读取 `.py`、`requirements.txt`、`requirements.lock`、`pyproject.toml`、`uv.lock`；拒绝绝对路径、遍历、反斜杠及危险重命名旧路径。
- 最多4个文件，依赖文件优先；每文件16KB、总源文件48KB、差异文本10,000字符。单次最多1个compare和8个固定commit文件请求。
- 校验GitHub文件类型、路径、实际字节数、UTF-8和Git blob SHA。实际diff由读取到的旧/新内容计算，不直接信任可能被截断的API patch。
- 明确保存未选文件数量、差异截断标记和 `coverage=bounded_sample`；GitHub文件列表达到300项时标明可能受接口上限截断。
- 只有文档或未采样到相关代码/依赖变化时跳过模型。采样、读取失败保持可见，不把未知源码判成无变化。

## 候选数据与成本

`AdapterCodeProposal` 固定基线release及 `base_generation`、输入来源快照、平台预期案例、原始代码包、最多2次候选预算。`dedupe_key` 由来源、基线、案例、请求键和预算构成；数据库锁阻止相同请求重复创建任务。

首笔模型请求前调用runner的 `health()`，确认协议和固定运行镜像；否则进入 `blocked_configuration`，不消耗候选次数或ModelCall。运行镜像摘要冻结在维护任务快照中，之后每份验证回执必须匹配它。

`shared_task_id` 与 `existing_attempts` 是JSON修复→代码修复的统一预算接点；服务还反查同一任务已保存的ModelCall数量，取较大值，避免调用方少报已用次数形成“JSON两次＋代码两次”。模型次数在外部请求前持久增加，调用继续受统一每日/任务/输入/输出限制，不自动提高1200 token输出上限。

模型优先生成严格增量JSON：`edits:[{path,old,new}]`，可选精确版本 `dependencies` 和明确新增的 `new_files:{"lib/helper.py":"源码"}`。每个old在原始基线及当前对应文件中都必须唯一匹配；缺失、重复、重叠造成不再唯一的补丁直接拒绝。可信平台只执行文本替换，再验证完整代码包；不会执行补丁中的Python。未改文件直接复用基线，不要求1200 token预算反复重写全包。完整spec输出仍兼容。

模型输入按既有 `llm_max_input_chars` 预算选择完整失败案例、有限上游diff和准确行号的源码片段；完整源码不够放时，优先提供文件头和run/parse_list/parse_detail/navigation函数窗口。省略案例、diff尾部、源码行范围及实际输入字符数记录在 `TaskRun.config_snapshot.code_model_contexts`，完整资料仍保存于候选快照。若预算连固定协议或可定位源码＋有效证据也容纳不下，在ModelGateway前拒绝，候选次数和ModelCall均不增加，不提高预算。部分回归案例保留为平台验收数据，不交给模型。

最终完整包包含真实 `files["adapter.py"]`，入口为 `run(operation,payload)`。代码包必须支持 `parse_list`、`parse_detail`、`navigation` 的测试契约，候选不能替换平台的测试oracle。仅注释、格式、docstring或单独版本常量变化被明确拒绝；这不是一般程序等价判定，行为仍要通过可信预期验证。

## 生命周期

```text
queued / retry_ready
  → runner预检
  → generating（持久消费一次候选额度）
  → draft_ready → resolving → resolved
  → uploading → validating
  → validated → 统一发布入口 → 原来源恢复
```

每个阶段再次检查取消、模型开关、基线release和发布代次；A→B→A也不能绕过generation检查。旧任务中断后，生成阶段进入 `unknown`，不会默认再发送一次模型请求。

`advance_code_validation()` 是无sleep的有界推进函数：解析依赖一阶段，上传一阶段，之后只推进有限测试案例的提交和查询。上传前得到真正的精确依赖锁，并以 `adapter-code/<sha>.json` 在平台存储中保存不可变完整源码＋锁；它是不可导入的JSON产物，不只依赖远端缓存。包摘要覆盖Python文件、依赖请求、协议和锁内容。`AdapterCodePackage` 以摘要唯一，已保存内容不能通过ORM更新；发布前重新读取并核验本地包内容，保留旧版本重建和回滚所需资料。

所有远端运行的完整POST内容、输入、包SHA和幂等键先提交数据库。POST结果未知时，后续只查原键；即使lookup返回404也不当成确定未执行，不创建新键或重新调用模型。

每个最终回执由中央RunnerClient检查签名、输入/包/依赖/输出摘要、运行镜像、实际依赖环境、进程退出、资源限制和清理结果。候选stdout里的 `passed=true` 没有权限改变验证状态。平台把已验证的实际输出与独立冻结的预期JSON作严格比较；布尔值与数字也不能被宽松相等混淆。

候选解析失败或输出不符合预期时，保留代码包与完整验证历史；最多允许预算内的第二候选。签名、隔离或回执绑定不合格直接停止。任何候选失败都不切活动版本。发布前 `assert_code_publishable()` 再次检查基线/代次、不可变包、完整案例、可信回执以及release验证记录；最终切换仍使用中央唯一active指针。

取消先持久化候选与任务的取消状态，再按已有运行ID或原幂等键发出有限取消请求；未知结果明确保存，不把发出DELETE等同于隔离进程已清理。

## 已执行的检查

第一轮纯函数验证：**14项通过，0.10秒**；修改文件Ruff通过。覆盖固定仓库/提交/预算/依赖优先、路径与重定向拒绝、Git blob摘要、文档差异不读取源码、实质源码或依赖变化、三个操作的oracle覆盖及候选自报通过无效。

统一迁移 `8b7fd5e97379` 就绪后，在专用数据库上完成两文件定向：先23项通过（0.98秒），补并发/取消/不可变包后28项通过（1.42秒），补增量输出与输入预算后**32项通过（1.59秒）**，修改文件Ruff通过。最新JUnit：`.runtime/code-maintenance-junit.xml`；私有连接仅在内存派生的启动器：`.runtime/run_code_tests.py`。

新增验证具体包括：

- 即使候选源码包含本地写文件语句，校验时也不执行它。
- 实际MockTransport返回小补丁后，平台补全完整源码；不要求回传未改文件，`max_tokens`仍不超过1200。
- 大源码/大diff被有界选择，输入字符数不超过配置且省略范围可审计；极小预算在ModelCall前拒绝，实际0次请求、0次候选额度。
- 重复创建/生成保持幂等；两个真实数据库会话并发生成，只产生1条模拟ModelCall。
- runner缺失或不健康时0成本阻塞；共享任务已有两次JSON请求时不能再产生代码模型调用。
- 唯一远端请求先持久化；提交响应未知后按旧键查询，不创建新运行或再请求模型。
- 返回 `passed=true` 的坏输出不能通过平台预期；最多两候选后停止，旧active指针保持。
- 基线发布generation变化阻止提交；模型阶段中断标为unknown而不重发。
- 取消未知提交先查询旧键并取消原运行，且不再生成；已保存代码包不能修改。
- 完整源码与锁被实际保存为惰性JSON，并按包摘要重新读取核验。

远端协议测试使用 `FakeRunner`，它只按固定资料返回响应，不eval/import/exec候选，也不联网。因此这些用例只证明平台的状态机、幂等键与比较逻辑；真实代码、真实依赖版本变化及容器隔离证据必须在授权远端单独记录。

## 已验签终态的分类补充

独立审阅发现原协议的unknown结果会一直停留在validating，合法失败回执的输出摘要约定也不一致。中央客户端补充终态分类后，平台服务已接入：

- `runner_execution_unknown`：保存原请求键、运行ID、未经改写的签名结果及原因，候选进入unknown，维护任务结束；不再调用模型或建立新执行。
- `runner_execution_cancelled`：保存完整已确认取消回执，候选及任务进入cancelled。
- `runner_infrastructure_failed`：作为基础设施失败结束，保留旧版，不消耗第二次候选生成额度。
- 只有 `runner_execution_failed` 表示已执行且隔离策略通过的候选失败，可以走预算内第二候选；原回执里的status/error_code不被平台重写。
- 签名或上下文校验不通过仍直接失败；清理未确认的 `runner_cleanup_pending` 保留原运行、继续查询，不自动重发。

新增7项状态回归使用真实 `AdapterRunnerClient.verify_result` 验证测试签名回执，覆盖unknown、取消、两类基础设施失败、可重试的明确候选失败、清理等待和伪造签名。模型使用MockTransport，协议数据由测试构造，没有远端或候选代码执行。

补充后的两个文件完整定向：**39项通过，2.32秒**；修改文件Ruff通过。新JUnit为 `.runtime/code-maintenance-terminal-junit.xml`，此前32项记录保留。本轮数据库仍为 `hr_research_code_test`。

## 复合版本发布与管理入口

`adapter_code_release.py` 已把代码包关联到既有 `AdapterRelease`。活动版本仍只有 `AdapterRegistry.active_release_id` 一个指针，所有发布/回滚继续使用 `release_generation`；没有新增独立代码active指针。

`manifest.program` 严格包含以下5个字段：

```json
{
  "contract": "python_adapter_v1",
  "bundle_sha256": "代码包SHA-256",
  "lock_sha256": "依赖锁SHA-256",
  "runtime_sha256": "sha256:运行镜像SHA-256",
  "dependency_environment_sha256": "实际依赖环境SHA-256"
}
```

字段实际均有固定格式校验，上例只说明用途。提案ID保存在 `release.validation.code_proposal_id`，平台可信案例报告保存在 `code_validation`。引用中不允许命令、任意路径或下载地址。

发布链路：

1. `create_release_from_proposal()` 仅接受validated/awaiting_publish提案，重验完整本地源码＋锁、全部远端签名回执、固定运行镜像、同一依赖环境、完整案例输入和平台预期。
2. 将program引用与原有JSON规则合成一个版本；JSON兼容契约也必须通过。仅保存复合候选不会切换active。
3. 统一 `activate_release()` 按registry→release锁顺序核对取消、原基线release与generation，重新验证代码证明后原子发布；提案变为published并留下审计和维护记录。
4. 历史代码回滚必须是曾发布版本，重新检查包和回执；它不重复要求旧提案base仍是当前active，也不因代码模型开关关闭而阻止管理员回滚。
5. 固定上游的人工与自动发布都在统一入口检查登记repo、base/target SHA和当前upstream_commit；成功切换时同事务推进target，防止次日重复生成同一上游差异。历史回滚不重复旧上游base CAS。

普通JSON候选不能添加、替换或剥离program。当前已运行代码时，旧JSON模型生成入口在模型请求前拒绝，提示使用代码提案。官网新增 `official` registry，对应采集来源careers；无program时仍沿用原native官网流程。其影子检查实际调用 `collection.jsonld_jobs` 验证内置JobPosting的字段、主体保留、未知文档和秘密字段排除，不使用固定 `passed=True`。官网native基线不开放无实际采集作用的JSON模型升级。

权限采用3个独立默认关闭的代码开关：`code_agent_enabled`、`code_auto_publish_enabled`、`code_upstream_enabled`。旧JSON模型/自动发布开关不能隐式打开它们；关闭代码模型会同时关闭代码自动发布及上游升级。没有固定上游的平台不能启用上游升级。

管理接口均要求管理员：

| 接口 | 用途 |
|---|---|
| `GET /adapter-maintenance/code-proposals` | 有界摘要列表，可按kind筛选 |
| `GET /adapter-maintenance/code-proposals/{id}` | 源码diff、依赖变化、验证运行、失败记录和输入省略范围 |
| `POST /adapter-maintenance/code-proposals/{id}/cancel` | 取消现有提案及已知远端执行 |
| `POST /adapter-maintenance/code-proposals/{id}/publish` | 接受当前active ID及generation，复核后发布 |
| `POST /adapter-maintenance/incidents/{id}/code-proposal` | 仅按已有事件ID交给服务端编排推导证据，不接受前端oracle |
| `POST /adapter-maintenance/releases/{id}/rollback` | 涉及代码版本时必须提供当前active ID及generation |

前端维护页增加独立代码开关、提案列表、源码/依赖差异、验证状态、取消和发布，并在历史回滚请求中发送CAS信息。页面不提供上传自定义oracle或任意代码执行参数。

本轮完整定向覆盖 `test_adapter_code_service`、`test_adapter_code_upstream`、`test_adapter_code_release`、`test_adapter_maintenance`、`test_auto_maintenance`：**103项通过，7.80秒**，专用库 `hr_research_code_test`；JUnit `.runtime/code-composite-release-junit.xml`。此前先完成47项/2项暂未选择及76项阶段性结果，最终上述103项包含官网和管理接口。

具体回归包括：单指针复合发布、旧代码历史回滚且generation已变化、取消后拒绝发布、A→B→A后旧候选拒绝、伪造JSON代码引用拒绝、损坏本地产物/签名输出拒绝、旧JSON入口0模型调用拒绝、独立代码开关、普通用户403、前端额外oracle参数422、上游人工发布推进提交以及上游commit冲突保留旧版。

前端 `npm run build` 通过（包含TypeScript检查）；Vite保留既有主包大小提示。此处仅记录构建与接口证据，尚不将页面构建等同于新版部署截图或真实外部平台恢复。远端runner及中央采集接线的实际证据由对应验证记录补充。

## 正式采集调用的运行时隔离

`adapter_code_runtime.py` 使用 `AdapterProgramExecution` 单独保存正式采集的纯解析请求，不混入候选验证任务。`FrozenAdapter`、`freeze_for_task()`、`ProgramRuntime.invoke(context)` 与 `evidence()` 接口保持统一。

- 来源第一次执行时把release ID、generation、fingerprint固定在原Task快照。后续代次先读取此冻结版本；新active发布、回滚，甚至后来active选择损坏，都不会让正在执行的任务混用新代码。历史版本仍须是曾发布且内容摘要一致的版本。
- 请求键包含task、source run、冻结版本、operation及完整公开输入摘要；同一来源的同一纯解析输入可以跨attempt复用。请求与`prepared→submitting`认领在真实PostgreSQL锁内持久化，两个并发调用只提交一次。
- POST结果未知后只查询原键；lookup未找到也不重新POST。签名终态和错误保留在原执行行，unknown/cancelled/基础设施失败不会变成新代码请求。
- 每次状态写入仍检查Task当前attempt和运行状态。新owner可以接管同一纯输入，旧owner的迟到成功或迟到错误都不能覆盖新owner已接受的记录。
- 重新核对完整原来源快照、公司/来源启用状态、entry URL、账号、来源类型、规则与任务固定版本。取消、绑定变化及总时间超限均阻止接受；属于当前owner的停止原因可以保存，旧owner则拒绝写入。
- 有界等待按原请求查询；真实客户端每次HTTP超时受剩余等待预算限制。普通心跳最多每2秒更新，不为轮询创建TaskEvent。
- 签名成功之后仍调用第一方 `validate_list_output`、`validate_detail_output`、`validate_navigation`。编造字段、其他输入、无证据链接、不同依赖环境均不能成为accepted。只有这一层通过才把run ID加入当前Runtime的已验证证据。
- `runner_cleanup_pending` 不会永久复用旧的未清理回执：仍查询同一run，取得清理确认后再验证。

官网允许显式注入第一方 `scope_guard=checkpoint.guard_in_session`。基础身份/域名/账号和原Task快照检查仍先执行；只有config这一项改由同Session检查点验证，允许本任务经 `remember_plan` 保存并加入 `binding_fingerprints` 的计划变化。没有callback时继续严格比较config；前端和执行请求不能传入callback。

运行时证据形状保持：

```text
release_id, release_generation, fingerprint,
bundle_sha256, lock_sha256, runtime_sha256,
verified, run_ids, error_code
```

新增 `test_adapter_code_runtime.py` 的**22项**回归使用真实PostgreSQL、真实版本发布和真实客户端HMAC校验，Runner响应及种子版本模型生成均为测试fixture，不在本机执行候选代码。覆盖并发单提交、丢响应查询、跨attempt接管、旧owner拒写、冻结旧版、取消/绑定/时间守卫、虚假字段与环境摘要拒绝、签名终态、清理刷新、有限等待、列表/详情/导航三类第一方验证，以及真实 `OfficialCheckpointStore.open/remember_plan/guard_in_session` 的官网config授权变化和额外手改拒绝。

运行时22项与既有code service/upstream 39项合并定向：**61项通过，5.75秒**；修改文件Ruff通过。数据库仍为 `hr_research_code_test`，JUnit `.runtime/code-runtime-junit.xml`。这个结果验证运行时边界；真实Broker、Worker和最终采集恢复由独立E2E记录说明。

## 官网代码采集、完整性与原生失败证据桥

新增 `official_program_collector.py`，入口为：

```python
collect_official_program(
    binding, identity, filters, task_id, source_run_id,
    expected_attempt, descriptor, checkpoint, progress=None,
) -> CollectionResult
```

这是独立的实际程序解析路径：每个列表、详情和导航都调用真实 `ProgramRuntime.invoke`，经过远端回执与第一方字段/链接校验；不会先调用native采集成功后补一个“代码已验证”标签。测试里替换的是HTTP和Runner响应，不替换Runtime、PostgreSQL、第一方校验或检查点。

代码指纹固定为 `descriptor.fingerprint`，不会用网站计划覆盖发布版本指纹。网站计划通过 `checkpoint_info.site_plan_hash` 和cursor单独保留，供中央入库范围指纹纳入计划变化。返回的 `code_execution` 使用统一运行时证据形状。

读取与预算：

- HTTP复用PublicFetcher的公网DNS验证和连接IP固定，响应hook在跟随跳转之前拒绝不同origin；BrowserCapture使用同源导航守卫。每次读取前后执行Runtime和检查点守卫。
- 网站有合法JSON-LD时直接进入代码解析，不申请网站计划模型。已有CSS计划必须通过WebsitePlan及保存的hash验证。
- 缺计划时仅对有界观察页面调用既有 `plan_site` 学习/验证，不执行整条native网站图；模型输入使用脱敏DOM。检查点保存过的body直接复用，计划检查用过的详情不会再次网络读取。
- 列表、详情和浏览器发现消耗原任务预算；最多读取预算内的新页面并保存页内位置。规则模型的适配预占保留在检查点，结果未知后不重复收费。
- 支持已观察链接的分页。程序提出的滚动/等待若没有已观察后继链接，不冒充已经读到新页面；保留限制说明。只有第一方能独立证明范围完整时才给complete。

完整性完全由平台判定：独立记录已批准岗位链接/JSON-LD inventory、实际完成详情、已观察分页、明确总数与结束提示。必须没有未处理链接、未完成详情、候选遗漏和冲突，且实际岗位数量或结束证据核对成功，才标记complete。明确可核验的零岗位范围也可complete；没有证明的空列表不会当作零招聘成功。候选不能通过自报字段改变coverage。

岗位身份与观察时间：

- 复用第一方 `rebind_official_detail_job`，JSON-LD的原identifier优先；无identifier保留原native岗位URL身份，CSS保留实际详情final URL身份。不会改成另一套hash造成首次转代码时重复新增。
- 详情 `observed_at` 来自对应HTTP/浏览器证据捕获时间。接续不会把旧岗位的观察时间改成重试时刻。
- 明确resume链可继承同公司、同binding、同固定代码指纹的旧accepted运行。继承时核对完整签名、请求摘要、依赖环境、原始页面hash、第一方输出以及原观察时间；不会任意引用别的任务的运行ID。
- `checkpoint_info.inherited_run_ids` 明示继承部分；新页继续实际程序执行。代码指纹发生变化时拒绝接旧代码cursor，代码修复的新任务应从新版本独立采集。

原生首次转代码的证据桥：

```python
capture_native_official_failure(binding, identity, checkpoint, error_code)
# -> 标准安全program_input DTO，或证据不充分时None
```

只处理实际解析类错误，读取检查点已保存页和当前已经取得但尚未提交的pending页；不新访问网络、不调用模型、不执行程序，也不标记native为code verified。从已有批准链接/保存计划导出详情身份，Tencent使用实际已捕获API JSON；敏感内容通过第一方ProgramContext脱敏后封装，DTO没有自带expected。原异常继续保留。

真实native入口 `website_agent.collect_official` 新增默认关闭的 `defer_parse_repair`。已有计划失败且明确启用此标志时，保留selector_mismatch/adaptation_required交给代码维护，不先进入另一条网站规则模型链消耗预算；默认行为及首次无计划的学习流程保持。中央Worker按独立代码开关使用此选项。

最终定向包括官网collector **21项**、代码service/upstream 39项、复合发布13项和既有website_agent 5项：**78项通过，17.94秒**，Ruff通过；JUnit `.runtime/official-program-final-junit.xml`，独立数据库仍为 `hr_research_code_test`。覆盖真实PG/Runtime与检查点接续、完整范围/明确零/遗漏/截断、同源跳转拒绝、取消、已保存浏览器计划、发现预算不放大、原生→代码实际ingest无假新增、一次学习和未知结果不重复收费，以及真实collect_official入口保留原解析失败形成安全DTO。全部外部页面、模型和Runner使用测试响应；没有本机候选执行或真实外部采集。

同期仅做两处管理薄改：显式管理员actor沿唯一发布路径传到恢复hook；恢复已排队或正在执行的提案不再显示“取消提案”，恢复任务从任务入口管理。最新 `npm run build` 通过，仍只有既有主包体积提示。
