# 需求与验收完成度审计

审计日期：2026-10-05（Asia/Shanghai）。项目根目录：`/Users/songbingrong/Documents/ChatGPT/hr-research`。

本审计逐项核对用户已确认需求、`PLAN.md` 第 3.1、19、20 节。依据是当前源码、测试具体断言、已保存运行产物及部署源码指纹；没有通过重写计划删掉范围，也没有用“多少项测试通过”替代功能覆盖。审计本身没有请求外部网站或模型。

最新765项完整回归通过，131个实现/部署输入前后相同。U33结构化订阅/政策日期提醒、U34分类保留已实现并在本机和授权服务器部署；b25迁移、Worker注册及真实API读取通过，主数据清理0。证据见[LIFECYCLE_VALIDATION.md](LIFECYCLE_VALIDATION.md)。U35真实模型修复质量仍unverified，账号/真实收件/适用许可等运行条件保留，不将工程验收当外部接入完成。

## 711历史版本、提示修复与真实质量边界

- `.runtime/final-prompt-integrated-round2-junit.xml`为真实完整执行的711项、0失败/错误/跳过，控制台126.21秒，JUnit126.147秒；SHA `9563cf70e8c3ce2ee4d78ef6c2a4c596cd15c6f59d50259e42b2d9236eca5e1f`。项目Conda、独立hr_research_integrated_test，自动化真实模型0，Ruff通过。
- 123文件before/after指纹分别为 `0919f1b0b33441b6379ce5c4522bfb51ce586075d429f6f288ae1b868a0e7675` / `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8`；仅SentimentPage.tsx并行修改，93个backend/adapters文件逐项一致，不称整仓未变。概要为 `.runtime/final-prompt-integrated-round2-summary.json`。
- 初次707项为705通过、2失败，原JUnit/日志保留。日报测试发送时钟固定在10点、投递默认时间却来自已越过10点的墙钟；只修测试，显式对齐到期/发送时间，按60秒退避检查到期前不发，增加2020闰日与2035跨年。53项通知回归通过后再真实重跑711，不拼接定向JUnit、不修改生产通知行为。
- 提示按AST优先保留故障完整函数并合并窗口；3000字符第一方失败资料先红后绿，75项专项通过，实际提示15691字符，parse_detail的114–151行及完整故障资料在场。16000字符内放不下关键实现或完整资料时在模型与候选递增前拒绝，输出仍限1200 tokens。
- 最终舆情范围页面已补来源、北京时间范围及预算入口：7组场景、61个200/202响应、6次提交仅内存，JS/外网/模型/主库写0。跨浏览器时区、切换主体清空来源、取消重开、无来源仅分析及非法整数等通过；源码SHA `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11`。证据 `.runtime/sentiment-scope-ui-validation-20261005/summary.json`，不冒充真实社交采集。
- 唯一真实代码修复请求使用独立QA、glm-5.3 low；提供商记录5960输入/1200输出tokens，finish_reason=length，网关failed/output_truncated。候选保存/执行/发布/held-out均未发生，未重试或换模型，质量unverified。该1次7160 tokens加主开发库历史2次3588，已知累计3次10748 tokens；无单价，不换算费用。直接证据 `.runtime/live-code-repair/model-call-audit.json`，详见 [LIVE_CODE_REPAIR_VALIDATION.md](LIVE_CODE_REPAIR_VALIDATION.md)。711项Mock回归不代表这次真实质量成功。

## 711最终同源部署与范围页面（completed）

证据 `.runtime/final-711-deployment-20261005/final.json` 为passed；同目录 `source-match.json`、`build-source-manifest.json` 与 `scope-ui/summary.json` 分别记录来源、构建输入与只读页面范围。API/Worker镜像 `sha256:b9cfbdf4b59b3dc06942a185a51741cc07a40c23dbfe8708163026ff9628b672`，Web镜像 `sha256:1390d3b5dfa62349b8bdbc5210935139ddda2fc2f692f73971bc9d507a762fd9`。

最终123源清单 `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8` 中122个部署路径全匹配，仅 `.env.example` 不部署；119构建输入清单SHA `ae9e960ca9721c7830635b9fbac6eeb2052b6fe922d999b8cf1cd1fee951cd60`。镜像程序集合SHA `15bd8bc15ea414af7528cbe78bb50f698d308c64ba7121575bdfeeb3d6932325`、提示服务 `10aea9607f8c53396f0df754bc7df3f299dea7b1617fc0369e64efedc3a8bab3`、舆情UI `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11` 均对应冻结实现。

迁移仍7c8、无新增迁移；API/Worker Runner ready，Search health200，Runner/Search专网分别3/2成员。10家公司、6个来源绑定完整摘要未变，远端ModelCall/活动任务0；14个保留容器不变，其中9个为无关业务。远端范围页8API200、JS错误0、非GET业务请求0、新任务0。本轮未重跑真实模型、真实搜索或代码维护E2E；P9相应工程增量为completed，U33/U34 incomplete、U35 unverified及外部依赖不变，整个V1目标尚未完成。

## 701项增量及同源部署（历史完成）

`.runtime/refinement-integrated-junit.xml`实际为701测试、0失败/错误/跳过，XML133.862秒；`refinement-source-before.json`与`refinement-source-after.json`均123个实现文件、SHA7991…不变。开发迁移head仍为7c8ba429a6ea。以下定向测试与页面证据均独立留证，不与701相加。

- 计划UI7场景使用真实构建、localhost HTTP夹具和生产PlanInput；编辑保留/修改/清空、新建重置、预算/标签边界和部署搜索地址校验通过，模态框632/632、19API响应全200，4次计划写及1次搜索设置写只在夹具内存，真实DB/外部/模型0。
- CompanyWorkspace发现UI4场景覆盖成功、部分引擎失败、无候选和409保留此前候选；25API响应含1个刻意409，面板1130/1130、JS0、候选跳转/绑定0。
- 舆情详情不被摘录降级、每次观察原文保留；资讯Feed/日期候选/行业筛选10项实际UI通过。新闻最后事务窗口真实PG先红2项，修后相关28项通过：Task FOR UPDATE持锁到文章/来源/partial终态共同提交。
- 原生BOSS/51job最终URL岗位ID门禁先红2项，再有53项通过；招聘管理与搜索联合69项通过。内部搜索例外仅精确部署端点，通用PublicFetcher私网保护不变，查询不自动访问/绑定候选。
- 独立真实API搜索证据 `.runtime/search-api-e2e-0a79bbb7/summary.json` 为passed：一次真实搜索，10条响应中保存5候选和Audit；SourceBinding/ModelCall/TaskRun均0，主库写0、候选站点访问0，样例为金蝶QA而非正式竞品。
- 本机已启用loopback搜索隧道及部署端点、刷新三服务；`refinement-local-runtime.json`为ready、origin=deployment、enabled=true、端点匹配，新增查询/设置写0。主库activeTasks0、enabledPlans0、历史ModelCall2。

