# 代码／依赖维护：当前实现与中央接线

更新：2026-10-05。对应原PLAN的P8.4。中央实现、最终本地644项回归、最终冻结源码的独立真实远端E2E和同源主栈控制网络/镜像/远端维护页只读QA均已通过，P8.4按本文限定工程范围为completed。本文区分组件、业务链路和主栈部署证据。

## 当前状态

| 部分 | 当前事实 |
|---|---|
| 发布与回滚并发保护 | `release_generation` + `replaces_generation` 已实现；两Session及ABA先红后绿；锁后刷新ORM；旧候选缺代次时拒绝 |
| 平台数据库 | 已升级到 `7c8ba429a6ea`，包括代次、代码包/候选、实际运行记录和独立开关；Alembic check无差异 |
| 代码包 | `adapter_code_package.py` 保存真正Python源码及可信依赖锁为内容寻址JSON；本机只语法/字节检查，不执行候选；本地完整产物保留用于回滚 |
| 执行服务客户端 | `adapter_runner_client.py` 认证、幂等、未知查询、签名和上下文复核、实际限制/清理核验；已验签的候选错误与设施故障分开 |
| 候选生命周期 | `adapter_code_models/service/upstream.py`，固定上游差异、有界增量代码补丁、最多两候选、持久阶段/原键恢复、可信oracle、取消、发布前重新核验 |
| 第一方资料边界 | `adapter_program.py`，覆盖official及四招聘平台；脱敏资料、稳定input/link ID、行内字段证据、公司归属、最终岗位URL身份与只读导航计划；来源URL/登录仍由平台控制 |
| 独立Broker | `deploy/adapter_runner/` 已在授权远端独立部署；已完成45项本地、7项有限远端审阅回归，原12组隔离证据保留；终态/落盘/配额补强后最新runtime为ae9906fb…，见CODE_RUNNER_VALIDATION |
| 联合源码验证 | `scripts/verify_adapter_program_remote.py` 与 `adapters/code/qa_cases_v1.json`；13项远端联合通过：旧代码在改版样例失效、新代码三操作通过、移除BS4真实失败；随后JSON私有键归一化另有34项本地证据，未重跑远端；不冒称真实上游业务改版 |
| 中央自动升级闭环 | **实现与业务链路通过**：复合发布、平台/泛型官网/腾讯程序选择、实际Runtime记录、失败及上游调度、一次恢复/回滚、独立管理开关和页面已接入；最终本地644项通过，最终冻结源真实Worker→远端Runner→恢复入库与旧依赖回滚E2E通过；同源主栈部署、实际API/Worker控制网及远端维护页只读已通过 |

## 已固定接口

候选代码入口是 `adapter.py` 内同步 `run(operation, payload)`，operation为 `parse_list`、`parse_detail`、`navigation`。程序只能产生数据和声明式读取计划，不获得浏览器句柄、Cookie、HTTP headers、Profile、数据库或Docker socket。

包结构是规范化JSON：`schema_version=1`、`contract=python_adapter_v1`、`kind`、`files`、`dependencies`、`lock`。源码不超过8文件/100KB，仅adapter.py及有限Python辅助文件。依赖由固定索引解析批准pure wheels，模型只能请求精确包版本，不能给下载地址、Dockerfile、安装命令或伪造wheel hash。当前批准包为beautifulsoup4、soupsieve、typing-extensions、html5lib、six、webencodings、packaging。

SHA使用 `json.dumps(sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')`。已有JSON manifest的指纹算法保持兼容。代码版本的 `manifest.program` 严格包含 `contract`、`bundle_sha256`、`lock_sha256`、`runtime_sha256`、`dependency_environment_sha256` 五个字段，把源码、锁、运行镜像和依赖环境绑定进同一复合版本指纹。

只保留现有 `AdapterRegistry.active_release_id` 指向一个 `AdapterRelease`，以ID＋generation进行CAS。代码候选不能通过JSON候选或shadow端点绕过代码验证。历史回滚重新核验完整保存包、锁与签名回执，并核对当前活动ID/代次；不要求历史提案的旧base仍是当前活动版本，否则无法合法回到已发布旧版。

