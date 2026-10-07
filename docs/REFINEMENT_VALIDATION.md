# 可独立补齐的需求缺口：本轮集成验收

日期：2026-10-05（Asia/Shanghai）。本轮在上一轮644项及部署之后重新核对原需求，补齐计划范围入口、内容保留与时间预算处理、仅公司名搜索，以及原生招聘详情身份门禁。未把这些独立可实现的问题归为等待真实账号的外部依赖。

最新状态：提示与舆情范围补齐后的 **711项全量及最终远端部署已通过**，下文保留701历史过程并在末节列出最新镜像与源码。唯一真实模型修复请求因输出截断未通过，没有候选执行或发布；工程部署成功不代表真实修复质量已验证。

## 改动与验收范围

| 原需求缺口 | 实际改动 | 直接证据 |
|---|---|---|
| 计划表单无法编辑已有filters | 新建和编辑可设置城市、岗位词、排除词、五种来源、页数/详情预算；保留未改字段，允许清空，非法预算拒保存 | 实际构建React页面、localhost HTTP夹具；7场景，计划请求交给真实PlanInput验证，4次内存计划保存，19接口响应 |
| 舆情详情被后续搜索摘录覆盖 | 按资料形式决定主记录替换，同级新内容可变短；每次原文/观察仍独立保存，未知作者/日期不清空已知值；主正文取得时间和最新观察时间分开 | 真实PostgreSQL先红6/8，再绿；内容模块113项通过；主记录、RawDocument、Observation、分析输入均有断言 |
| 资讯到达时间预算丢失已读文章 | 只对time_budget保留部分结果，后续来源停止；最终Task行锁覆盖数据、来源和partial终态提交 | 两Session实际复现取消/新attempt窗口先红2项，修复后28项通过；取消/旧执行不以部分保存为由越权写入 |
| 资讯页面隐藏资料/日期边界 | 显示Feed摘要/内容/正文形式、不确定性与日期候选，接入现有industry筛选 | 真实Chromium10项检查，6API200，JS/外网/主库写入0；已视觉检查截图 |
| 名称搜索依赖用户另找服务 | 可选固定版本SearXNG＋独立Valkey，Bing中国站；搜索专用客户端只接受部署指定的完整内部接口，公网查询与候选继续受公共地址边界约束 | 独立服务真实查询成功；40项专用transport/API/PG回归；真实FastAPI到搜索再到QA候选/Audit已通过 |
| 搜索失败被普通结果文案遮盖 | 部分引擎失败显示partial和原因；失败保留历史候选；无结果不等同引擎失败；绑定已知招聘平台公司页时使用相应collector | CompanyWorkspace真实浏览器4场景、25API响应（含预期409）；绑定/候选导航/真实模型/主库写入0；四平台绑定和非公司页拒绝有API测试 |
| 原生BOSS/51job仍可接受同平台其他岗位重定向 | 原生与代码浏览器路径及匿名HTTP详情统一核最终URL岗位ID；先保留正常登录/验证判断，再拒绝错误岗位正文 | 两个真实生产函数负例先红；平台关联53项通过，未仅覆盖program分支 |

计划和搜索页面的变更只补齐既有API字段和状态；没有让模型自行添加公司、访问搜索候选或绑定来源。

## 最终本地统一检查

- **701 passed，133.90秒；0失败、0错误、0跳过。** `.runtime/refinement-integrated-junit.xml`。
- Ruff通过；前端TypeScript/Vite构建通过，3.43秒，公共依赖体积提示保留。
- Alembic check无漂移，仍为 `7c8ba429a6ea`；本轮无数据库迁移。
- 使用 `hr-research-dev` Conda 和独立 `hr_research_integrated_test`，没有本机Docker或本机候选代码执行。
- `.runtime/refinement-source-before.json` 与 `refinement-source-after.json`：123个明确实现/部署文件逐项一致，SHA均为 `7991a3b9cc7df37490aedef6e9146c0ff3c07764725124d443a31eec1ecc60cd`。本轮清单加入搜索部署文件，与上一轮113文件集合不同。
- 搜索客户端和既有管理/采集合跑69项，属于上述统一套件的子集，不重复相加。