701版主栈后续已完成部署与验收：Backend `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157`、Frontend `sha256:607170d6b73b29b8b033276686ebd7d8743496fcc188574856f6de079d453b0c`，119文件构建清单SHA `e067748baa4d8cdb5d70fdc66424e476efe57795a24948152458a9b289ecb6c3`。搜索网API/SearXNG与Runner网API/Worker/Broker均符合约定，14页及维护页只读通过；10家公司/6来源绑定摘要未变，主栈模型/通知/活动任务/代码提案0、14个保留容器未重建。证据 `.runtime/refinement-deployment-20261005/final.json`。总记录见 [REFINEMENT_VALIDATION.md](REFINEMENT_VALIDATION.md)、[CONTENT_REFINEMENT_VALIDATION.md](CONTENT_REFINEMENT_VALIDATION.md) 与 [SEARCH_DEPLOYMENT_VALIDATION.md](SEARCH_DEPLOYMENT_VALIDATION.md)。701历史部署为completed，新增711源码的P9部署/远端增量页面已凭独立最终证据标为completed。

## 上一轮P8增量（644项历史基线）

开发库head为 `7c8ba429a6ea`。中央触发、复合发布、平台/泛型官网/腾讯实际Runtime采集、原生失败证据桥、一次恢复/回滚和管理页面已落地，并通过第二轮 **643 passed，135.89秒**。该轮backend/adapters前后逐文件未变；唯一变化是维护页failed中性文案，随后重新构建，不能把整体SHA差异写成全仓库未变。见 [CODE_PLATFORM_INTEGRATION_VALIDATION.md](CODE_PLATFORM_INTEGRATION_VALIDATION.md)。

第二轮后，详情跳转到同平台另一job ID的负例先红，生产门禁已修复并纳入最终 **644 passed，132.06秒**；113文件前后SHA均为 `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`，Ruff通过。维护页本地现网只读与隔离交互QA均通过，分别验证真实空态和受控提案/CAS交互，不混称主库真实发布。最终冻结源真实远端E2E已通过（dd39b8ca，driver源码SHA 48d50c98…）；其清单范围与本地113文件不同。同一程序源码当时已部署到对应主栈，迁移、实际API/Worker控制网、队列smoke与远端维护页只读均通过，当轮P8.4及对应P9工程验收为completed；不跨版本签收后续701/711源码。范围合同见 [CODE_MAINTENANCE_INTEGRATION.md](CODE_MAINTENANCE_INTEGRATION.md)。该轮代码维护新增真实业务网站、模型和收件调用0，本地开发库ModelCall为2、远端主栈为0；本轮真实搜索另列。

## 状态口径

- `completed`：该行限定的功能或验收已有相匹配的源码及运行证据。若证据使用受控网站/模型，会明确写出，不代表外部真实登录也完成。
- `unverified`：已有实现或旧版本证据，但不足以证明该项当前状态；例如没有对应场景测试、只有旧镜像记录。
- `incomplete`：当前确有缺失或仍在开发/接线，不能通过现有证据认定已交付。
- `external_dependency`：实际使用依赖尚未提供或确认的账号、收件目标、许可、真实来源或网络条件；与代码是否已实现分开。

## 优先结论

1. 单公司数据/模型上下文、公司与来源管理、岗位历史、招聘分析/比较、通知与日报、受限自动维护均有可执行代码和具体测试支持。腾讯官网两轮各两条岗位及单公司模型报告已有保存证据；第二轮确认这两条样本内容未变、观察时间推进，不推断全站招聘稳定。
2. 招聘平台和社交平台“登录后真实采集”仍不能标为完成。浏览器登录中心与受限状态已验证，BOSS/猎聘/智联/前程无忧及小红书/抖音/微博的真实账号未提供，公开读取证据也有明确限制。
3. BettaFish 已在独立 Conda 和授权远端可选镜像执行真实固定提交的 ReportAgent。最新简报有独立原始 HTML 与安全静态导出 SHA，默认三阶段预算拒绝五阶段模板且请求为 0；真实模型语义质量及生产授权仍为外部依赖。
4. T04 重复快捷触发、T12 旧执行晚到和浏览器回调冻结缺陷已修复并有针对证据。官网检查点在真实独立 Celery Worker、PostgreSQL 和本地 HTTP 下完成失败后续跑、局部推进、原始观察时间保留及旧执行拒写。
5. 三公司真实 Redis/HTTP 并发验证证明同账号串行、不同账号并行，单 Worker 默认并发 3；不承诺任意多个 Worker 的全局配额。一个合并岗位的官网来源消失而 BOSS 仍被观察时，真实 API 保留整体岗位和各来源状态。
6. 当前711项为真实完整执行，93后端/适配器文件前后相同；123文件清单仅有并行的舆情前端变化，最终范围UI另有7组验证。原707失败与53通知专项保留。701同源主栈已完成，最新711镜像与远端增量页面也已独立验收，不跨版本借用证据。
7. JSON与代码/依赖升级的受控工程闭环已有历史E2E及部署证明；唯一真实模型修复请求因输出截断失败，没有进入候选验证，质量unverified。名称搜索已独立部署、真实查询及接入701主栈，不再待用户提供服务；搜索候选仍不是主体核实。

## 用户明确需求与 PLAN 3.1

