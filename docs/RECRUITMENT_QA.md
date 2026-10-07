# 招聘采集与登录链路 QA

失败证据驱动的自动维护闭环已完成 **87 项联动回归**，含真实公司 Worker 和内部采集接口恢复入库；见 [AUTO_MAINTENANCE_VALIDATION.md](AUTO_MAINTENANCE_VALIDATION.md)。

适配器版本维护已增加注册、影子验证、原子发布、回滚和受限模型候选；接口与 **78 项联动回归** 见 [ADAPTER_MAINTENANCE.md](ADAPTER_MAINTENANCE.md)。

后续公司录入、入口发现、来源编辑和可撤销岗位匹配已实现，完整契约与 **23 项管理/历史回归** 见 [RECRUITMENT_MANAGEMENT.md](RECRUITMENT_MANAGEMENT.md)。

日期：2026-10-05（Asia/Shanghai）。本轮由独立招聘 QA 子任务执行。本文记录真实验证与修复，不代替整个系统的验收。

## 执行边界

- 本地 Python 与 pytest 全部使用已有项目专用 Conda 环境 `hr-research-dev`。
- 未调用、安装、启动本机 Docker；未重启正在使用的 API、Worker 或前端。
- 集成测试使用专用 PostgreSQL 数据库 `hr_research_recruit_test`，与其他并行测试隔离。测试 fixture 会清表，因此此数据库仅供本 QA 使用。
- LLM 的真实调用次数为 **0**。官网学习、规则复用与改版测试均使用 `httpx.MockTransport`；子进程同时覆盖 LLM 配置为无效测试域名和测试密钥。
- 真实站点只做有界匿名入口读取；没有尝试登录、绕过验证码、批量采集或保存个人身份信息。

## 已修复的实际问题

| 问题 | 复现证据 | 修复后的行为 |
|---|---|---|
| JSON-LD 单页岗位数突破 `max_details` | 上限 2，原实现返回 3 条；末尾状态还被覆盖为 complete | 最多保留预算内岗位；发现未处理岗位时维持 partial |
| 完整性标记只检查元素是否存在 | `.end` 内容为“加载中”，原实现仍判 complete | 明确匹配结束提示文本；未知提示不作为完整性证据 |
| 登录页被当成完整的零岗位列表 | “请先登录”与结束元素同时存在，原实现返回 complete、0 条 | 进入 waiting_login，不产生完整基线 |
| 人工验证被分类为重新登录 | “完成安全验证”“访问过于频繁”原来进入 waiting_login | 独立进入 waiting_verification |
| JSON-LD 分页跨公司域自动跟随 | 下一页指向另一域名，原实现继续请求 | 不自动扩大范围，记录异常且不判 complete |
| 结束提示覆盖矛盾的总数量 | 页面声明 3 个职位、实际 1 个，并有“没有更多职位”，原实现 complete | 有总数时必须核对一致，结束文字不能覆盖矛盾 |
| 官网浏览器忽略 HTTP 错误 | 返回 HTTP 401/403 但正文含岗位 JSON-LD | 401 为 waiting_login；403/429 为 access_restricted；其他错误页拒绝采信 |
| 平台列表有数据时跳过登录提示检查 | BOSS 模拟响应有岗位，但列表页面要求登录 | 即使缓存响应存在岗位，也先验证列表访问状态 |
| 平台 HTTP 错误页可能被解析为详情 | 模拟详情 HTTP 401/404，同时有职责选择器 | 登录错误与 HTTP 错误均拒绝入库 |
| 缺少职责区域仍创建岗位 | 所有详情选择器都失效 | 跳过该条；全无有效详情时 source_contract_changed；不保存空正文伪成功 |
| 无关域名接口可撞中 joblist 规则 | 外部域名返回相同 JSON 路径及岗位字段 | 只采信对应招聘平台域名的响应 |

对应代码：

- `backend/app/collection.py`：页面可读性检查、结束标记、预算和域名范围。
- `backend/app/website_agent.py`：复用共同访问状态判定，修正总数与结束标记的优先级。
- `backend/app/browser_capture.py`：浏览器导航响应状态检查。
- `backend/app/platform_collectors.py`：列表/详情状态检查、响应来源校验、空正文处理。

## 测试证据

1. 修改前已有招聘定向测试：18 项通过，说明原有测试未覆盖上述边界。
2. 新增完整性回归首次运行：7 项失败、1 项通过，实际复现问题后再修复。
3. 修复后完整性测试 8 项通过；之后新增的 2 项真实本地 Chromium HTTP 错误测试通过，完整性文件共 10 项通过。
4. BOSS/猎聘平台定向测试（不含需要数据库的内部 API 鉴权用例）：15 项通过、1 项暂未在该命令中选择。
5. 6 个测试文件的最终合并回归使用专用 QA 库执行：**49 passed in 12.42s**。
6. 修改文件的 Ruff 检查通过。

