# 当前实现状态

更新：2026-10-11（Asia/Shanghai）。完整目标仍为 [PLAN.md](../PLAN.md) 的全部V1，未通过删减计划宣布完成。功能、受控验证和真实外部验收分别列示；逐项状态以 [需求审计](REQUIREMENTS_AUDIT.md) 为准。

## 2026-10-11 远端恢复

按用户要求恢复已 down 的远端主栈、Runner 和搜索，全部七个容器网络固定为不与现场路由重叠的 `172.16.240` 至 `172.16.246` 七个 `/24`，不再依赖 Docker 自动选址。Web 入口保持 <http://10.255.200.16:18080>；真实管理员登录、原有宇树26/智元50岗、内部控制网连通均通过，现存数据卷保留，其他16个容器未变。复用原应用镜像，前端镜像变量已持久固定，无本机环境、新模型请求或全量应用重测。见 [网络恢复验证](NETWORK_RECOVERY_20261011.md)、[网段约定](../deploy/NETWORKING.md) 和 DEC-0044。下文工程测试为各自历史版本记录。

## 当前仅使用远端

新增拟议方向：用户提出Web下载Win/Mac桌面探针，由终端主动连接平台并管理本机Chrome登录/采集，以减少手工CDP和登录桥兼容负担。已补 [桌面探针方案](DESKTOP_PROBE_PLAN.md)、PLAN9.3和U37 incomplete；当前尚无探针安装包、注册/控制通道或原生终端验收，现网未切换。详见DEC-0046。

2026-10-11 用户指出关注公司混入验证样本。已将明确的11个开发/部署样本全部归档（其中1个原已归档），停用13个测试关注主体、5个仍启用的招聘来源及2个舆情来源，取消7个未完成的测试任务。真实平台API验证关注公司仅为宇树科技、智元机器人、智身新创；正式岗位26/50、计划及4条模型记录不变。测试历史保留为归档和审计，不再生成新样本；见DEC-0045。

当前入口为<http://10.255.200.16:18080>，管理员`admin`。已迁入最新源码、正式公司/数据源/计划、76条已有岗位及观察历史、官网自述、登录记录、原文证据、检查点和4条既有模型调用；不清零预算。原文内容SHA校验、管理员登录及页面访问均通过。后台与BettaFish独立运行环境已配置，真实账号登录及模型质量的边界仍保留。

按用户要求，本机前端、API、Worker、Beat、PostgreSQL、Redis、搜索SSH隧道停止，三个项目Conda环境已卸载；本机运行目录、浏览器缓存、前端依赖及.env移入废纸篓，源码文档保留。不自动重建本机环境。原`.runtime`审计、数据库与原文已归档到服务器本项目`retired-local-20261005/`，包括模型调用意图和历史失败。下文的本地记录均为迁移前历史。见[远端访问](../deploy/REMOTE_ACCESS.md)。

## 正式对象已在本地平台开始运行

用户确认竞品宇树科技、智元机器人，自有公司智身新创（别名智身科技，官网`genisomai.com`）。修复官网列表内正文、经核实的JS查询分页及本机TUN环境的国内DNS后，保存的“机器人竞品招聘监测”计划通过平台API触发、Worker执行，观察并入库宇树26岗、智元50岗。宇树列表没有可核对的全量结束证据；智元到计划50岗上限并保留检查点，均未冒充完整招聘数或推断下架。批次为`86204dbb-f922-417c-a940-a119c20a1508`。

智身舆情保存官网自述1条，来源明确标为官网；微博、小红书要求登录，抖音匿名访问返回验证码中间页，已补齐识别为人工验证。三者已创建备注型登录记录并绑定来源，仍需用户在平台完成真实登录/验证码，再重新执行采集；没有真实第三方舆情数据时不能声称无负面信息。本轮新计划使用已核对并保存的规则；早前宇树适配输出截断及其模型记录仍保留，未自动重发。

本次代码仅更新本地Conda实例；远端没有同步本次源码或正式业务数据。代码与配置决策见DEC-0042；真实运行记录在`.runtime/formal-use-20261005/`。既有765项是历史工程基线，本轮有针对性回归、前端构建和真实目标运行证据，不把这些证据互相替代。

## 最新765版本