```sh
conda run -n hr-research-dev python -m ruff check backend/app backend/tests
conda run -n hr-research-dev python .runtime/run_integrated_tests.py \
  --junitxml=.runtime/refinement-integrated-junit.xml
conda run -n hr-research-dev python -m alembic -c backend/alembic.ini check
# frontend目录
npm run build
```

## 搜索的实际数据链路

SearXNG固定版本为 `2026.10.4-d48c4b555`，仅启用Bing中国站，服务及Valkey独立于主应用数据库/Redis。服务不发布宿主端口，主API通过专属内部网络访问；Worker不需要该网络。

客户端通过 `DISCOVERY_INTERNAL_SEARCH_URL` 获得一个完整固定接口。只有与此值精确相同的内部URL走专用transport；禁止跳转、禁止继承代理、限制响应2MB和等待预算。普通官网、搜索候选和其他公网源没有获得私网例外。公网搜索接口同样在跳转时立即停止，不先跟随再检查终点。

缺少数据库搜索配置时使用部署默认值；用户显式保存的配置和关闭状态优先。搜索结果不要求number_of_results；保留查询词、响应文本SHA、返回数量、实际引擎及引擎失败信息。顶层error、错query、非JSON、超时、非2xx不会变成正常no_candidates。成功和失败均有Audit；搜索从不自动访问或绑定结果网址。

真实搜索API验收在独立QA数据库执行，测试公司为无官网的“金蝶”，role=other、notes明确标识开发验证样例。通过真实FastAPI，仅发出一次真实SearXNG查询，保存5个候选和1条发现Audit；SourceBinding、ModelCall、TaskRun均0，候选网页请求0，正式主库写入0。证据 `.runtime/search-api-e2e-0a79bbb7/`。程序源集SHA `8f6bf3dbd7de297ce86c0b02d6309a73356f93c3f627aadb7755038ef4a5782b`；该集合不同于123文件清单。

已知BOSS、猎聘、智联、前程无忧公司页在用户明确点击绑定时路由到对应数据源，并提取URL中已有的公司ID；不将平台页错误绑定成通用官网。无法识别公司入口的单岗位平台链接要求进一步核对，不猜公司标识。

## 当前运行与部署状态

本机已通过 `deploy/search/ssh_tunnel.py` 建立仅绑定 `127.0.0.1:18083` 的SSH转发。脚本每次核对授权节点、固定容器/镜像、专属网IP与健康状态；重建后再次start会重新发现IP，不调用本机Docker。私有.env仅增加搜索接口及15秒配置并留私有备份，没有输出或复制模型凭据。

确认主库没有queued/running任务或启用计划后，本地API/Worker/Beat已刷新。readiness为ready；真实GET搜索配置显示origin=deployment、enabled=true且loopback接口匹配；隧道status为healthy。此过程仅健康/配置读取，上游查询0，设置API写入0，本机ModelCall仍为历史2条。证据 `.runtime/refinement-local-runtime.json`。

同源代码维护四场景也已重跑通过：两次程序发布和实际恢复、两次不兼容候选拒绝、CAS回滚后旧BS4 4.13.4锁的实际采集。模型4次均Mock，6次真实内部HTTP，真实模型/通知/平台登录均0。证据 `.runtime/code-maintenance-e2e-a1c51a3e/`；旧644版本E2E保留历史，未替代本轮源证据。

