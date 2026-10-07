# 观势 · 企业外部情报平台

当前使用远端实例：<http://10.255.200.16:18080>，管理员`admin`，初始密码在私人对话及服务器私有配置中。用户已要求仅远端运行；下文的本地启动步骤是历史开发说明，不应自动执行。详见[远端访问](deploy/REMOTE_ACCESS.md)。历史`.runtime`证据已归档到服务器本项目`retired-local-20261005/`私有目录。

面向公司内部的招聘情报、整体舆情与 HR 资讯平台。当前处于开发阶段，完整范围与验收标准见 [PLAN.md](PLAN.md)；尚未完成整个 V1。

## 当前可运行能力

最新765项全量及本机/授权服务器部署通过。资讯结构化订阅、政策日期提醒和四类数据保留已补齐；默认有限期限和7天宽限，实际使用前可在运行设置查看/调整。见[本轮验收](docs/LIFECYCLE_VALIDATION.md)。

- 内部登录、服务端可撤销会话。
- 添加关注公司、绑定招聘来源、建立手动/定时计划；页面可编辑城市、岗位词、排除词、来源与采集预算。
- 多公司批次拆成独立公司任务，使用 PostgreSQL 持久化、Celery/Redis 执行。
- 公开 JSON-LD 岗位读取、腾讯官网专用列表/详情适配；支持有界检查点接续和从头采集。
- 岗位入库、版本记录、基线、字段变化、完整性保护与疑似下架。
- 工作台、公司、来源、任务、岗位、计划与运行设置页面。
- Chat Completions 网关：非流式/流式解析、每日及单任务预算、用量记录、超时不自动重试。
- 单公司 LangGraph 分析与有岗位引用的简报，已完成一次成功的 glm-5.3 真实链路验证。
- 官网动态页面和受限选择器规则学习/复用/改版适配；批量名录、官网入口发现与可撤销跨来源岗位合并。
- 独立人工登录窗口与加密会话；BOSS、猎聘、智联、前程无忧采集适配（各平台实测边界见下方验证资料）。
- 招聘日序列、冻结统计快照的跨公司比较；限定中文指令与可执行动作草稿。
- 整体舆情公开资料采集/导入、来源覆盖、分类线索、收藏备注、报告和 BettaFish 隔离桥；任务可选择来源与北京时间窗口，后续搜索摘录不覆盖已有详情，观察原文和主正文时间分别保留。
- HR 资讯 Feed/网页采集、原文摘录、多来源去重、政策日期/歧义、行业筛选与收藏；时间预算结束可保留已读部分，取消/旧执行继续拒写。
- 邮件、Webhook、飞书通知规则、每日摘要、加密通道、事务 outbox、投递记录和未知结果保护。
- 适配器登记、固定上游检查、受预算的JSON规则及代码/依赖候选；独立执行、可信验证、唯一版本发布、一次恢复和旧依赖回滚。
- 可选内置 SearXNG 公司名称搜索，Bing中国站生成有出处的待核对候选；失败和部分引擎失败分别显示，不自动访问或绑定候选。
- 内部用户权限管理、监测计划编辑与管理操作审计。

这些模块已有代码与测试，不代表每个外部平台的真实账号采集均已验收。BOSS/猎聘等登录后链路与真实通知目标仍需相应账号/配置；网络受限来源不会显示为零内容。BettaFish固定版本已在独立环境和可选容器执行真实ReportAgent流水线，验证使用合成模型响应，实际许可仍关闭，真实模型报告质量尚未验收。当前验证范围与剩余工作见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。

## 本地环境

本机开发不依赖 Docker。Python 使用项目专用 Conda 环境：

~~~bash
conda create -n hr-research-dev -c conda-forge python=3.11 pip -y
conda create -n hr-research-services -c conda-forge postgresql=16 redis-server=8 -y
conda run -n hr-research-dev python -m pip install -r backend/requirements.lock
conda run -n hr-research-dev python -m pip install --no-deps -e ./backend
PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/browsers" conda run -n hr-research-dev python -m playwright install --no-shell chromium
~~~