### 平台包与客户端

- `validate_candidate_spec`、`has_implementation_change`：检查语法和允许范围；忽略明显注释/docstring/版本声明变更，不声称能判断任意程序语义等价。
- `build_bundle`、`validate_bundle`、`content_sha256`：代码＋锁的固定包。
- `persist_bundle`、`load_bundle`：相对storage的 `adapter-code/<sha>.json`；完整字节hash复核，不把.py文件解包到API运行目录。
- `AdapterRunnerClient.health/resolve_dependencies/upload_bundle/submit_run/get_run/lookup_run/cancel_run/verify_result`：仅可信控制面，测试模式未经MockTransport不能联网。

### Broker HTTP

所有请求使用独立Bearer凭据，API/Worker不得挂载Docker socket。

- `GET /v1/health`：status、contract、实际固定runtime_sha256。
- `POST /v1/dependencies/resolve`：精确dependencies → lock/lock_sha256。
- `PUT /v1/bundles/{sha}`：规范化包，路径与内容hash必须一致。
- `POST /v1/runs`：`{idempotency_key,bundle_sha256,operation,input}`，返回run_id/status/request_sha256。同key不同内容409；同key不再执行。
- `GET /v1/runs/{id}`、`GET /v1/runs/by-key/{key}`：查询原执行；404不是未执行的证明。
- `DELETE /v1/runs/{id}`：取消已登记执行，不新建替代任务。

receipt由Broker签名，候选没有签名密钥。派生密钥为HMAC-SHA256(token, `hr-adapter-runner-receipt-v1`)，对移除signature字段后的canonical receipt再做HMAC；`signature_algorithm=hmac-sha256-v1`本身也在签名范围内。

回执绑定run_id/status/error_code/operation、完整请求与input摘要、包/锁/运行镜像/依赖安装与环境/输出摘要、真实限制、exit/OOM/执行是否开始及清理。失败的output=null也必须有对应hash。未知、取消、未启动、Docker设施故障不得通过消耗下一次模型候选处理；只有已执行且隔离证据完整的明确候选错误才能使用剩余额度。不能仅凭签名认定输出事实或测试通过。

### 第一方程序边界

`prepare_program_input(...)`返回ProgramContext，只有`.request`发往Runner；完整绑定/公司身份和原始导航令牌留在第一方。input_id与link_id按内容生成，保证同资料同操作稳定。Runtime执行键绑定task、source run、冻结adapter fingerprint、operation和input hash；`owner_attempt`是独立的持久写入门禁，不进入执行键。相同纯输入可在后续合法attempt复用原执行；未知POST只查询原key，不能换key补交。复用输出仍须通过当前任务、来源、版本、配置和代次校验，不能跨公司重用权限。

`validate_list_output`重新核验原资料中的ID、标题、公司等字段及同一行证据，返回JobDocument和私有导航信息；`validate_detail_output`只更新正文/要求；`validate_navigation`只允许已观察链接opaque ID、有限scroll/wait。候选不得给URL、POST、headers、通用click、观察时间、complete覆盖或passed结论。实际动作仍执行第一方DNS/跳转/账号锁/取消/代次/总预算检查。

当前第一方程序边界支持BOSS、猎聘、智联、前程无忧和official。`SourceBinding.kind=careers`映射到official registry；`official_program_collector.py`实际调用ProgramRuntime处理泛型JSON-LD/CSS和腾讯公开招聘接口，并保留官网域名、主体、来源范围及检查点权限。不会静默退回原生采集后仍标新代码已使用。

泛型官网以第一方已批准链接inventory、详情补全、分页和声明总数/结束证据决定完整性；候选遗漏岗位、页面/详情预算耗尽或总数未覆盖均不能标complete。原生→代码与检查点接续保留native岗位ID和实际HTTP观察时间，旧缓存不制造新观察。腾讯保留真实PostId及实际招聘主体，详情请求/响应编号必须一致。