701基线已完成远端主栈三份Compose部署、14个已登录页面及维护页只读验证，JS错误0。Backend镜像为 `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157`，Frontend为 `sha256:607170d6b73b29b8b033276686ebd7d8743496fcc188574856f6de079d453b0c`；119文件构建清单SHA `e067748baa4d8cdb5d70fdc66424e476efe57795a24948152458a9b289ecb6c3`。迁移仍为 `7c8ba429a6ea`。10家公司、6个来源绑定的全行摘要前后一致；14个保留容器（含9个无关容器）未重建。搜索网仅API/SearXNG，Runner网仅API/Worker/Broker。主栈没有模型、通知、真实搜索或正式目标写入；证据 `.runtime/refinement-deployment-20261005/final.json`。

随后新增的完整故障函数提示修复与舆情范围表单必须单独验收，不能用上述701部署签收。当前真实BOSS/社交账号、收件目标、正式竞品名单和BettaFish适用授权仍未提供，不能把受控测试或搜索候选当作真实账号采集或主体核实。

## 专项材料

- [内容保留及新闻事务边界](CONTENT_REFINEMENT_VALIDATION.md)。
- [搜索服务与真实响应](SEARCH_DEPLOYMENT_VALIDATION.md)。
- 计划UI：`.runtime/plan-filters-ui-validation-20261005/summary.json` 与截图。
- 搜索UI：`.runtime/discovery-ui-validation-20261005/summary.json` 与截图。
- 搜索transport：`.runtime/search-transport-validation-20261005/final-junit.xml`。
- 原生跳转：`.runtime/native-redirect-red-junit.xml`、`native-redirect-green-junit.xml`。
- 主实施方已查看计划筛选、资讯日期/Feed摘要、搜索partial截图，布局与实际交互结果一致。

## 后续提示修复与最终回归

701之后，代码修复提示改为按AST保留故障操作的完整函数并合并重叠窗口，避免文件片段包含函数起点却缺少后续赋值/返回。完整失败资料或关键实现无法放入16000字符时，在候选递增和模型调用前停止；输出仍限1200 tokens。真实生产源码与3000字符合成失败资料已先红后绿，75项专项通过，源码 `adapter_code_service.py` SHA `10aea9607f8c53396f0df754bc7df3f299dea7b1617fc0369e64efedc3a8bab3`。

第一次全量为707项：705通过、2失败。失败位于通知日报测试：采集观察固定在2026-10-05上午10点，发送器仍使用该时钟，而数据库投递默认时间已超过它。只修正测试的到期时间与发送器时钟，按实际60秒退避推进，并增加2020闰日/2035跨年覆盖；生产通知代码未改。通知专项53项通过。原失败JUnit与日志保留，未改写为成功。

最终完整重跑为 **711 passed，126.21秒，0失败/错误/跳过**；Ruff通过，使用项目Conda、独立 `hr_research_integrated_test`，真实模型0。证据 `.runtime/final-prompt-integrated-round2-junit.xml`，SHA `9563cf70e8c3ce2ee4d78ef6c2a4c596cd15c6f59d50259e42b2d9236eca5e1f`；概要 `.runtime/final-prompt-integrated-round2-summary.json`。这是实际完整执行的结果，未拼接定向用例生成JUnit。

该轮123文件清单有一个并行前端变化（`SentimentPage.tsx`），因此不声称全仓库冻结；93个backend/adapters文件逐项未变。前端使用其随后最终构建和专用浏览器验证签收，部署再核对统一最终源清单。

舆情范围表单最终构建通过（3.38秒），7组真实浏览器场景通过；纽约时区浏览器输入北京时间后正确提交UTC，切换对象/清空/取消重开/三种任务模式与日期/整数预算边界均经过真实RunInput验证。61个响应全为200/202、6次提交只写夹具内存，JS/外网/主库/任务/模型为0。源SHA `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11`，summary SHA `f3519eec62f2afd9713023605ccc799a34694a217c3cf88c1399b7932b67e818`；记录 [舆情范围验证](SENTIMENT_SCOPE_VALIDATION.md)。根已查看最终采集/分析截图，标签、范围说明与操作按钮可见。