前端在 frontend 目录执行 npm ci。不要复用系统 Python 或 base 环境安装项目依赖，也不需要运行 Docker。

先按照 .env.example 配置私有 .env。应用数据库和测试数据库应使用不同数据库，测试库名以 _test 结尾；首次管理员密码至少 12 位。

当前开发实例的配置已经保存在被 Git 忽略、权限为 0600 的 .env 中。管理员访问资料保存在 .runtime/local-access.txt；不要提交这些文件。

原生 PostgreSQL/Redis 服务默认使用本机 55432/56379 端口及 .runtime 下的专用数据目录：

~~~bash
conda run -n hr-research-dev python scripts/local_services.py start
conda run -n hr-research-dev python -m alembic -c backend/alembic.ini upgrade head
~~~

分别在终端运行：

~~~bash
conda run --no-capture-output -n hr-research-dev python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
conda run --no-capture-output -n hr-research-dev python -m celery -A app.queue:celery worker --pool=threads --concurrency=3 -Q recruitment,analysis,sentiment,news,notifications,maintenance,celery --hostname 'hr-research@%h' --loglevel=WARNING
conda run --no-capture-output -n hr-research-dev python -m celery -A app.queue:celery beat --schedule .runtime/celerybeat-schedule --loglevel=WARNING
~~~

前端：

~~~bash
cd frontend
npm run dev
~~~

访问 http://127.0.0.1:5173 。API 文档在 http://127.0.0.1:8000/docs 。服务默认仅绑定本机。

如本机采用 TUN Fake-IP DNS，可在私有配置中启用 PUBLIC_DNS_FALLBACK=true。该模式只处理 198.18.0.0/15 的合成解析，随后验证公网 DNS 并固定公网连接 IP，保留原域名的 TLS 校验；不允许绕过检查访问私有网段。部署环境默认关闭。

### 可选名称搜索与本机连接

已授权服务器上的搜索服务独立部署，固定SearXNG/Valkey版本，不发布宿主端口；具体配置见 [搜索服务验证](docs/SEARCH_DEPLOYMENT_VALIDATION.md)。Docker主栈通过 `compose.search.yaml` 给API配置一个固定内部接口，普通网页采集的私网限制不变。

当前Conda开发实例通过本机loopback SSH转发连接同一服务，私有.env已配置 `DISCOVERY_INTERNAL_SEARCH_URL=http://127.0.0.1:18083/search`。重启电脑或远端搜索容器重建后，先执行：

```sh
conda run -n hr-research-dev python deploy/search/ssh_tunnel.py start
conda run -n hr-research-dev python deploy/search/ssh_tunnel.py status
# 不再需要时可执行同一脚本 stop。
```

脚本只调用SSH，不调用本机Docker；每次重新核对授权节点的服务身份和当前内网IP。没有搜索服务或不希望启用时可清空该环境值，并在运行设置中保存关闭状态。显式保存的搜索配置优先于部署默认配置。只有精确匹配部署配置的搜索接口获得私网访问权限，不能借此读取其他内部网址。

## 测试与成本

~~~bash
conda run -n hr-research-dev python -m pytest backend/tests -q
conda run -n hr-research-dev python -m ruff check backend scripts
conda run -n hr-research-dev python -m alembic -c backend/alembic.ini check
~~~

前端在 frontend 目录执行 npm run build。

自动化测试使用独立 PostgreSQL 测试库并会重建该库的应用表，测试库必须以 `_test` 结尾；并行测试使用不同库名。模型、签名、加密测试使用临时密钥，测试模式禁止未经模拟的真实模型/通知网络调用。当前最终全量765项及源码范围见 [本轮验证](docs/LIFECYCLE_VALIDATION.md)；[早期集成记录](docs/INTEGRATION_VALIDATION.md) 保留历史，不能用旧版测试数量代表新代码。

不要为了测试而批量运行模型或抓取网站。仅在实际接入需要时使用有界验证。现有官网验证脚本限制一页列表、最多两个岗位详情，并将公司明确标记为开发验证样本：

~~~bash
conda run --no-capture-output -n hr-research-dev python scripts/smoke_recruitment.py --live
~~~