资讯结构化订阅/政策施行提醒、分类保留已补齐；765项完整回归通过，147.16秒，131个实现输入不变。Conda本机与授权Docker主栈均升级b25d0c8f3142，API/Worker健康且新任务已注册。10家公司、6绑定、原文/报告/任务/日志的原列摘要保持；14个其他容器未变。主数据清理0、新增真实模型0，四策略在7天宽限。详见[LIFECYCLE_VALIDATION.md](LIFECYCLE_VALIDATION.md)。

## 711历史版本的缺口修复与源码状态

711历史完整回归为 **711项通过、0失败/错误/跳过，126.21秒**，Ruff通过；使用项目Conda及独立 `hr_research_integrated_test`，真实模型调用0。JUnit为 `.runtime/final-prompt-integrated-round2-junit.xml`，SHA `9563cf70e8c3ce2ee4d78ef6c2a4c596cd15c6f59d50259e42b2d9236eca5e1f`；概要为 `final-prompt-integrated-round2-summary.json`。该轮123文件清单从 `0919f1b0b33441b6379ce5c4522bfb51ce586075d429f6f288ae1b868a0e7675` 变为 `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8`，唯一变化是并行的 `frontend/src/SentimentPage.tsx`；93个后端/适配器文件逐项一致，不能声称全仓库未变。最终前端另有7组专用验证；711版同源增量部署与远端范围页面只读验收也已通过。

此前707项完整执行为705通过、2失败，原JUnit与日志保留。两处失败是日报测试把发送时钟固定在2026-10-05上午10点，而投递模型默认到期时间来自真实墙钟；仅修测试使到期时间与发送器时钟一致，按实际60秒退避推进，增加到期前不发、2020闰日和2035跨年验证。通知专项53项通过后才完整重跑得到711；没有修改生产通知行为或拼接JUnit。

701基线的133.90秒全量、Ruff/Alembic及123文件同SHA `7991a3b9cc7df37490aedef6e9146c0ff3c07764725124d443a31eec1ecc60cd` 保留为历史。**701版主栈、搜索控制网及远端页面部署已完成**，证据见下文和 [SEARCH_DEPLOYMENT_VALIDATION.md](SEARCH_DEPLOYMENT_VALIDATION.md)；701和711各有独立部署证据，不能互相借用版本范围。迁移head仍为 `7c8ba429a6ea`，本轮没有新增迁移。

本轮补齐了原先只有接口而缺页面的计划城市/关键词/排除词/来源及页面/详情预算控件；编辑保留既有值、清空数组、新建重置、预算和标签边界已有实际构建UI与生产schema验证。CompanyWorkspace现在分别展示成功、部分引擎失败、无结果和失败保留旧候选，不再把partial当完整成功。原生BOSS/51job的详情最终URL岗位ID检查由2项先红复现，再纳入53项定向回归；招聘管理/搜索联合69项通过。所属定向有重叠，不与701或711相加。

舆情详情保留主资料并保存每次摘录观察；新闻时间预算partial保留文章，最终Task行锁与文章/来源/终态同事务提交，真实PG两Session先红2项、修后相关28项通过。资讯页展示Feed形式、日期候选/歧义和行业筛选。见 [CONTENT_REFINEMENT_VALIDATION.md](CONTENT_REFINEMENT_VALIDATION.md) 与 [REFINEMENT_VALIDATION.md](REFINEMENT_VALIDATION.md)。

最终舆情任务页面已接入既有API的来源选择、北京时间范围和预算：7组实际构建浏览器场景、61个200/202响应通过，6次提交仅内存，JS/外网/模型/主库写0；覆盖跨时区转换、切换主体清空来源、取消重开、无来源分析和非法整数。源SHA `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11`，证据 `.runtime/sentiment-scope-ui-validation-20261005/summary.json`；这是前端请求及状态验收，不是社交账号真实采集。

独立SearXNG/Valkey已部署并真实查询；客户端经真实FastAPI HTTP和隔离QA库完成一次名称搜索，保存5个候选及Audit，SourceBinding/ModelCall/TaskRun均0，没有访问候选站点。“需要用户另找搜索服务”已不再是U07外部前提。701主栈搜索控制网、实际部署端点及14个已登录页面已经验收；711版同源更新也已验收；701记录保留历史完成状态。详见 [SEARCH_DEPLOYMENT_VALIDATION.md](SEARCH_DEPLOYMENT_VALIDATION.md)。

## 提示修复与唯一真实模型请求

