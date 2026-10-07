# 招聘管理与匹配实现记录

日期：2026-10-05。此轮由招聘 QA 子任务完成，主任务负责中央路由注册、迁移与前端集成。

## 实际功能与调用契约

- `POST /api/v1/companies/bulk`：逐行 `names` 或结构化 `companies`，单次最多 100 家。已有同名及同批重复项返回 `existing_review`，不自动合并法律主体、不覆盖已有官网、不加入猜测公司。
- `POST /api/v1/companies/{id}/entry-discovery`：优先只读取用户已填写的官网首页，检查同域招聘链接与 JSON-LD 元数据；保存标题、源 URL、内容 SHA-256、发现时间和方法。候选本身不被读取，也不自动创建采集来源。
- 仅有公司名时，使用明确保存的搜索配置；没有保存值时可使用部署提供的可选SearXNG接口。两者皆无则返回 `search_configuration_required`。管理员仍可通过 `GET/PUT /api/v1/settings/recruitment-discovery` 配置或关闭。只有与 `DISCOVERY_INTERNAL_SEARCH_URL` 精确相同的完整内部接口可走专用客户端；公网搜索和所有候选/官网仍受公共地址限制，跳转先拒绝。没有放开通用PublicFetcher私网访问。
- `GET /api/v1/companies/{id}/entry-candidates` 查看候选；`POST /api/v1/entry-candidates/{id}/bind` 显式绑定，`/reject` 拒绝。已拒绝状态不会被后续发现自动重置。腾讯官网复用 `tencent`；BOSS/猎聘/智联/前程无忧公司页按已知域名/路径识别对应来源和已有公司ID，无法识别公司入口的单岗位链接拒绝自动绑定成通用官网。账号仍需平台内人工配置。
- 搜索只发一个有限查询，最多检查100条响应记录并保存配置数量的有效、不重复候选；无效URL不占候选名额。响应中的引擎失败、查询词和文本SHA写入证据及Audit；零结果与引擎失败区分，partial在页面可见。失败不清空既有候选，不新增TaskRun、SourceBinding或模型调用。
- `PATCH /api/v1/source-bindings/{id}`：网址、账号、平台公司 ID、启用状态、配置。校验平台域名、账号类型、公开网络地址、CSS 规则、配置大小与禁止密钥字段，并保存修改前后审计。已存在 TaskRun 的冻结来源快照保持不变。
- `POST /api/v1/companies/{id}/job-matches`：在同一公司内按标题、非空地点、非空招聘主体、至少 40 字标准化正文筛选跨来源候选；正文四字片段相似度需达到 0.9。返回相似度、指纹、源记录 ID、当前内容 hash 和原文链接，不自动合并。GET 同路径查看已保存候选。
- `POST /api/v1/job-matches/{id}/merge`：人工填写 `reason` 并指定两个候选之一的 `target_job_id`；合并前重新校验主体和证据是否过期。只改变来源记录归属，旧 Job 保留为 `merged`，版本、观察、变化记录不删除。
- `GET /api/v1/companies/{id}/job-merges` 查看操作记录；`POST /api/v1/job-merges/{id}/undo` 恢复来源归属，并根据最新来源数据恢复岗位状态。已有后续归属变化时返回冲突，不覆盖后续操作；活动合并不允许互相叠加，先撤销再调整组合。
- 匹配默认最多读取 500 条当前来源记录、执行 3,000 次候选比较，超出时明确返回 `partial`，不暗示已比较全部岗位。

## 代码与持久化

- `backend/app/recruitment_management_models.py`：搜索配置、入口候选、岗位匹配候选、合并记录四张表。
- `backend/app/recruitment_discovery.py`：批量公司、官网/搜索发现、显式来源绑定。
- `backend/app/recruitment_search.py`：固定内部接口、部署默认与保存配置优先级、有界传输和SearXNG响应/失败契约。
- `backend/app/recruitment_matching.py`：候选证据、人工合并、撤销和后续操作保护。
- `backend/app/recruitment_management_api.py`：认证与输入校验、API、审计。
- `backend/tests/test_recruitment_management.py`：数据库和接口回归。
- 主任务完成迁移 `5db56eb1bf2c` 和中央注册，在岗位列表/统计中排除已合并旧 Job；此子任务未修改 `main.py/models.py/api.py/workers.py/ingest.py`。

## 验证证据

当前补充：40项专用搜索transport/API/PG测试，加既有管理/采集合跑69项通过；四平台公司页绑定不误用careers、单岗位平台链接拒绝有实际API用例。最终纳入701项全量。本轮真实SearXNG查询及独立真实FastAPI→五候选/Audit已验收，SourceBinding/ModelCall/TaskRun0，未写正式目标；见 [REFINEMENT_VALIDATION](REFINEMENT_VALIDATION.md)。CompanyWorkspace新增partial/错误/无候选显示，4个实际构建浏览器场景通过。下列23项是早期阶段记录，不替代本轮。

- 管理与岗位历史两个测试文件使用独立 `hr_research_recruit_test` 数据库、实际 Alembic 迁移：**23 passed in 1.89s**。Ruff 通过。
- 覆盖同名不自动合并、批量无效输入事务原子性、明确搜索配置入口、官网只读一页与不访问候选、SearXNG JSON 请求契约、显式绑定/拒绝、JSON-LD、候选持久化数量预算、跨域重定向拒绝、已知适配器复用。
- 来源编辑测试验证账号平台不匹配会拒绝、旧任务冻结快照不变、错误平台域名拒绝、密钥字段拒绝、变更审计存在。
- 匹配测试验证跨公司、不同地点/主体、正文太短均不生成错误候选；过期证据不能合并；后续归属改变不能直接覆盖撤销。
- 特别验证：合并后下一次 `ingest` 仍更新原来源记录并保持 canonical Job，薪资变化、版本和观察正常新增；撤销后最新薪资与版本历史保留。没有修改 `ingest` 实现来迎合测试。
- 真实只读取一次 `https://www.tencent.com/zh-cn/`，发现“加入我们”指向 `https://careers.tencent.com/zh-cn/home.html`；未访问候选页面、未绑定来源。验证在 QA 数据库事务内完成并回滚，没有改变用户正式公司名单。
- 全部使用 Conda `hr-research-dev`；真实 LLM 0 次；没有调用 Docker 或重启主服务。

## 自主决策与限制

- 高相似度招聘内容可能仍对应不同编制，因此候选必须人工确认，不能自动改变岗位归属。撤销过的候选不自动再次提出。
- 已有同名公司只返回选择提示，允许用户通过原有单公司录入另建主体；批量导入不会擅自判断两者是同一家公司。
- 第一版搜索供应商采用标准 SearXNG JSON 接口；可选自建服务使用Bing中国站，不需第三方搜索账号。部署配置缺失时仍不猜测域名或调用付费搜索；搜索排名不等于主体身份已核实。
- 入口发现为有界同步操作，最多读取一个已填官网或一个搜索响应；不创建未请求的定时任务或外发通知。