该脚本不调用 LLM，结果保存在 .runtime/recruitment-smoke.json。不要把该开发样本当成用户指定的竞品。

模型默认 glm-5.3，备选 deepseek-flash；不自动切换、不自动重试。GLM-5.3 使用 low 推理强度（该模型不能关闭思考）；输出预算包含推理消耗。每日请求、单任务调用、输入长度、最大输出均有上限。提供商未返回用量时保持未知，不伪造费用。

真实公司简报验证脚本为 scripts/smoke_analysis.py，只有显式 --live 时才执行，已有调用时默认拒绝重复；不要在普通测试或持续集成中调用它。

一次真实代码修复实验已消耗唯一请求预算，返回在1200输出tokens处截断，网关拒绝了该结果；没有执行或发布候选，也没有重试。该能力的真实模型质量尚未验证成功，不能用Mock回归代替，详见 [单次代码修复验收](docs/LIVE_CODE_REPAIR_VALIDATION.md)。此实验的ledger保留，不能通过删除文件或更换run_id重新调用。

## 项目资料

- [实施计划](PLAN.md)
- [后续开发执行约定](AGENTS.md)
- [自主决策及审计记录](docs/DECISIONS.md)
- [当前实现与后续工作](docs/IMPLEMENTATION.md)

Docker 部署文件已提供，711回归对应的提示修复及舆情范围页面已在授权SSH节点完成独立Compose部署；最新源码、镜像、健康和页面证据见 [最终部署记录](docs/REFINEMENT_VALIDATION.md)。BettaFish另有可选镜像；真实外部账号、报告质量和通知目标单独验收。本机没有 Docker，不在本机调用、安装或启动 Docker。

当前授权部署使用基础栈、独立Runner控制网及搜索控制网三个文件。搜索服务和网络先按其文档准备；在**部署服务器**操作主栈时持续带上三个文件，避免重建丢失网络：

```sh
docker compose --env-file deploy/server.env -f compose.yaml \
  -f compose.code-maintenance.yaml -f compose.search.yaml up -d --wait
```

仅部署基础能力时可不启用可选覆盖文件；不要把服务器命令放到没有Docker的本机开发流程。

- [远程部署说明](docs/DEPLOYMENT.md)
- [Docker 部署验证](docs/DEPLOYMENT_VALIDATION.md)
- [招聘采集验证](docs/RECRUITMENT_QA.md)
- [连续两轮真实官网观察](docs/RECRUITMENT_REAL_OBSERVATION.md)
- [公司发现与岗位匹配](docs/RECRUITMENT_MANAGEMENT.md)
- [舆情验证](docs/SENTIMENT_VALIDATION.md)
- [HR 资讯验证](docs/NEWS_VALIDATION.md)
- [趋势与对话验证](docs/ANALYTICS_ASSISTANT_VALIDATION.md)
- [通知验证](docs/NOTIFICATION_VALIDATION.md)
- [每日摘要](docs/DIGEST_VALIDATION.md)
- [官网检查点与真实观察时间](docs/CHECKPOINT_VALIDATION.md)
- [真实队列与并发隔离](docs/RUNTIME_CONCURRENCY_VALIDATION.md)
- [自动维护闭环](docs/AUTO_MAINTENANCE_VALIDATION.md)
- [舆情与资讯最新采集证据](docs/CONTENT_COLLECTION_VALIDATION.md)
- [BettaFish真实上游流水线](docs/BETTAFISH_UPSTREAM_VALIDATION.md)
- [BettaFish可选容器](docs/BETTAFISH_DOCKER_VALIDATION.md)
- [逐项需求审计](docs/REQUIREMENTS_AUDIT.md)
- [本轮需求补齐与集成验收](docs/REFINEMENT_VALIDATION.md)
- [代码维护真实E2E](docs/CODE_MAINTENANCE_E2E.md)
- [Runner与当前主栈部署](docs/CODE_RUNNER_DEPLOYMENT.md)
- [独立名称搜索](docs/SEARCH_DEPLOYMENT_VALIDATION.md)