生产提示现按AST定位并优先保留故障操作的完整函数，再加入其他源码和上游差异；重叠窗口合并，完整失败资料或关键实现放不下时，在候选次数递增和模型调用前报input_budget。生产源码加3000字符独立`.jd-live-qa`失败资料先红后绿，75项专项通过；实际提示15691字符，parse_detail的114–151行完整，省略行有审计。输入16000字符、输出1200 tokens预算未增加，候选未在本机执行。

随后唯一真实质量请求使用独立QA库、`glm-5.3`、low，输入15697字符；提供商报告5960输入/1200输出tokens，finish_reason=length。网关持久状态为 **failed/output_truncated**，没有保存候选输出、执行候选、发布或开展held-out验证，也没有重试或切换备用模型。故真实代码修复质量为 **unverified**，不是passed；这不抹去既有受控工程闭环，也不能被711项Mock回归替代。直接证据为 `.runtime/live-code-repair/model-call-audit.json` 与不可覆盖的请求意图；详细记录由 [LIVE_CODE_REPAIR_VALIDATION.md](LIVE_CODE_REPAIR_VALIDATION.md) 单列。

本次独立QA消耗7160 tokens；主开发库历史2次为3588 tokens，分库合计已知3次10748 tokens。没有提供单价，不推算人民币费用；主开发库仍为2条，不能把独立QA新增调用混写为主库3条。

## 上一轮P8代码维护基线（644项）

代码维护中央接线已落地，上一轮补上同平台详情最终URL岗位身份门禁后的统一回归为 **644项通过、0失败，132.06秒**。113个显式实现文件前后完全一致，SHA `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`。迁移head为 `7c8ba429a6ea`；上一380项、远端96文件/14页及第二轮643均保留各自版本范围。

已接入固定上游差异与失败触发、Python/依赖候选、可信oracle、独立Runner客户端、复合发布/回滚、实际Runtime持久调用、平台/泛型官网/腾讯程序路径、原生失败证据桥、一次恢复和管理页面。代码权限独立于JSON权限；未知、取消、设施故障不触发付费重试。旧JSON恢复继承旧版本和收尾前ABA的问题已修复。独立Runner与13项真实代码组件联合验证另有证据，但不等于完整业务Worker远端E2E。

第二轮643期间backend/adapters逐文件一致，唯一UI中性文案变化后单独重建；最终644期间113文件整体也未改变。当轮前端使用中性文案构建；本轮其他页面增量另列。本地管理页现网只读和隔离API交互QA均通过，后者明确是内存夹具、未修改主库。见 [CODE_PLATFORM_INTEGRATION_VALIDATION.md](CODE_PLATFORM_INTEGRATION_VALIDATION.md) 与 [MAINTENANCE_UI_VALIDATION.md](MAINTENANCE_UI_VALIDATION.md)。

**上一轮P8.4与对应P9工程验收为completed**：最终本地644、最终冻结源真实远端E2E以及同源码主栈部署均通过。E2E完成A/B两次发布恢复、不兼容候选保留及旧依赖实际回滚；主栈已完成迁移、真实API/Worker控制网、队列smoke和远端维护页只读QA。验收限已实现的有证据、受预算约束升级，不代表任意站点自治维护、真实平台账号或真实模型质量已验证。后续依据 [CODE_MAINTENANCE_INTEGRATION.md](CODE_MAINTENANCE_INTEGRATION.md) 更新对应版本证据。该轮代码维护回归的外部业务网站、真实模型及真实收件调用均为0，主开发库ModelCall只有早期2条；本轮独立真实搜索另列。

## 环境与用户约束

- 本机不调用、安装或启动Docker。应用Python为新建的 `hr-research-dev`（3.11.13），PostgreSQL16.15/Redis8.10.2由专用 `hr-research-services` 提供；分别监听127.0.0.1:55432/56379。
- BettaFish独立环境为 `hr-research-bettafish`，固定上游 `b67928ff1ce515dd7672408221fb1e9500d0972d`，依赖独立锁定。
- Docker只在授权SSH节点 `dabing@10.255.200.16` 的独立项目验证；不修改其他项目、全局Docker代理或现有服务。可选代理为SOCKS5，不作HTTP代理使用。
- 已确认的环境、部署、自主决策、验证和成本约定见 [AGENTS.md](../AGENTS.md)。未讨论的普通工程问题自主决策并记录，不因“待审计”停工。
- 新增取舍见 [DEC-0020—0039](DECISIONS.md)：除预算简报、静态报告、自动修复、日报、检查点、执行代次、舆情归属、独立Runner和统一版本证据外，DEC-0034—0039还记录独立搜索、内容保留、资讯时间预算、计划范围/平台身份、完整故障提示与单次真实修复失败、舆情来源/时间范围。
- 私有配置位于忽略的 `.env`（0600），管理员访问资料位于 `.runtime/local-access.txt`，不写入报告或源码。