| ID | 要求 | 状态 | 具体实现与证据 | 未被该证据证明的部分 |
|---|---|---|---|---|
| U01 | 中国大陆企业内部平台，招聘/整体舆情/HR 资讯统一入口 | completed | App路由、main三模块、实际登录/权限集成测试已纳入第二轮643；旧冻结版远端14页有独立证据 | 不代表外部来源全量覆盖；新代码管理页远端只读及本地隔离交互分别验收 |
| U02 | 整体舆情包括客户、产品、经营、雇主口碑 | completed | `sentiment_service.rule_classification` 分类与分析主题；`sentiment_api.SubjectInput`；`SentimentPage.tsx`；`test_sentiment.py` 分类/归属/证据用例 | 规则分类不是事实核实或自动法律结论 |
| U03 | 内部登录、基本管理权限 | completed | `security.py`、`admin_api.py`；`test_api` 登录/注销/Origin；`test_admin` 最后管理员保护与会话撤销；主应用真实会话及普通用户 403 测试 | 未要求的 SSO 不纳入范围 |
| U04 | 用户指定竞品，不猜测名单；公司、品牌、产品、行业主题 | completed | 公司 `Company/Subject`；舆情 own_company/brand/product；资讯 `subject_id/topics/industries`；批量录入同名 review；前端无预置正式竞品 | 自有公司/产品及正式名单仍待用户录入（配置前提） |
| U05 | 表单、批量粘贴名称、身份/来源链接 | completed | `recruitment_discovery.bulk_companies`；`recruitment_management_api.add_companies`；`test_recruitment_management` 批量原子性/同名不合并；`CompanyWorkspace.tsx` 编辑 | 同名候选由用户明确选择；不自动合并法律主体 |
| U06 | 官网入口发现与人工修正 | completed | `discover_entries/bind_candidate`；已填官网同域链接/JSON-LD；`PATCH /source-bindings/{id}`；真实腾讯首页发现 careers 链接，事务回滚记录见 `RECRUITMENT_MANAGEMENT.md` | 未配置名称搜索服务时返回可操作配置入口，不猜官网 |
| U07 | 仅公司名的外部搜索服务 | completed | 独立SearXNG/Valkey真实查询；实际FastAPI与QA PG一次搜索→5候选/Audit，SourceBinding/ModelCall/TaskRun0；701主栈搜索控制网、实际部署配置和页面验收已通过 | 搜索结果仍是未核实候选；711版更新部署亦已完成P9.4，不再要求用户另供搜索账号 |
| U08 | 一家公司一个底层任务；上层批量计划 | completed | `tasks.create_batch` 分别创建 TaskRun/CompanyRun 和来源/身份快照；`test_api::test_batch_splits_company_context_and_is_idempotent`；`test_agents` 请求和引用公司隔离 | 跨公司比较是用户明确请求后的独立任务，不混入普通公司任务 |
| U09 | 有限并发与账号串行 | completed | RUNTIME_CONCURRENCY_VALIDATION：三公司真实 PostgreSQL/Redis DB14/主应用内部 HTTP，账号争用 waiting_account 后独立恢复；账号最大并发1、不同账号并发2；同公司不重入、分析上下文隔离 | 限单 Worker `--concurrency=3` 默认部署；`Settings.company_concurrency` 未被调度读取，不承诺任意多 Worker 全局上限；外部平台数据使用本地夹具 |
| U10 | 手动、定时、批次进度、重试/取消 | completed | 既有任务/计划/调度/幂等；本轮补计划城市/关键词/排除词/5来源和页面/详情预算UI，编辑保留、清空、新建重置、边界由真实构建与PlanInput通过 | UI夹具写不视为主库新建计划；本轮主库enabledPlans仍0 |
| U11 | 数据源、账号、平台内交互登录、状态展示 | completed | `browser_sessions.HumanLogin`、`browser_api`、`LoginSessionView.tsx`；真实本地/容器 Chromium frame/click/type/save，状态加密与 owner 测试 | 真实第三方扫码/账号有效性单列，不把 fixture 当登录成功 |
| U12 | 招聘官网真实采集 | completed | 腾讯首轮一页两岗位、partial、基线2；第二轮 `.runtime/recruitment-smoke-second.json` 再读1列表/2详情、observed2、新增观察2、版本2→2、first_seen/hash不变、last_seen推进、变化事件0 | 两轮均为partial，不是腾讯全量、不推断全站无变化，也不是任意官网通用能力 |
| U13 | 官网规则复用、改版后再识别 | completed | `website_agent` LangGraph、保存 plan_hash、CAS 不覆盖用户修改；`test_website_agent` 真实本地动态网页+MockTransport 模型的学习、复用、改版 | 任意无链接滚动/未知站点仍须适配，不在这里伪称覆盖 |
| U14 | 官网页内检查点与重启恢复 | completed | CHECKPOINT_VALIDATION记录32个不同用例；JSON-LD/CSS/腾讯游标、真实专用Celery/PG/本地HTTP完成未完成步骤续读与partial推进；最新远端任务面板实测显示2页证据/可接续期限 | 限已验证显式分页路径；任意无限滚动不在已证明范围。远端UI用本地HTTP样本，不冒充外部账号采集 |
| U15 | BOSS 为辅助、猎聘次辅助 | external_dependency | `platform_collectors` 契约、公司匹配、登录/验证/空正文/错误页拒绝；`test_platform_collectors`；BOSS 匿名首页可读，猎聘公开探测 DNS 失败 | 没有真实已登录公司列表→详情→入库证据 |
| U16 | 后续增加智联/前程无忧 | external_dependency | 两份 manifest、`platform_documents`、`test_extended_platforms`；真实智联公司页 12 摘要且详情 waiting_login；51job 公司入口 WAF，官方 bundle 字段与 SHA 已固定 | 不是登录后真实端到端通过；51job 详情 DOM 只有契约测试证据 |
| U17 | 岗位列表、详情、历史、变化、趋势 | completed | `ingest.py`、`analytics_service.company_metrics`、岗位 API/前端；`test_history`、`test_analytics`；已真实展示两条腾讯岗位/缺失值 | 实际岗位数量不等于招聘人数，部分来源不能用于完整排名 |
| U18 | 公司分析与独立跨公司比较 | completed | `agents.company_analysis_graph`、`analytics_service.create_comparison/execute_comparison_task`；公司/引用隔离、快照冻结、不同范围不比较测试；真实 GLM 单公司报告 `.runtime/analysis-smoke.json` | 跨公司真实业务数据不足时报告 partial；无额外模型成本的统计比较是当前实现 |
| U19 | 对话也可操作目标/任务/计划及查询 | completed | `assistant_api` 有限语法→绑定用户签名动作→既有 CRUD/任务；`test_assistant` 11 个具体场景；前端只读查询实际腾讯数据 | 是公开说明的有限语法；写动作有显式执行按钮，不是任意自然语言自治代理 |
| U20 | BettaFish 真代码调用、独立报告与原文追溯 | completed | 固定 SHA `b67928f…` 的真实 ReportAgent；最新可选容器生成 2196756 字节原始 HTML、175457 字节安全静态 HTML，分别核对 SHA；禁网、合成响应3次、真实模型0次 | 预算内简报与静态阅读导出，不代表四万字、互动脚本、PDF或真实模型语义质量 |
| U21 | BettaFish 生产激活、真实模型完整报告 | external_dependency | 版本/授权门禁、独立解释器及锁依赖、逐阶段统一网关；默认3阶段对5阶段模板在0请求时拒绝；可选依赖镜像已构建验证 | 企业适用授权、正式激活及真实模型报告质量尚未确认；专项镜像通过不等于生产已激活 |
| U22 | 舆情关键词搜索→具体条目详情 | completed | 原有搜索/详情/下一页/归属；本轮详情主资料不被低形式摘录覆盖，每次Observation/RawDocument与出处独立保存，分析区分主正文取得时间和最新观察 | 真实社交账号仍缺；无视频音频/OCR声明；保留详情不代表新一轮重新取得全文 |
| U23 | 舆情多关键词/别名覆盖不能静默遗漏 | completed | `default_urls` 已生成名称→别名→“名称+主题词”，泛主题词不单独判归属；counts.search_queries/pending_urls与成功后search_cursor轮询已有测试，内容模块103项回归记录见CONTENT_COLLECTION_VALIDATION | 真实社媒仍受登录/网络限制，不能称已查到其全文；本行完成的是多词调度与透明覆盖 |
| U24 | HR 资讯源、采集、去重、分类、摘要、订阅 | completed | 来源/日期/去重/通知；本轮补行业筛选、Feed范围和日期歧义UI，以及时间预算partial文章入库/Task行锁持有到终态共同提交，真实PG相关28项通过 | 受控UI/事务验证不代替所有真实来源连通；已有政府公开文章成功另列U25 |
| U25 | HR 资讯真实公网来源运行 | completed | 新证据 `.runtime/reputation-live-validation/live-news-ingest-result.json`：授权SSH固定DNS/TLS读取政府网就业相关资讯，真实QA任务/来源均succeeded、入库1篇、正文12625字、发布日期独立、LLM/通知均0 | 这证明该真实页面获取→当前处理链→入库；不代替所有来源或主应用各网络环境可达性 |
| U26 | 邮件、通用 Webhook、飞书机器人 | completed | `notifications.py`、事务 outbox、签名/TLS/稳定 ID/unknown；`test_notifications` 的 SMTP/HTTP 模拟及并发/CAS断言 | 真实 SMTP/飞书目标未提供，实际收件另列 external_dependency |
| U27 | 每日摘要 | completed | 上海自然日/次日HH:MM、分片/去重/版本戳原49项；本轮测试时钟修复及4个跨日参数后53项通过并纳入711，生产通知行为未改；历史QA摘要pending随后cancelled、规则/通道关闭、outbox0 | 实际接收端未配置；不把界面和模拟发送当真实收件，不默认外发空日报 |
| U28 | 采集器版本、上游检查、隔离验证、发布/回滚 | completed | 四招聘平台与official登记；固定上游差异、JSON/代码包、唯一active指针及generation CAS、已发布历史产物复核；第二轮643含相关具体回归 | 未登记上游不搜索随机仓库；最终冻结源真实远端E2E及同源主栈部署已通过，P8.4按受控工程范围完成 |
| U29 | 真实解析异常→Agent修复→验证→一次恢复 | completed | 真实 Worker/内部路由/PG/ingest 的自动恢复组合用例；修复后新增真实 EventGreenlet 回归证明本次 manifest 不混用新 active 版本、列表路径错误证据仍捕获并脱敏，四文件89项通过 | 支持有真值的详情选择器、列表路径变化；未知字段语义为 needs_engineering。默认关闭、最多2候选、恢复一次；模型与外部平台资料为受控替身 |
| U30 | 最终 Docker、无本机 Docker 时用独立 Conda | completed | `compose.yaml`、前后端 Dockerfile、远端授权独立项目构建/运行/持久性；本机 hr-research-dev；`DEPLOYMENT_VALIDATION.md` | 此行证明部署方式存在；最新代码镜像覆盖与 BettaFish 可选镜像另列 |
| U31 | 严控真实 LLM 成本 | completed | 网关输入/输出/任务/每日预算及原子预留；711回归真实调用0。独立QA唯一真实修复请求输出截断即停，未重试/切模型，7160 tokens；加历史2次3588，已知3次10748 | 主开发库历史2条与独立QA1条分别统计；预算控制通过不等于修复质量通过，无单价不推算费用 |
| U32 | 未讨论问题自主决策并记录 | completed | `docs/DECISIONS.md` 与各验证文档记录决定/约束；无用户试点作为开工前提 | “尚未审计”没有被用作开发停工条件 |
| U33 | PLAN13.2资讯行业/地区/主题结构化通知与政策生效主动提醒 | completed | 105项真实PG/模拟接收器联合，6组实际构建页面与Worker闭环；765全量和b25实际部署通过，稳定去重/改日/09:00/未知门禁 | 真实通知目标待配置，官方来源权威性基于管理员配置，不核实法律效力 |
| U34 | PLAN18.3网页/报告/日志/会话分类数据保留 | completed | 四类有限期限/宽限、权限/预览/幂等任务/文件安全/并发/恢复；26专项与765全量、13组页面、旧数据升级保留及b25部署通过 | 测试删除只在临时目录；主数据清理0，已清理文件不能承诺无备份撤销 |
| U35 | 真实模型生成可通过验证的代码修复 | unverified | 唯一glm-5.3请求failed/output_truncated；无候选、执行、发布或held-out，未重试 | 既有Mock/受控工程E2E及711全量不能替代真实修复质量；不为取得成功扩大已消耗的一次预算 |
| U36 | 舆情任务表单来源与时间范围 | completed | SentimentPage补既有API的source_ids/time_from/time_to；7组实际构建浏览器场景、61个200/202、6次仅内存提交，跨时区/主体切换/无来源分析/非法预算通过 | UI请求与状态验证不是社交账号采集；711远端范围页只读8API200/JS0/业务写0已通过 |
| U37 | Win/Mac可下载探针、主动注册/通道、Chrome登录引导、同浏览器/出口采集 | incomplete | 0.1.0三包实际发布；设备/配对/profile/命令与回执、主动WSS/轮询、Chrome引导、招聘/舆情提供器、Web设备与绑定、内部CA正常TLS已部署；801后端、Node5项、隔离真实双通道、正式下载SHA及两页只读核验通过，见 [交付记录](PROBE_IMPLEMENTATION.md) | 原生Win/Mac、真实Chrome/账号/同出口采集未验收；登录确认只saved，独立只读确认、升级提示及离线通知整合有缺口。用户接受自签HTTPS及未签名包，签名不是本轮阻塞；模拟浏览器不替代原生/站点 |