合并回归命令（在 Conda 子进程内，`TEST_DATABASE_URL` 指向专用 QA 库；数据库口令不打印）：

```sh
python -m pytest -q \
  tests/test_collection.py \
  tests/test_website_agent.py \
  tests/test_platform_collectors.py \
  tests/test_recruitment_integrity.py \
  tests/test_history.py \
  tests/test_browser_sessions.py
```

其中包括真实本地 Chromium 的动态页面、规则学习后复用、官网布局改动后再适配、交互登录输入与加密会话保存，以及 MockTransport 模型预算记录。

## 真实匿名入口读取

本轮每站仅请求 1 个入口页面，使用项目的 `PublicFetcher` 和现有公网地址校验，不使用真实 LLM。

| 站点 | 观察结果 | 边界 |
|---|---|---|
| `https://www.zhipin.com/` | 可读；511,093 个字符；118 个 `/job_detail/` 或 `/job/` 链接 | 仅说明匿名首页可读，未验证登录后的公司页采集 |
| `https://www.liepin.com/` | `dns_error`：公网 DNS 验证失败 | 是当前网络解析链路失败，不解释为平台零招聘或确认平台禁止匿名访问 |
| `https://www.zhaopin.com/` | 可读；847,492 个字符；1 段 JSON-LD，无 `__NEXT_DATA__` | 未确认该 JSON-LD 是岗位列表；不能作为完成接入证据 |
| `https://www.51job.com/` | 可读；240,804 个字符；148 个 `jobs.51job.com` 或 `/job/` 链接 | 未进入详情或公司页，未确认企业 ID 契约 |

## 智联与前程无忧的后续适配方案

本轮没有增加空适配器或按钮；新增渠道应在官网与 BOSS 的现有路径经验证可用后继续。

1. 先用各 1 个明确公司页和 1 个详情页验证公开字段契约，保存脱敏测试样本。不能由首页岗位推荐反推用户的竞品。
2. 分开定义 `zhaopin` 与 `51job` 的公司标识、岗位标识、来源域名、详情字段和登录状态契约。前程无忧页面存在独立岗位子域名，来源域名验证要按实际平台根域实现，不能沿用官网“完全相同 hostname”的分页规则。
3. 优先读取可验证的公开结构化内容；需要浏览器时使用平台专用会话。需要人登录时接入统一登录中心，不实现验证码绕过。
4. 同时实现并验证服务端来源类型、来源录入、登录 URL、公司链接匹配、采集路径、任务状态和通知，不把 manifest 存在当作可用接入。
5. 新契约必须覆盖主体不匹配、匿名受限、HTTP 错误、分页/详情预算、缺失正文和不确定完整覆盖；不能把未知页面当成成功空列表。

## 仍不能据此宣称完成的部分

- 没有可用的真实 BOSS/猎聘登录会话，因此已登录的真实端到端采集未在本轮证明；本地会话操作和采集状态机已经测试。
- 猎聘本次读取需解决部署环境 DNS/网络可达性后继续，有界失败已记录，不重复无意义探测。
- 当前 BOSS/猎聘采集仍保守标为 partial，不据此判断全量岗位下架。
- 官网未覆盖任意无限滚动、无链接翻页或所有企业页面；现有受限 CSS 规则与改版恢复路径通过测试。
- 本文不说明其他模块或 Docker 交付已经完成。

## 第二轮：新增智联与前程无忧适配（用户扩大范围后）

此轮在主任务明确授权后实际扩展来源，第一轮“没有新增适配器”的记录只描述当时状态。

### 接入契约

| 项目 | 智联招聘 | 前程无忧 |
|---|---|---|
| `kind` | `zhaopin` | `51job` |
| 平台业务根域 | `zhaopin.com` | `51job.com` |
| 建议登录入口 | `https://www.zhaopin.com/` | `https://www.51job.com/` |
| 登录页相关静态域 | `zhaopin.cn` | `51jobcdn.com` |
| 公司标识 | `companyNumber`，如 `CZ552912730` | `coId`；公司 URL 契约 `/all/co<ID>.html` |
| 公司入口解析 | `/companydetail/CZ….htm`、`/companydetail/jobs-CZ…/`、旧 `company.zhaopin.com/CZ….htm` | `/all/co<ID>.html` |
| 列表路径 | 页面 `__INITIAL_STATE__.onlinePositions`，公司概览的 `hotAndRecommendJobList` | 被动响应 `resultbody.items[].jobListItemVO`，或对应可见 DOM card |
| 岗位标识 | `number` / `job.base.jobNumber` | `jobId` |
| 详情 URL | `/jobdetail/<number>.htm` | `jobs.51job.com/<hrefAreaPinYin>/<jobId>.html`；缺失城市路径时不猜测 |
| 来源证明 | 实际公司在招页与实际详情页的公开 JSON 字段 | 平台当前官方前端 bundle 的字段、响应路径与链接构造；manifest 记录 URL 和 SHA-256 |