## 可运行的业务链路

| 模块 | 已实现 | 重要边界与证据 |
|---|---|---|
| 公司与任务 | 表单/批量公司、身份/来源绑定、入口发现、监测计划、单公司任务/上层批次、幂等键、重试/取消 | 不推测正式竞品；独立搜索真实隔离API生成5候选，701主栈接网及本机端点已验证；711同源增量部署已通过。已有官网可直接发现招聘链接 |
| 官网招聘 | 原生JSON-LD/CSS/腾讯路径，以及冻结程序的列表/详情/导航；受控规则学习、检查点、接续与原观察时间 | 第一方inventory/分页/详情/总数或结束证据决定complete；遗漏/截断不完整；原生→代码保持岗位身份，任意无限滚动不在已验证范围 |
| 招聘平台 | BOSS、猎聘、智联、前程无忧适配；独立人工登录、加密会话和账号锁 | 原生BOSS/51job最终URL岗位身份门禁先红后绿53项已纳入701；真实登录后采集仍无用户账号验收，匿名/截断/WAF证据见RECRUITMENT_QA |
| 岗位与分析 | 来源身份、版本/观察/变化、两次完整遗漏、可撤销跨来源匹配；日趋势、冻结快照比较、单公司LangGraph简报 | 旧缓存不刷新观察时间、不覆盖新版本；部分采集不判下架；模型引用不能串公司 |
| 对话入口 | 查询岗位、添加关注、采集/计划/比较动作草稿 | 首版限定中文指令，明确可执行动作及成本；不是任意自然语言代理 |
| 整体舆情 | 关注公司/品牌/产品；任务来源/时间范围/预算表单；搜索→详情→下一页、实体归属与查询轮询、来源覆盖、导入、收藏/备注/忽略、规则摘要 | 最终范围表单7组本地验收通过，真实社交账号仍缺；正文/摘录/视频文案区分，较低形式不覆盖已得详情，每次观察/出处独立保存；未知不是零 |
| BettaFish | 独立Bridge、固定真实ReportAgent、预算预检/简报、静态导出、可选Docker镜像 | 上游和渲染真实执行，模型响应合成；许可false，真实模型质量未验收 |
| HR资讯 | Feed/网页/可选浏览器、原文去重、地区/主题/行业筛选、资料形式/政策日期歧义、收藏与通知 | 真实政府文章已有QA入库；预算partial保留文章且持Task行锁到终态同事务提交，两Session验证；未预置用户偏好 |
| 通知 | SMTP、通用Webhook、飞书；规则、即时/每日摘要、事务outbox、未知结果、稳定事件/通道版本 | 默认不外发；日报按平台收到事件的自然日，启用前历史不补发；真实目标仍未配置 |
| 自动维护 | JSON与真实Python/依赖候选、增量补丁、隔离签名回执、第一方oracle、唯一指针/代次、触发/发布/一次恢复/回滚和页面 | 独立代码开关默认关闭；共享事件最多2候选。644/701受控整链与对应部署均为历史完成；711提示修复回归与同源增量部署均通过。唯一真实修复请求输出截断，质量unverified；本机不执行候选 |
| 运行管理 | 内部登录、可撤销会话、管理员权限、审计、预算与调用记录 | 只有管理员维护外发/适配/内部用户；页面不显示秘密 |

## 实际验证与版本范围

后端源码持续更新时，旧通过记录仅是历史基线。每次最终回归/构建记录源码指纹、迁移和对应产物，不以测试数量替代需求覆盖。