## PLAN 19 各阶段工作与交付逐项核对

下表把同阶段的不同交付拆开，避免一个已启动服务代表整阶段完成。

| 条目 | 状态 | 当前证据与精确边界 |
|---|---|---|
| P0.1 工作区、工具和专用 Conda | completed | 环境说明、锁文件、本轮所有 Python 测试使用 `hr-research-dev`；没有本机 Docker 执行记录 |
| P0.2 前后端、迁移、健康、依赖锁、Docker文件 | completed | README/requirements.lock/package-lock、Alembic、`test_application_integration` readiness、真实远端构建 |
| P0.3 schema、状态、适配契约、幂等策略 | completed | 模型/输入 DTO；T04 修复及主 API 并发验证；源版本/覆盖契约测试 |
| P0.4 BettaFish/采集器固定版本与许可说明 | completed | manifest固定引用；BettaFish固定HEAD/干净已跟踪文件核验；许可门禁与已记录冲突，不伪造授权 |
| P0.5 自主决定可审计 | completed | DECISIONS及模块验证文档；不以用户回复拖延普通实现 |
| P1.1 内部登录、公司CRUD、来源、模型配置 | completed | security/admin/recruitment_management API；模型配置为受保护环境配置和运行状态页，不泄露Key |
| P1.2 手动任务、进度、取消、重试 | completed | API/DB任务状态与TaskEvent；主应用多domain重试/取消；T04新快捷入口 |
| P1.3 存储、证据、岗位最小模型 | completed | RawDocument/Job/Version/Observation；`ingest` 与真实腾讯入库 |
| P1.4 第一个真实官网纵向结果 | completed | recruitment-smoke与analysis-smoke，开发样本标签明确，没有正式竞品伪数据 |
| P2.1 入口发现、歧义、动态浏览器 | completed | 同域入口/候选人工绑定已有证据；独立真实搜索→隔离API五候选及审计通过，CompanyWorkspace实际UI区分partial/无结果/失败留旧；最新主栈接网另列 |
| P2.2 规则保存/复用/布局改动 | completed | website_agent相关用例、plan hash与不覆盖并行编辑的CAS |
| P2.3 标准化/历史/观察/去重/变化 | completed | JobDocument与ingest、同来源键隔离、可撤销跨来源匹配测试 |
| P2.4 基线/部分覆盖/下架/重现 | completed | `test_history` 明确断言两次完整遗漏，部分失败不下架，范围改变不复用 |
| P2.5 连续两次真实官网采集差异 | completed | 第二轮真实腾讯任务 `e4025482-693a-4ca0-be59-7b10a4b65575`：1列表/2详情、partial、observed2；新增观察2，版本2→2，hash/first_seen不变、last_seen前进、变化/下架0。只证明两条相同样本本轮未变，未推断全站稳定；模型/通知0 |
| P2.6 页内中断、检查点和真实Worker接续 | completed | CHECKPOINT_VALIDATION：32个不同用例，真实独立Celery+随机专用Redis队列/PG/本地HTTP；已成功步骤不重复读，TTL/范围/损坏拒复用 |
| P3.1 Profile、交互登录、健康/锁 | completed | 本机及容器真实Chromium fixture、会话加密、所有者控制；锁共享代码存在 |
| P3.2 BOSS候选审阅/封装 | completed | 固定参考commit、原生被动响应适配，不执行未知CLI；导航秘密不入记录 |
| P3.3 BOSS/猎聘真实登录采集 | external_dependency | 平台账号缺失；运行状态可等待登录，不能以Mock签收为验收 |
| P3.4 登录后继续任务、不重复已有结果 | external_dependency | dispatcher等待来源重排、已成功SourceRun跳过有代码与受控证据；真实Redis账号等待恢复已通过。仍缺真实外部人工登录→续跑，不能把账号锁释放当成人工登录成功 |
| P4.1 多公司拆分与独立上下文 | completed | create_batch及analysis scope测试；比较单独任务只保存统计快照 |
| P4.2 有限并发上限与账号争用实际验收 | completed | 三公司实际Worker/主HTTP/Redis租约证明同账号最大1、不同账号并行、等待方不释放他人锁并独立恢复；默认单Worker并发3，非多Worker全局限额 |
| P4.3 定时、幂等、重叠控制 | completed | 既有调度行锁/时点key与T04；本轮计划筛选/来源/预算控件补齐，生产schema校验真实UI写入，保留既有值与边界通过；未在主库启用计划 |
| P4.4 Worker恢复/执行代次 | completed | 中央expected_attempt/active_execution已接；CHECKPOINT_VALIDATION真实Worker在HTTP响应期间被新代次接管后不能提交旧结果，部分结果继续推进且不重复观察 |
| P4.5 趋势、比较、原文引用 | completed | analytics/agents的窗口与证据测试；真实单公司报告、无模型独立比较 |
| P4.6 最小对话 | completed | 公开有限语法、签名动作、既有service调用及11个针对用例；非通用自然语言代理 |
| P5.1 事件/规则/outbox/发送Worker | completed | 事务回滚、并发claim/CAS、队列部署记录；没有未知目标默认外发 |
| P5.2 SMTP/Webhook/飞书/测试发送 | completed | 受控SMTP与MockTransport测试签名、TLS和拒绝；不是生产收件验收 |
| P5.3 去重/冷却/重试/记录 | completed | 稳定event ID、unknown不自动重投、显式风险确认；并发晚到结果不覆盖新代次 |
| P5.4 每日聚合摘要 | completed | 独立DIGEST记录与实际函数；超过工作预算分轮、不丢事件、通道版本变化阻止转发 |
| P5.5 真实邮件/机器人收件 | external_dependency | 尚无真实接收配置，不能擅自发送给任意人员 |
| P6.1 Bridge/独立环境/固定版本/目录隔离 | completed | 真上游Conda及真实ReportAgent产物；不同task目录、不读取latest文件 |
| P6.2 关注关键词采集与分析 | completed | 搜索→观察到的详情/下一页统一预算，多词已查/未查与有界轮询、归属边界已在内容模块103项回归中验证；真实社媒接入另列 |
| P6.3 登录/状态/原始条目/失败传播 | completed | 原文观察及受限状态；本轮低形式不降级已得详情，每次观察仍独立留原文/出处；主资料时间与最新观察区分，新增PG回归已纳入701 |
| P6.4 安全报告展示、覆盖、通知 | completed | sandbox报告路由、来源覆盖及不可执行上游HTML；舆情通知用既有outbox |
| P6.5 主平台真实上游完整报告 | external_dependency | 本地真实引擎+合成模型已过；生产授权与实际模型报告质量仍未证明 |
| P7.1 Feed/网页/栏目与原文 | completed | news_service和本地源fixture，限制同域/页数且记录原文 |
| P7.2 去重/分类/筛选/摘要 | completed | URL/内容去重原有；本轮补行业筛选页面、Feed摘要/内容标签、不确定性与日期候选；实际构建浏览器10项通过、6API200、JS0 |
| P7.3 政策日期/权威区分/汇总订阅 | completed | 日期测试区分发布/成文/生效/采集；多日期不猜；news事件可进入日报 |
| P7.4 真实外部资讯结果 | completed | 新gov真实采集/QA入库记录 `aa06f8d5-1729-4c65-93ce-cba5b6709043`；此前失败仍保留，不覆盖为伪成功 |
| P8.1 登记/上游检查/维护队列/隔离 | completed | 五类registry、固定上游元数据/有界差异、维护队列、JSON与真实Python包的独立权限及隔离客户端；第二轮643覆盖中央接线 |
| P8.2 异常→修复→测试→恢复 | completed | 真实Worker/内部路由/PG/ingest组合测试及修复后的真实EventGreenlet边界回归；候选与恢复限定有证据的JSON配置，模型与平台资料受控 |
| P8.3 不兼容候选保持旧版/回滚 | completed | shadow失败、内容指纹冲突、自动恢复失败回滚、历史发布回滚测试 |
| P8.4 配置之外的代码/依赖候选自动升级 | completed | 644/701受控A/B恢复、坏候选和旧依赖回滚及对应部署留证；当前711含提示完整性修复回归。限独立真值/预算/依赖白名单的工程能力；真实模型修复质量另列U35未验证；711同源部署已完成P9.4 |
| P9.1 既有工程集成/页面/权限验收 | completed | 旧380项/14页证明既有业务基线；第二轮643覆盖当前中央接口和权限；新增页面及远端增量另列，受限状态不冒充真实来源成功 |
| P9.2 最新并行增量全量回归与页面走查 | completed | 711真实完整回归及Ruff通过，93后端/适配器文件测试前后不变；最终范围UI本地7组与远端只读8API200/JS0/业务写0通过。701远端14页保留历史，当前只签收本轮增量页面 |
| P9.3 启动/配置/登录/备份/回滚说明 | completed | README、DEPLOYMENT、实际卷备份/重启校验和版本清单 |
| P9.4 Docker最新源码/迁移/队列验收 | completed | 711 API/Worker镜像b9cfbdf4…、Web1390d3b5…；最终123源清单122部署路径全部匹配，仅.env.example不部署，119构建清单ae9e960c…。7c8无新增迁移、Runner ready/Search200、专网3/2；10公司6绑定摘要及14保留容器未变，远端ModelCall/活动任务0；未重跑真实模型/搜索/E2E |
| P9.5 BettaFish可选容器依赖交付 | completed | 已在授权远端独立构建可选镜像，固定Git/独立venv及真实ReportEngine HTML离线通过；默认预算预拒、license=false、24既有容器未变化。见BETTAFISH_DOCKER_VALIDATION；未切换基础栈、不是当前整个平台最终部署 |
| P9.6 真实完成/待配置/缺口与自主决定清单 | completed | 本审计及各模块验证文档提供细分证据，外部接入不混同工程测试 |