原生失败证据桥读取已保存或pending的原始资料，不为提案额外采集或调用模型。`collect_official(..., defer_parse_repair=True)`把既有计划的解析错误保留给代码维护；首次无计划仍走受控学习。恢复任务清除旧 `adapter_versions` 与resume，再冻结新发布程序，且只对原失败来源恢复一次。发布/恢复期间的ABA仍由持久generation阻止旧结果收尾写入。

## 中央接线的完整验收范围

以下为完整验收合同。中央实现已通过最终统一644项回归（132.06秒），113个显式实现文件前后SHA均为 `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`，Ruff通过；迁移head为 `7c8ba429a6ea`。第二轮643项、此前25/33/49项定向结果是各自版本的分层历史证据，有重叠，不能相加。详细自主决定见DEC-0033。所有本机Python验证使用项目Conda；Docker仅在授权远端执行。

1. **复合版本与发布。** 使用现有唯一 `AdapterRegistry.active_release_id` 和代次，不创建并行active指针。给版本明确代码类型/引用，原manifest与代码包、完整锁/wheel、runtime、上游提交原子绑定。代码候选不能走原JSON shadow函数就取得发布资格。创建release、发布前校验、手工/自动发布和历史回滚都要重新核验产物与签名证据，并保持ID＋generation CAS。历史代码/依赖产物不提前清理。
2. **运行选择。** 采集开始时冻结复合版本，显式穿过Playwright EventGreenlet回调。程序执行通过独立Runner，返回后经过第一方验证再入库。保留HTTP、登录/验证码、公司/岗位跳转及平台特定的访客截断检查（例如智联guestJobDescClamp）；代码有原文证据不代表原文就是完整正文，不能用程序绕过这些检查。SourceRun记录实际代码、锁和runtime，而不是仅显示一个新版本号。Runner不可用时不得在本机import候选或静默回退原生后宣称新程序成功。
3. **状态恢复。** 原请求和输入先持久化再提交；未知结果查询原run/key，不换key重跑；同资料稳定ID不可因重新构造上下文而随机改变。相同纯输入可跨合法attempt复用，写入以独立owner_attempt fencing保护。取消/过期Worker/配置或版本变化都阻止旧结果写入。
4. **真正自动触发。** 固定上游提交变化应进入有限差异采样和有依据的候选流程；文档/无关变化不花模型。解析故障只有独立可核验的失败资料才能生成/发布；可信oracle不由候选或模型自己编写。JSON和代码修复共用同一个源事件最多2次候选预算，不能叠加成2+2。
5. **权限与成本开关。** `code_agent_enabled`、`code_upstream_enabled`、`code_auto_publish_enabled`三个独立开关默认关闭，不能把旧JSON模型开关悄悄扩大为自动执行代码。Runner及模型配置缺失、输入预算不足必须在付费请求前明确阻塞；单次输出预算保持原值，以精确增量补丁支持小改动。
6. **实际恢复与回滚。** 发布后新恢复任务只针对原失败来源，使用固定新代码实际采集/入库后才记录recovered。失败不循环付费；只有当前仍是失败版本/代次时回滚。回滚后下一次实际调用旧代码与旧依赖，不能只更新数据库显示。
7. **产品入口。** 管理页面显示来源差异、候选、代码/依赖变化、执行证据、预算、未知结果、失败原因、发布/回滚，保留人工操作入口。普通业务用户不接触Broker凭据或任意执行参数。
8. **部署。** 独立QA控制容器到Broker的bridge通信已通过真实E2E；同源最新主栈另已完成实际API/Worker的受认证控制网、镜像、迁移、队列和页面验收。API/Worker无Docker socket、Broker宿主端口为空，控制网精确3服务；不把临时UI探针的host network当控制网证据。默认运行配置与密钥继续私有保存，本机Docker始终禁用。

## 完成证据不能省略

受控改版必须经过真实业务Worker：原采集失败 → 固定来源/脱敏证据 → Mock模型产生真实源码／依赖修改 → 真实远端执行旧回归及失败样本 → CAS发布 → 冻结新代码实际恢复 → PostgreSQL入库。模型模拟不替代代码执行；源网站可以是明确QA夹具，不冒充真实平台登录。