- 当前开发库head为 `7c8ba429a6ea`，包括发布代次、代码包/提案、实际程序调用与独立权限开关；`9d924bc617c7`、`8b7fd5e97379`是历史迁移阶段。
- 上一轮代码接线全量：**644 passed，132.06秒**；JUnit `.runtime/code-platform-integration-round3-junit.xml` 为644测试/0失败0错误0跳过（JUnit时间131.905秒）。`.runtime/code-integration-round3-source-before.json` 与 `...-after.json` 的113文件SHA均为 `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`；Ruff通过。
- 上一轮冻结源真实远端E2E：`.runtime/code-maintenance-e2e-dd39b8ca/verification-summary.json`为passed，源码SHA `48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96`。该driver清单与113文件范围不同。两次A/B发布恢复各新增Observation；两次坏候选均经真实签名回执拒绝且保留B；CAS回滚A后真实使用旧锁/beautifulsoup4 4.13.4并新增Observation。三轮影子正式表摘要不变；6次内部HTTP、4次Mock、0真实LLM/通知/平台登录，16个既有容器未变。这是独立QA栈；随后完成的主栈部署另有独立证据。
- 第二轮统一全量 **643 passed，135.89秒**；该轮前后backend/adapters未变，维护页中性文案改动单独构建通过。首轮635 passed/3 failed是夹具不兼容新增descriptor/runtime_scope，保留失败JUnit，未删并发断言。详情最终URL身份门禁已纳入后续644，不能倒写为643原来就覆盖。
- EventGreenlet修复后冻结全量：**380项通过、0失败0跳过，107.32秒**；Ruff通过，Alembic check无差异；96个后端文件前后指纹一致（`4ed3a553ea6bb7275bc1615bdcf34dc55f27ad3f89951926893ffc44b8af8947`）。修复前378项仅是历史基线。完整记录见 [INTEGRATION_VALIDATION](INTEGRATION_VALIDATION.md)。
- 上一轮代码管理页前端TypeScript/Vite构建通过（3.34秒）。代码管理页本地现网只读6个GET全200、JS/外部请求0；隔离API夹具覆盖15阶段、开关联动、源码/依赖diff、CAS冲突与恢复文案，114响应含2个刻意409，7次变更仅内存夹具。抽屉860/860，无溢出。最新远端维护页同脚本只读QA也通过，见CODE_RUNNER_DEPLOYMENT；本地隔离夹具证明受控交互，远端只读不声称真实主库发布；旧14页/长字段仍为历史回归范围。
- 官网检查点：真实独立Celery进程、随机Redis队列、PostgreSQL与本地HTTP，32个不同用例；验证不重复读取、部分入库接续、真实观察时间、过期/范围变更/损坏拒用及旧代次拒写。
- 真实并发：三公司、同账号串行/不同账号并行、实际dispatcher恢复、HTTP503一家失败而其余成功、分析上下文与跨来源状态隔离。新版夹具显式接受并核验descriptor/runtime_scope，独立4项通过2.42秒后纳入643项；旧380后的4项记录仍为历史，不把新源码说成与旧版本相同。
- 自动维护：87项相关回归及真实业务Worker→内部HTTP→事件→模拟Agent→发布→新恢复任务→Worker入库闭环；真实EventGreenlet回归已先红后绿，修复后相关四文件89项通过；外部浏览器/LLM替身不冒充真实平台验收。
- 通知/日报：原49项回归保留；本轮只修测试时钟并增加4个跨日期参数场景，53项全部通过并纳入实际711全量，生产通知代码未改，无真实外发。采集模块联合103项历史回归另列。
- 真实资讯：国家统计局2026-09-16文章经授权节点读取、真实 `execute_news_task` 入独立QA库，正文12625字，added=1/pages=1；原先直连失败是历史记录，最新成功见 [CONTENT_COLLECTION_VALIDATION](CONTENT_COLLECTION_VALIDATION.md)。
- 真实招聘开发样本：腾讯连续两轮均限制一页列表、最多两个详情，保持partial；公司为“腾讯（开发验证样本）”，role=other，不是用户正式竞品。第二轮真实3步保存检查点，新增2条观察、版本仍2条，无内容/薪资/下架变化；hash/first_seen不变、last_seen真实推进、missing_count=0。该任务模型与通知均0，首轮证据SHA不变；分别见 `.runtime/recruitment-smoke.json`、`recruitment-smoke-second.json`。这里只能证明两条样本无变化，不能推断全站招聘稳定；详细记录见 [连续真实官网观察](RECRUITMENT_REAL_OBSERVATION.md)。
- 真实模型已知累计3次、10748 tokens：主开发库早期2次3588（输入1909、输出1679），一次输出无正文、一次生成有岗位引用的简报；独立QA代码修复新增1次7160（输入5960、输出1200），failed/output_truncated后停止，无重试/备用模型/候选执行。自动化711项真实模型调用0，不能与独立QA混计；无价格依据，不推算货币费用。
- BettaFish真实固定上游分别在独立Conda及非root、禁网、只读根目录的可选容器运行；3次合成响应、真实请求0，超预算在首笔模型调用前拒绝。原始HTML约2.2MB、安全静态HTML约175KB，分别记录hash。见 [BETTAFISH_UPSTREAM_VALIDATION](BETTAFISH_UPSTREAM_VALIDATION.md) 与 [BETTAFISH_DOCKER_VALIDATION](BETTAFISH_DOCKER_VALIDATION.md)。
- 上一冻结版本的完整主栈已在授权SSH节点增量部署验收：迁移head一致、服务健康、完整七队列、权限/内部代次门禁通过；14主页面及新增日报/检查点、长字段边界通过，JS/API错误0。96个部署允许源文件本机/远端指纹一致（`92cc91d868ea5341fcd7005242676c75e71a980bf8fde2b64ccb69368c70f633`）；9个既有业务容器未变。部署测试模型/通知/真实登录均0，临时日报pending已取消、通道关闭、outbox0。准确镜像及各范围证据见 [DEPLOYMENT_VALIDATION](DEPLOYMENT_VALIDATION.md)。可选BettaFish镜像仍独立，主栈未切换该变体。