前端冻结后重新逐项核对，当前123文件全部与round2 source-after相同，聚合SHA `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8`；证据 `.runtime/final-prompt-source-frozen.json`。这份最终匹配记录不把“测试过程中UI曾变化”改写成未变化。

本机确认无活动任务/启用计划后已刷新API、Worker、Beat，readiness=ready、实际Worker ping=pong，主库仍为历史2条ModelCall；没有本机Docker或刷新引发的模型请求。证据 `.runtime/final-prompt-local-stop.json`、`final-prompt-local-runtime.json`。

单次真实模型修复使用独立QA库及远端Runner，结果单列 [真实代码修复验收](LIVE_CODE_REPAIR_VALIDATION.md)。离线提示序列化第一次预检被拒时，付费意图、HTTP意图和QA库状态均未创建；该脚本问题保留原停止记录，不算已执行模型请求。

修复预检后实际执行的唯一模型请求**未通过**：`glm-5.3`、low、输入15697字符，提供商报告5960输入/1200输出tokens，finish_reason=length；网关持久记录为failed/output_truncated。没有保存候选输出、运行候选、发布或执行held-out，也没有再次请求或切备用模型。`.runtime/live-code-repair/model-call-audit.json` 与两份不可覆盖意图为直接证据。该请求消耗7160 tokens；连同主开发库历史2次3588，已知总用量为3次10748 tokens，不能据此推算未提供单价的人民币费用。自动化全量711项仍全部使用Mock，不能替代这次真实质量失败结果。

## 最终711版本的实际部署

授权服务器的独立主栈已通过三份Compose完成增量部署。此次产品变更仅 `adapter_code_service.py`、`SentimentPage.tsx`；没有重复运行真实搜索、模型或整套远端E2E。迁移仍为 `7c8ba429a6ea`，新增迁移0。

| 项目 | 最终结果 |
|---|---|
| Backend / API / Worker镜像 | `sha256:b9cfbdf4b59b3dc06942a185a51741cc07a40c23dbfe8708163026ff9628b672` |
| Frontend镜像 | `sha256:1390d3b5dfa62349b8bdbc5210935139ddda2fc2f692f73971bc9d507a762fd9` |
| 部署文件 | 最终123清单中的122个部署路径逐项匹配；唯一未部署项为非运行文件`.env.example` |
| 镜像内程序SHA | `15bd8bc15ea414af7528cbe78bb50f698d308c64ba7121575bdfeeb3d6932325`，提示服务和页面源分别匹配10aea… / d7ef… |
| 119文件构建清单SHA | `ae9e960ca9721c7830635b9fbac6eeb2052b6fe922d999b8cf1cd1fee951cd60`，与123清单范围不同 |
| 服务与网络 | 应用healthy，API/Worker Runner ready，搜索health200；Runner控制网3成员、搜索控制网2成员 |
| 业务配置保护 | 10家公司、6个来源绑定完整行hash前后相同；远端ModelCall0、活动任务0 |
| 其他服务 | 14个非更新容器保持不变，包括9个其他业务容器及数据库/Redis/Runner/Search/Valkey |
| 部署后表单 | source_ids、两个datetime-local、北京时间和历史覆盖说明可见；8个API全200、JS0、非GET业务请求0、启动任务0 |
| 备份 | 远端项目内`deploy/backups/final-711-prompt-scope-20261005`，含源码、私有配置及一致性数据/应用文件备份 |

直接证据为 `.runtime/final-711-deployment-20261005/final.json`、`source-match.json`、`build-source-manifest.json` 和 `scope-ui/summary.json`；根已读取这些结果并查看部署后的 `sentiment-scope-read-only.png`。只读页面走查验证真实部署可见性，完整表单提交/时区/边界行为由前述隔离浏览器7组验证支持。

剩余工程项仍为资讯结构化订阅/政策生效提醒和分类数据保留；它们没有被上述部署或711回归补齐，见需求审计U33/U34。真实模型修复质量为U35 unverified，真实账号/收件目标/适用授权则分别保留运行依赖。