不兼容候选保留旧版、验证后篡改拒绝、同时发布/ABA/取消/旧执行拒写已有本地具体回归。最终冻结源码的真实远端E2E进一步证明原生失败→程序A、A失败→程序B两次自动发布/恢复，坏候选两次经回执验签后确认验证失败，保留B，CAS回滚A后实际使用旧bundle/锁及 `beautifulsoup4==4.13.4` 并新增Observation。三轮影子阶段正式业务表全行摘要不变。同一程序源码随后完成主栈镜像、迁移、实际API/Worker控制网、队列smoke和远端维护页只读验收，因此P8.4工程范围为completed；真实网站账号、真实模型质量及未知语义自治修复没有被该受控验收证明。

最终E2E证据为 `.runtime/code-maintenance-e2e-dd39b8ca/`，driver冻结源码SHA `48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96`；该清单范围与本地113文件清单不同，不把两个不同范围SHA直接比较。整轮6次真实内部HTTP、4次Mock模型、0真实LLM/通知/平台登录；16个既有容器未变。受控网站与模型仅替换外部传输/响应，Worker、浏览器DOM、Runner、验证、发布及入库实际执行；不声称真实平台登录或真实模型质量已验收。此前ab6b2f03是详情URL身份门禁修复前的历史E2E。

最终主栈证据为 `.runtime/code-maintenance-deployment-20261005/`。Backend实际镜像为 `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89`，Frontend为 `sha256:1458187644d93d621690a69f72e0736e76b282e2a21b3b0ae4d694c4da4c42b5`。118文件部署清单SHA `75aa288cec2fb48cd8011582f6791c78c01f5a420d42fe2a82665fb5f73ea6b8`；本地113与部署118的110个重叠文件逐项一致，89个E2E程序文件全部包含在部署清单且镜像内复算一致。head为7c8、Alembic check无漂移；9个无关容器和原PG/Redis未变。远端主库ModelCall/已发通知/代码提案/queued或running任务均为0，不与本地开发库2条历史ModelCall混计。

本地维护页QA也已完成：现网登录后只读6个GET全200，无业务配置/提案修改；隔离内存API夹具覆盖15阶段、源码/依赖diff、CAS冲突、开关联动，114响应含2个刻意409，7次变更只影响夹具。JS/外部请求0，抽屉860/860。失败文案为“失败，请查看版本记录”，不能一概断言失败意味着旧版本保留。远端最新部署也以同一脚本通过只读5来源/15关闭开关/6GET200/JS0验证；远端未修改业务配置或制造提案，真实发布交互仍对应独立E2E和本地隔离夹具证据。

现有参考证据：[中央统一回归](CODE_PLATFORM_INTEGRATION_VALIDATION.md)、[维护页QA](MAINTENANCE_UI_VALIDATION.md)、[自动编排](ADAPTER_CODE_ORCHESTRATION_VALIDATION.md)、[真实远端E2E](CODE_MAINTENANCE_E2E.md)、[最终主栈部署](CODE_RUNNER_DEPLOYMENT.md)、[候选生命周期](CODE_MAINTENANCE_VALIDATION.md)、[Runner隔离](CODE_RUNNER_VALIDATION.md)、[第一方程序联合验证](ADAPTER_PROGRAM_VALIDATION.md)、[程序协议与QA源码](../adapters/code/README.md)。这些是分层证据，不能相互替代。

包/客户端38项通过的 `.runtime/adapter-package-client-junit.xml` 保留为此前组件证据；当前本地统一结果为644项，完整远端业务E2E另有上述最终冻结记录。开发库迁移和此前本地readiness通过，维护页现网检查未见代码候选/历史版本，主开发库历史ModelCall仍2条。本地主进程的已记录刷新发生于643项回归后，不把该记录倒写成最后修复后的刷新证明。远端最新主栈已完成部署；本地进程刷新记录仍按实际时间保留，不能拿远端运行证明本地进程版本。