## 711版同源增量部署（completed）

最终部署已通过，直接证据位于 `.runtime/final-711-deployment-20261005/` 的 `final.json`、`source-match.json`、`build-source-manifest.json`、`scope-ui/summary.json`。API与Worker镜像均为 `sha256:b9cfbdf4b59b3dc06942a185a51741cc07a40c23dbfe8708163026ff9628b672`，Web镜像为 `sha256:1390d3b5dfa62349b8bdbc5210935139ddda2fc2f692f73971bc9d507a762fd9`。

最终123源清单 `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8` 的122个部署路径逐项匹配，仅非运行文件 `.env.example` 不部署。镜像内程序集合SHA `15bd8bc15ea414af7528cbe78bb50f698d308c64ba7121575bdfeeb3d6932325`、提示服务SHA `10aea9607f8c53396f0df754bc7df3f299dea7b1617fc0369e64efedc3a8bab3`、舆情UI SHA `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11` 均对应本轮冻结实现。119个构建输入另有清单SHA `ae9e960ca9721c7830635b9fbac6eeb2052b6fe922d999b8cf1cd1fee951cd60`，不同范围的聚合SHA不混称相同。

迁移仍为 `7c8ba429a6ea`、无新增迁移；API/Worker的Runner检查ready，搜索健康200。Runner专网精确3成员、搜索专网2成员。主库10家公司及6个来源绑定完整摘要不变，远端ModelCall/活动任务均0；14个保留容器不变，包括9个无关业务容器。

远端最终舆情范围页面只读检查8个API全部200、JS错误0、非GET业务请求0、新任务0。这里只签收部署、同源、健康及本轮增量页面；没有重新调用真实模型、真实搜索或重跑代码维护E2E，不把历史整链扩大成新的真实质量通过。U33/U34仍incomplete，U35真实修复质量仍unverified，外部账号/授权/收件条件保持原边界。

## 701版主栈部署（历史完成）

701冻结源码对应主栈已完成三份Compose部署、搜索网接入、迁移、14个已登录页面与维护页只读验收，JS错误0。Backend镜像为 `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157`，Frontend为 `sha256:607170d6b73b29b8b033276686ebd7d8743496fcc188574856f6de079d453b0c`；119文件构建清单SHA `e067748baa4d8cdb5d70fdc66424e476efe57795a24948152458a9b289ecb6c3`。搜索网精确为API/SearXNG，Runner网为API/Worker/Broker；主库10家公司及6个来源绑定全行摘要未变，14个保留容器未重建，主栈模型/通知/活动任务/代码提案均0。证据 `.runtime/refinement-deployment-20261005/final.json`，详见 [SEARCH_DEPLOYMENT_VALIDATION.md](SEARCH_DEPLOYMENT_VALIDATION.md)。

711提示修复与最终舆情范围页面随后另行完成同源增量部署，具体镜像与验证范围见前一节；没有复用701镜像签收。