## PLAN 20.1：24 项核心验收

| ID | 状态 | 精确证据/缺口 |
|---|---|---|
| T01 同名公司 | completed | `test_companies_are_not_merged_by_name`、批量same-name review、助手歧义不启动任务；候选有来源证据 |
| T02 批次与上下文隔离 | completed | `test_batch_splits_company_context_and_is_idempotent`、`test_analysis_reads_only_selected_company_and_validates_evidence`、拒绝外公司引用 |
| T03 并发与账号锁 | completed | 三家公司实际Worker/主HTTP/真实Redis，等待账号的任务不阻塞不同账号；账号最大并发1；另测同公司两个任务不能同时进入浏览器 |
| T04 重复触发 | completed | 本轮 `test_run_idempotency.py` 13项真实main API；两个快捷入口、并发、无header、参数冲突、resume原子性；实际TS helper 4项语义测试 |
| T05 岗位基线 | completed | `test_first_snapshot_is_baseline_not_new_publication`，真实腾讯 counts baseline_seen=2/first_seen=0 |
| T06 JD变化 | completed | `test_format_changes_do_not_create_content_events`、版本/事件持久化与实质变化 |
| T07 失败不下架 | completed | `test_partial_runs_never_mark_missing_jobs_removed`、登录/验证/错误页/预算边界测试 |
| T08 连续完整观察下架 | completed | `test_removal_requires_two_complete_observations_and_reappearance_is_recorded`、changed scope测试 |
| T09 跨来源一个消失另一个仍在 | completed | 真实ingest与主API合并官网/BOSS岗位，官网两次完整遗漏→该来源suspected_removed；BOSS仍observed、整体岗位observed、只一条官网下架事件；详情和列表均保留两来源 |
| T10 登录恢复 | external_dependency | 本地会话保存、等待恢复与来源幂等有实现；真实Redis账号等待续跑已过，但真实平台人工登录后续跑仍缺账号，不能以等待锁释放替代登录验收 |
| T11 登录页不是岗位 | completed | collection/website/platform tests检查正文截断、HTTP401/403与登录提示，不能成功零岗位 |
| T12 Worker重启 | completed | 既有代次/检查点恢复；本轮新闻partial最后Task行锁窗口真实PG两Session先红2项、修后相关28项通过，竞争更新等待文章/来源/Task终态共同提交，不覆盖新attempt状态 |
| T13 上下文/工具预算 | completed | 模型/采集原预算仍在；新闻达到时间预算保留已读文章为partial并停止后续来源，取消/旧代次仍拒写；计划页面预算边界验证，事件最多2候选/恢复一次 |
| T14 比较口径 | completed | `test_comparison_different_filters_or_missing_data_keep_differences_null`、快照不可变、范围变化标记 |
| T15 通知重试与秘密 | completed | `test_notifications`稳定ID/unknown/CAS/SMTP/密钥不回显；摘要超时不重投与目标变更取消 |
| T16 上游更新失败 | completed | JSON原有失败保留及代码无关diff零模型、不兼容候选不切active、未知/设施故障不花第二候选均有第二轮643内的具体回归；最终远端E2E两次坏候选实测保留B，同源主栈部署也已通过，工程范围见P8.4 |
| T17 回滚 | completed | JSON及复合代码历史回滚、完整源码/锁/回执复核、持久generation和收尾前ABA已有本地真实PG负例；最终冻结远端E2E实测回滚A后使用旧bundle/锁、beautifulsoup4 4.13.4并新增Observation |
| T18 影子不污染正式数据 | completed | `test_shadow_contract_does_not_write_jobs_tasks_or_model_calls`及候选/实际恢复分离；正式岗位仅在新采集任务ingest |
| T19 BettaFish隔离 | completed | 子进程目录测试；真实固定源码两份独立HTML目录/SHA核验；配置秘密不继承 |
| T20 舆情覆盖 | completed | source run/extent/limitations、缺失渠道不计0、搜索摘要不装作详情；最新多查询范围另列U23 |
| T21 资讯日期 | completed | 发布/采集/政策日期区分及未知不猜；本轮资讯页面展示资料形式、歧义和多个原文日期候选，不把Feed摘要当全文 |
| T22 对话动作 | completed | 绑定用户/有效期/篡改拒绝、草稿无副作用、复用create_batch/plan CRUD；读查询不调模型 |
| T23 凭据保护 | completed | 加密profile/通道、接口不回显、子进程不带环境密钥、内部接口worker token、自动证据剔除账号/导航秘密；不据此声称完成全面渗透测试 |
| T24 不可信内容不升级权限 | completed | 原URL/DNS/域/代码隔离；本轮原生BOSS/51job最终URL岗位身份门禁先红后绿53项，搜索内部例外精确到部署端点且通用抓取私网限制不放宽；不是全面安全审计 |