已实现的调用入口仍是：

```python
collect_platform(account_id, kind, binding, identity, filters, progress=None)
```

- 对新增两个来源，`account_id=None` 或没有保存会话时先尝试有界公开 HTTP 读取；页面需要正常浏览器加载时使用临时匿名浏览器。
- 有保存会话时使用账号隔离的浏览器上下文，复用既有账号登录能力。
- BOSS/猎聘原来的必须先有保存会话的行为保持不变。
- 新来源的匿名上下文不会保存成账号会话，不会尝试刷新或提交登录信息。
- 仅解码明确的 `__INITIAL_STATE__` JSON 数据，不 eval 网站脚本；不构造签名接口或执行上游 CLI。
- 公开页面或重定向超出平台业务域会拒绝采信。招聘账号登录中心可访问的静态资源域与岗位证据域分开。
- 公司 ID 绑定时必须严格相等；详情再次核对主体。缺失正文、主体不匹配、访客截断均不会生成占位岗位。
- 所有成功结果仍为 `partial`，没有宣称全量覆盖，不据此判定岗位下架。

### 此轮真实读取证据

**智联：** 读取实际公司页 `https://www.zhaopin.com/companydetail/CZ552912730.htm` 后，验证它公开指向的在招页 `https://www.zhaopin.com/companydetail/jobs-CZ552912730/`。解析得到 12 条岗位摘要，全部 `companyNumber=CZ552912730`。字段包含岗位名称、城市、薪资、经验与学历。该公司仅为开发验证样本，没有加入用户正式竞品名单。

所测详情 `CC552912730J40315317816` 明确显示“登录查看完整内容”，且结构化状态标记 `guestJobDescClamp.visibleLines=5`、`totalLines=10`。解析器返回 `waiting_login`，没有利用初始 JSON 里的正文绕过页面展示限制。

随后使用真正的 `collect_platform(None, "zhaopin", ...)` 执行一次 `max_pages=1,max_details=1` 的端到端公开读取，真实结果为 `waiting_login`。这证明调用链、公司列表解析及受限状态处理已接线；**不证明已登录后的真实详情采集成功**。

**前程无忧：** `jobs.51job.com/companys/` 与首页公开链接指向的 `mq.51job.com/company/` 返回阿里 WAF 脚本校验页，含 `meta[name^="aliyun_waf_"]`。解析器明确返回 `waiting_verification`，不执行挑战解码。额外一次普通匿名浏览器首页导航 HTTP 200，但没有收到岗位列表响应，未编造样本。

平台首页引用的官方公开 bundle `https://js.51jobcdn.com/jbs-www/www/www-CT59qKpc.js` 明确使用 `jobListItemVO`、`jobId`、`coId`、`companyName`、`provideSalaryString`、`jobAreaString`、`workYearString`、`degreeString`、`hrefAreaPinYin`，并从 `resultbody.items` 读取列表。适配器据此实现字段解析，manifest 固定该文件 SHA-256。**实际公司详情因当前访问校验而尚未取得，详情 DOM 路径只有契约测试证据；不能将前程无忧宣称为真实端到端已验证。**

更正第一轮入口统计的含义：前程无忧首页的 148 个链接包含城市/分类目录，并不等于 148 个实际岗位，本轮没有把这些目录生成岗位记录。

### 此轮测试与中央接线要求

- 新增 `backend/app/platform_documents.py`、`adapters/manifests/zhaopin.json`、`adapters/manifests/51job.json`、`backend/tests/test_extended_platforms.py`，扩展 `platform_collectors.py` 和其测试。
- 覆盖真实字段契约、同名/不同 ID 排除、详情再次核对、缺失正文、访客截断、WAF、重定向域限制、公开完整调用、有账号浏览器调用、匿名动态浏览器回退和不保存匿名会话。
- 7 个测试文件在独立 `hr_research_recruit_test` 数据库上最终合并回归：**69 passed in 12.87s**；Ruff 通过；真实 LLM 调用仍为 0。
- 主任务需要完成中央接线：来源与账号枚举增加两个 kind；来源管理表单展示新类型；登录平台映射增加入口；`browser_internal` 允许新增平台无账号调用并采用独立来源锁；Worker 对新增来源不强制先有账号，但有账号时继续检查类型和独占锁。中央文件由主任务负责，本 QA 未修改。

完整交付仍需在合法可访问或人工登录后的真实会话里，对前程无忧公司详情和智联完整正文进行有界验收；此限制不会被 Mock 测试结果替代。