## 644版主栈部署（历史）

上一轮644对应主栈已在授权SSH节点完成备份、构建和7c8迁移，Alembic check无漂移。Backend实际镜像为 `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89`，Frontend为 `sha256:1458187644d93d621690a69f72e0736e76b282e2a21b3b0ae4d694c4da4c42b5`；118文件部署清单SHA `75aa288cec2fb48cd8011582f6791c78c01f5a420d42fe2a82665fb5f73ea6b8`。镜像内89文件程序源码SHA与最终E2E一致；本地113文件清单与部署清单110个重叠文件逐项SHA一致，不能把三个不同范围的聚合SHA写成相同。

实际API/Worker均通过第一方Runner.health和匿名401检查；控制网只有API/Worker/Broker，API/Worker无Docker socket，Broker无宿主端口。Beat→Worker任务正确进入waiting_login，Worker→API鉴权通过。远端维护页为5来源、15关闭开关、6个GET全200、JS0且无业务配置/提案修改；临时UI探针的host network只用于访问loopback页面，不作为控制网证据。主栈ModelCall=0、已发通知=0、代码提案=0、queued/running=0；9个无关容器及原PG/Redis未变。主栈零模型记录与本地开发库历史2条分别统计。

证据为 `.runtime/code-maintenance-deployment-20261005/` 的final/source-manifest/inputs/control-network/stack及live-read-only/summary，说明见 [CODE_RUNNER_DEPLOYMENT.md](CODE_RUNNER_DEPLOYMENT.md)。Web仍仅绑定远端127.0.0.1:18080，所有代码维护自动开关和真实模型维持关闭。

## 本地使用

前端：<http://127.0.0.1:5173>。API：<http://127.0.0.1:8000>。本轮已启用仅loopback监听的搜索SSH隧道、最小更新私有部署端点并刷新API/Worker/Beat，readiness为ready；实际GET搜索配置为origin=deployment、enabled=true且loopback端点匹配，未新增查询或设置写入，证据为refinement-local-runtime.json。主库activeTasks=0、enabledPlans=0、ModelCall=2，没有新增正式目标/计划。后台完整队列为 `recruitment,analysis,sentiment,news,notifications,maintenance,celery`。第二轮真实腾讯观察是早期原生路径证据，本轮没有借它宣称新程序已真实外采。

先录入关注对象和来源，然后手动运行单公司或上层批次。官网“接续重试”保留有效检查点，“从头采集”建立新观察。来源等待登录时在数据源中心完成相应平台人工登录，账号互斥不阻塞其他公司的独立账号。规则与自动任务按用户配置启用；当前未配置真实通知收件人。

## 尚未完成与外部条件

701和711主栈各自范围的部署验收均已完成；711采用最终共同源清单与远端范围页面只读证据。真实代码修复请求因输出截断失败，质量unverified；不能归因于未提供账号或用Mock补成成功。

PLAN13.2结构化订阅/政策日期提醒与PLAN18.3分类保留已在765版本兑现，证据见本文件最新节及生命周期验收。以下外部条件继续独立列示。

1. 正式竞品、自有品牌/产品、HR来源偏好尚未输入。平台提供输入和管理入口，不凭实施方判断添加正式业务目标。
2. BOSS/猎聘等以及小红书/抖音/微博的真实账号尚未提供；匿名社交浏览器访问有访客登录/网络限制，不能推断“没有舆情”或承诺登录后一定全量可采。
3. BettaFish实际适用许可与真实模型质量尚未验收；源码/依赖/合成流水线已完成，不能再把“上游未安装”当当前状态，也不能擅自确认许可。
4. SMTP/飞书/通用Webhook真实目标未提供；管理员可配置启用，当前仅模拟或连接前拒绝验证，无真人消息。
5. 任意网站无限滚动、未知字段语义、企业官方账号所有权自动认证不在当前已证明能力内；真实站点适配失败应保留明确原因和具体工程入口。
6. 单API、单Worker并发3、单Beat是V1部署约定，不宣称任意横向扩容全局限额已完成。前端公共依赖主包仍有体积提醒，构建通过；完整聊天历史不是当前助手功能。

这些条件不阻塞已经授权的独立工程工作。最终验收按PLAN的T01—T24、E01—E10逐项列明；不能仅凭平台可以打开就把整个目标标为完成。