## PLAN 20.2：端到端操作

| ID | 状态 | 证据/仍需什么 |
|---|---|---|
| E01 登录→公司/来源→真实岗位 | completed | 腾讯smoke任务、原文和前端查看，明确开发验证样本 |
| E02 至少3公司独立运行，一家失败不阻塞 | completed | 真实主API/Worker/Redis/localhost HTTP证明failed/succeeded/succeeded、批次2/1、公司与账号隔离和锁释放；新版fixture校验descriptor/runtime_scope，独立4项2.42秒并纳入第二轮643，未删并发断言 |
| E03 岗位详情来源/时间/JD/历史 | completed | `/jobs/{id}`、JobVersion/Observation、详情组件与真实两个岗位；历史业务规则有测试 |
| E04 服务器登录中心登录后继续 | external_dependency | 容器内本地fixture交互已过；真实平台账号未提供，不能视为完成 |
| E05 对话采集/定时计划/只读查询 | completed | 助手11用例、真实只读腾讯查询；模型成本类型有明确标识 |
| E06 公司报告与比较报告有证据 | completed | 实际GLM公司报告+统计比较生成测试/远端analysis队列报告；覆盖不足时partial，不伪造排名 |
| E07 舆情经Bridge实际报告 | external_dependency | 真上游+合成回复产物已证明；主平台生产授权/真实模型舆情报告未验收 |
| E08 真实资讯来源进入列表摘要 | completed | 新政府网真实任务入库记录含URL、文章、原始SHA和日期；来源读取经授权SSH，生产关注名单未改变 |
| E09 通知测试与规则投递可追踪 | completed | 模拟SMTP/本地transport/outbox/摘要记录符合计划允许的本地接收验收；真实通道另待配置 |
| E10 适配器候选/发布/回滚 | completed | JSON配置闭环及代码复合发布/恢复编排已有真实DB/Worker测试；最终冻结源另有真实远端Runner、A/B恢复、坏候选及旧依赖回滚E2E；此行限定工程操作，同源主栈交付已按P8.4签收；无新增真实LLM |

## PLAN 20.3—20.4：页面与证据

| 条目 | 状态 | 证据/限制 |
|---|---|---|
| 列表/详情/任务/登录/错误/空/部分状态浏览器走查 | completed | 上一冻结版远端14主页面JSON/截图、登录/错误/partial、日报/检查点面板，JS/API/兼容警告0；当前代码管理页面的远端只读和本地隔离交互已按P9.2完成 |
| 长公司名/长JD/多城市/缺薪资/缺日期布局 | completed | 146字公司名/189字岗位名/4659字JD/12城市/缺薪资日期；CSS修复后列表1440/1440、抽屉760/760、JD712/712。重取顶部/底部截图前后均left680/right1440/width760、transform none，连续5次稳定，无裁切/溢出或Invalid Date |
| 刷新后任务状态保留/错误下一步 | completed | PostgreSQL任务与登录页状态、服务器重启持久性；账户失效提示，候选/配置入口 |
| 报告可打开、原文可点、统计口径一致 | completed | 真实公司报告/原文与统计页面观察、API证据/缺失值测试、最新报告页和日报面板；BettaFish真实引擎静态HTML另经禁网专项核验，不声称主栈已激活BettaFish |
| 秘密掩码/不注入公共配置 | completed | 配置API、普通用户权限、Webhook错误脱敏及本地设置页证据 |
| 最新冻结版全量测试/构建/截图/镜像指纹一致 | completed | 711真实全量的93后端/适配器源不变；并行SentimentPage最终d7ef8ac9…另经本地7组与远端只读验收。最终123清单571723e7…的122部署路径逐项匹配，镜像程序15bd8bc…/服务10aea…，119构建清单ae9e960c…；不同范围不混称相同SHA |

## 审计中修复的 T04（不是仅记录问题）

发现：原 `Companies.run` 和 `Plans.action` 不带稳定幂等键；两个快捷 API 以 `idempotency_key=None` 创建批次。Redis公司锁只串行执行，无法防止重复创建。原测试仅向通用接口手工传固定key，未覆盖用户实际入口。

本轮修复：

- 三个运行接口均接受 `Idempotency-Key`，并兼容通用接口旧正文key；头/正文key冲突、同key公司/筛选/计划/名称/触发方式/恢复前驱变化返回409。
- `create_batch` 对工作范围及键使用PostgreSQL事务advisory锁；无header旧客户端只复用相同公司集合、规范化筛选、plan的queued/running批次，不重用完成历史、不修改已有快照。
- 一次前端动作同步ref防双击；网络异常、5xx和不确定超时保留原key供人工重投，无自动重试；成功或明确终态后新动作使用新key。
- 内部 `resume_from_task_id` 在新Task创建的唯一提交之前写入快照，避免Worker抢先执行无前驱的任务；换前驱同key拒绝，无key复用不补写既有任务。
- 仅修改授权的三个run endpoint及tasks、两个前端运行handler/api helper；没有修改api.retry或中央Worker。

验证：`test_run_idempotency.py` 新13项，连同API、实际主应用、助手回归 **52 passed in 7.61s**；`scripts/test_idempotency_frontend.mjs` 执行实际TypeScript API helper的4项语义测试，fetch全部替换，外部请求0；TypeScript检查及Ruff通过。无真实LLM调用。

首轮联跑唯一失败是并行舆情重试新增显式 `selected_source_ids` 导致旧测试整份snapshot相等断言过严。主任务调整为主体/来源集合/时间/预算/render语义后，本轮52项全过；没有为使T04通过而删减相关功能。

## 审计中修复的真实 Playwright 回调边界

发现：`collect_platform` 通过两个 ContextVar 冻结 manifest 并收集修复证据，但同步 Playwright 的响应事件由 `_connection.py` 中新建的 `EventGreenlet` 执行。真实 EventGreenlet 不继承父上下文；普通假浏览器直接调用回调的旧测试无法暴露这一点。

修复前两个无网络回归实际失败：

1. 页面访问途中发布新 active manifest，回调重新调用 `contract()` 读到新标题字段，却返回旧版本指纹，发生同轮规则混用。
2. 岗位列表移动到新路径后，回调中的 `_capture.get()` 为 None，错误虽然报出，但修复事件没有 `repair_evidence`。

修复将当轮冻结 mapping 和独立 capture 状态显式传入响应回调。正常列表解析以及新路径候选/脱敏后的再次试解析都显式接收同一 mapping，不再在 EventGreenlet 内读取动态 active manifest 或依赖父 ContextVar。其他同步 DOM 路径保留原作用域机制。

`test_platform_collectors.py` 的浏览器 fixture 现统一通过真实 Playwright `EventGreenlet` 派发响应。新增两个回归先复现错误、再验证旧字段/旧指纹一致、运行中无新版本读取、列表修复证据存在且剔除导航令牌/联系人/电话。定向四文件覆盖采集、扩展平台、版本维护与完整自动恢复：**89 passed in 3.95s**，Ruff 通过。环境为 `hr-research-dev`、专用本地 `hr_research_recruit_test`；外网、真实模型和本机 Docker 调用均为 0。

此历史证据使用真实调度边界与受控浏览器资料，证明当时已修复对应缺陷；不代替真实平台账号现场采集。修复纳入了当轮380项及对应旧远端主栈清单，没有复用修复前378项指纹；后续代码维护版本另列643/644证据。

## 证据版本与来源

- 招聘真实采集：`.runtime/recruitment-smoke.json`，batch `8b824051-bd43-4fae-b649-b834814dcc5a`，两岗位partial。
- 招聘第二轮：`.runtime/recruitment-smoke-second.json`，task `e4025482-693a-4ca0-be59-7b10a4b65575`，北京时间05:27:38真实新观察；1列表/2详情共3个新检查点步骤、cached_steps=0。observed=2，其余baseline_seen/first_seen/content_changed/salary_changed/suspected_removed均0；observation新增2、version维持2、missing_count=0。首轮文件SHA未变、全局ModelCall2→2、通知事件/投递0。来源scope采用新版来源指纹，两个partial样本不能用于完整增减率；详见 [RECRUITMENT_REAL_OBSERVATION.md](RECRUITMENT_REAL_OBSERVATION.md)。
- 真实公司模型报告：`.runtime/analysis-smoke.json`，task `b3ee17b1-ac7b-4964-928a-77198a42edf1`，调用962输入/779输出tokens；此前一次失败也已计费记录，没有追加广量验证。
- BettaFish真实源码：固定SHA；最新专项镜像 compact 任务 `7a21399c-cd26-4cc9-b333-51f90aa9fbe4` 的原始HTML 2196756字节、安全静态HTML 175457字节，分别SHA核对一致；multi任务 `bee6f08b-ea87-4a48-ab69-92336d0793ed` 在默认3阶段预算时0请求拒绝。旧本地3/5次合成运行单列为历史，所有专项付费调用0。
- 新资讯真实证据：`.runtime/reputation-live-validation/live-news-ingest-result.json`，gov公开URL `https://www.gov.cn/zhengce/202609/content_7081277.htm`，HTTP200、原始61275字节及SHA、QA任务/source succeeded、入库1篇，真实模型/通知0。旧NEWS_VALIDATION中的网络失败结论不能覆盖这份后续成功记录。
- 社交匿名有界证据：`browser-probe.json` 与 `browser-probe-wb-recheck.json`，微博游客链最多2次导航后host预算停止，小红书/抖音导航失败；items保留null，未宣称匿名全文成功。
- 旧Docker源清单：`.runtime/deployment-validation-20261005/verification-results-source-final.json`，85文件，指纹 `b1297d722ceffaec7c1084ad6a8d90d527fc3620929e90ac37fff1a2ef20ee34`，只作为第一轮历史版本；不用于新检查点、日报、舆情或回调修复的验收。
- Docker旧版本机器结果真实记录：比较partial、舆情waiting_login、BettaFish blocked_configuration、资讯restricted_address、通知failed/restricted_address；这些是正确的受限状态验证，不是外部内容成功验收。
- 官网检查点：`CHECKPOINT_VALIDATION.md` 中32个不同定向用例以及真实专用Celery/Redis/PG/本地HTTP恢复证据；统一全量另包含旧代次成功、失败、缓存身份等 Worker fencing 用例。
- 三公司与跨来源：`RUNTIME_CONCURRENCY_VALIDATION.md` 记录真实锁/HTTP/PG、同账号串行、独立上下文及合并岗位单来源消失；原3项定向1.88秒。新增E02证明实际计划中failed/succeeded/succeeded终态组合，文件4项2.36秒；不是381项全套重跑。
- 日报：`DIGEST_VALIDATION.md` 49项定向（19项新增/30项通知回归），上海自然日、分片、事件去重和目标配置变化处理；不含真实外发。
- 第二轮全量：`INTEGRATION_VALIDATION.md` 378项通过，106.61秒，96个后端文件指纹前后一致，迁移头 `9d924bc617c7`；仅属EventGreenlet修复前基线。
- 第三轮全量：380项通过，107.32秒，Ruff/Alembic通过，head仍为 `9d924bc617c7`；96文件聚合SHA `4ed3a553ea6bb7275bc1615bdcf34dc55f27ad3f89951926893ffc44b8af8947`，前后无变化。`.runtime/integration-round3-source-after.json` 与JUnit有完整记录。
- 第三轮后E02：`.runtime/e02-runtime-source-validation.json` 与 `e02-runtime-junit.xml` 证明4项定向通过；逐项SHA检查仅 `backend/tests/test_runtime_concurrency.py` 改变，产品源码和适配器清单未变。
- 旧冻结主栈：`DEPLOYMENT_VALIDATION.md` 与 `verification-results-source-final-core.json`、对应14页及边界结果。部署清单SHA `92cc91d868ea5341fcd7005242676c75e71a980bf8fde2b64ccb69368c70f633` 只与当时本机快照匹配；不宣称与本轮代码维护源码相同。
- 长字段实测缺陷已闭环：初次抽屉760/4360、JD712/4336；`overflow-wrap:anywhere`修复后760/760、712/712。审计另查看 `detail-bottom-fixed.png`，连续英文按栏宽折行、正文与历史可读；日报表单09:00、检查点2页和期限、JS/API0及QA pending取消/outbox0均由机器结果核验。
- 旧岗位抽屉截图曾采到入场动画中间帧；当轮重取后连续5次边界稳定，left680/right1440/width760、transform none，scrollTop为0/2007。当轮采样未再改产品或重跑业务；这不是本轮新代码管理页的截图证据。
- 代码维护第二轮全量：`.runtime/code-platform-integration-round2-junit.xml`，643项通过；backend/adapters前后未变，UI中性文案另构建，完整记录见CODE_PLATFORM_INTEGRATION_VALIDATION。
- 上一轮代码维护最终全量：`.runtime/code-platform-integration-round3-junit.xml`，644项、0失败/错误/跳过，控制台132.06秒；`.runtime/code-integration-round3-source-before.json` 与 `...-after.json` 均113文件、SHA `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`。包含新增详情最终URL岗位身份门禁。
- 最终冻结源真实远端E2E：`.runtime/code-maintenance-e2e-dd39b8ca/verification-summary.json`为passed，driver源码SHA `48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96`，不同于113文件清单范围。A/B两次自动发布恢复、坏候选两次经回执验签后确认验证失败，保留B、CAS回滚A后实际旧依赖4.13.4与新Observation均通过；三轮影子正式表摘要不变，6次真实内部HTTP/4次Mock/0真实LLM通知登录，16既有容器未变。独立QA栈与随后主栈部署分别留证。
- 上一轮644对应主栈：`.runtime/code-maintenance-deployment-20261005/` 的final/source-manifest/inputs/control-network/stack及live-read-only/summary均有结果；Backend `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89`，Frontend `sha256:1458187644d93d621690a69f72e0736e76b282e2a21b3b0ae4d694c4da4c42b5`。118文件清单SHA `75aa288cec2fb48cd8011582f6791c78c01f5a420d42fe2a82665fb5f73ea6b8`；镜像内89程序文件与最终E2E一致，本地113与部署118的110个重叠文件逐项一致。7c8迁移无漂移，真实API/Worker Runner.health通过、匿名401、无socket；控制网精确3服务、Broker无宿主端口。远端UI只读6GET200/JS0/业务配置修改0；主库ModelCall0/代码提案0/已发通知0/运行任务0，9无关容器与PG/Redis未变。主栈0模型与本地开发库2条历史记录不混计。
- 新管理页本地QA：`MAINTENANCE_UI_VALIDATION.md`。现网只读summary SHA `3d11e7f27a708ad2f2c3d17ae053da85abf0fb7e618e1230a8e173b032a0647e`；隔离夹具summary SHA `6ec78cb5757c702676eb93e008340a3d1ab0faf161efae6484870a7ccd0ed3ed`。114夹具响应含2个故意409，7次状态变化仅内存；抽屉860/860，原文/冲突/现网截图已实际查看。

## 当前实施边界与待配置

追加审计发现的U33/U34已由765版本实现并验证，来源/时间表单U36也有独立证据。原711缺口记录保留历史，不削减要求；当前未知仍按对应条目及外部配置边界列示。

- 自动维护已实现有证据的JSON规则与受限Python/依赖升级；644/701受控整链及对应主栈已通过，当前711含提示完整性修复回归。唯一真实代码修复请求输出截断停止，质量仍unverified；任意站点/未知语义不保证自动修复，缺独立真值不继续付费。711同源增量部署已完成P9.4，未重跑真实模型或历史E2E。
- 默认单Worker并发3；没有实现任意多个Worker共享的公司全局配额。
- 任意官网无限滚动、社交视频音频/OCR和账号归属自动核实未实现，不将其混入已验证采集覆盖。
- 自有公司/产品/竞品名单、真实来源账号、通知目标、BettaFish适用授权、正式域名/TLS仍属运行配置依赖。独立搜索已部署并真实经过API，701主栈接网已验收，不再作为用户必须提供的外部条件；711同源增量部署亦已验收。
- 旧远端页面/长JD/代码管理页和701远端14页有各自冻结版本范围；计划/发现/资讯及最终舆情范围UI已本地验证，711远端范围页面只读也已签收，未重跑全部历史业务场景。真实官网只观察两条样本；新搜索真实查询也仅证明有限候选，不包装成全站采集或身份核实。

后续只按新增实际证据更新具体行，不一次性把整张表改成completed，也不以未提供正式配置要求用户先试点或拖延可独立完成的工程工作。
