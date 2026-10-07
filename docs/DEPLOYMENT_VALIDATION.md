# Docker 部署验证证据

## 验证边界

2026-10-05，在用户授权的 `dabing@10.255.200.16`（Ubuntu 24.04 / x86_64）执行。Docker 29.2.1，Compose v5.0.2。独立目录 `~/hr-research-verification-20261005`、Compose 项目 `hr-intel-verify-20261005`，Web 只绑定 `127.0.0.1:18080`。

本机未执行 Docker；本机脚本语法检查使用 `/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python`。同步只允许应用源代码、锁文件和部署目录，没有复制本机 `.env`、数据库、浏览器登录会话或真实模型密钥。远程 `deploy/init_env.py` 生成独立凭据，模型 key 为空且每日预算为 0。

验证前记录已有业务容器 ID/名称/启动时间与端口。所有 Docker 操作限定该 Compose 项目；没有修改已有容器或全局 daemon 配置。

## 已执行命令及当前结果

```sh
# 只在本机检查脚本，不运行 Docker。
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check deploy
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m py_compile deploy/*.py
sh -n scripts/remote_sync.sh
sh scripts/remote_sync.sh dabing@10.255.200.16 hr-research-verification-20261005

# 以下在远程项目目录执行。
python3 deploy/init_env.py --project hr-intel-verify-20261005
docker compose --env-file deploy/server.env config --quiet
docker compose --env-file deploy/server.env build api web
```

静态检查及 Compose 配置检查通过。镜像拉取通过服务器原有 registry mirror 成功，未调用 SOCKS5 代理，也未改变 daemon 网络设置。构建日志保存在远端 `deploy/build.log`。

## 招聘基线：构建与运行通过

验证的是同步时的招聘基线，而不是仍在并行新增的舆情、通知和维护模块。实际构建输入记录在远端 `deploy/verification-results-source-baseline.json`：48 个源文件，SHA-256 为 `68bc19b05c6635046142c90f08b900abb96784fa9e525ffc418ca6d73b5146c9`。本机副本在 `.runtime/deployment-validation-20261005/`。

运行版本：Python 3.12.15、Playwright 1.63.0、完整 Chromium 153.0.8010.12、Celery 5.6.3。数据库迁移为 `c14c1d73eb99 (head)`，`alembic check` 输出 `No new upgrade operations detected.`。

| 验证项 | 实际结果 |
| --- | --- |
| 后端与前端镜像构建 | 均成功，依赖及浏览器已安装；首次构建主要耗时为大陆网络下载与镜像导出 |
| PostgreSQL、Redis、API、Worker、Web | Compose 健康检查均通过；Beat 正常运行；迁移容器退出码 0 |
| 前端入口与 SPA 深链接 | `/`、`/companies` 均正常返回 |
| 部署后的 React 实际渲染 | 容器 Chromium 通过 Nginx 加载登录页，密码输入框与登录按钮可见，JavaScript 异常 0；截图已保存 |
| 管理员登录、会话撤销 | 登录后可访问 API，注销后恢复 401 |
| Origin 校验 | 非允许来源的写请求返回 403 |
| 内部路由隔离 | `/api/internal`、子路径及编码变体经 Web 均返回 404 |
| Worker 到 API 内部网络及鉴权 | 无 token 返回 403，正确 token 到达业务检查并对不存在的 ID 返回 404 |
| Beat → Worker → 数据库任务状态 | BOSS 无账号任务从排队进入 `waiting_login`，没有发起外部招聘采集 |
| 容器浏览器 | 非 root UID 10001，以完整 `chromium` 通道完成 frame/click/type/save，本地测试页 PNG 6326 字节 |
| 会话加密 | 文件权限 0600，磁文不包含测试 cookie 明文，解密后内容一致 |
| 停止并重建容器后的持久化 | `down`（无 `-v`）再 `up` 后，原公司 ID、Redis fixture 值、加密会话文件 SHA-256 与解密内容全部一致 |
| 模型成本 | key 未配置、日预算 0、调用记录 0 |
| 端口范围 | `ss` 仅显示 Web 监听 `127.0.0.1:18080`，没有发布 PostgreSQL/Redis/API/CDP 端口 |
| 原有容器隔离 | 验证前后 9 个既有容器的 ID、名称、启动时间完全一致 |

本轮真实发现并修复一个部署问题：只读容器根目录下 Chromium crashpad 无法建立数据库，报 `chrome_crashpad_handler: --database is required`。在 Compose 中将 `XDG_CONFIG_HOME`、`XDG_CACHE_HOME` 分别设为 `/tmp/browser-config`、`/tmp/browser-cache` 后通过浏览器交互验证。保留根文件系统只读和非 root 运行，不扩大容器权限。

实际运行命令（均位于远程独立项目）：

```sh
docker compose --env-file deploy/server.env up -d --wait --wait-timeout 120
python3 deploy/verify_stack.py
docker compose --env-file deploy/server.env exec -T api python - < deploy/verify_browser.py
docker compose --env-file deploy/server.env exec -T api python - < deploy/verify_frontend.py
docker compose --env-file deploy/server.env exec -T worker python - < deploy/verify_internal.py
docker compose --env-file deploy/server.env exec -T api python -m alembic -c /app/backend/alembic.ini check
docker compose --env-file deploy/server.env exec -T api python -m alembic -c /app/backend/alembic.ini current
docker compose --env-file deploy/server.env exec -T api python - record < deploy/verify_persistence.py
docker compose --env-file deploy/server.env down
docker compose --env-file deploy/server.env up -d --wait --wait-timeout 120
docker compose --env-file deploy/server.env exec -T api python - check < deploy/verify_persistence.py
```

已验证镜像 ID：

| 镜像 | SHA-256 |
| --- | --- |
| hr-intelligence-backend:local | `03be97795a80dd928132e71a2dc2bde864537dd46087c9f3a1fd8058b362ba8c` |
| hr-intelligence-web:local | `96f2dac3af4424eedc2d6756d753569d89a8942ec7d221032214b44c54637419` |
| postgres:16-alpine | `721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea` |
| redis:8-alpine | `3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0` |
| nginx:1.28-alpine | `a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236` |

## 招聘基线验证时的范围

- 未复制或使用真实第三方账号，未实际登录 BOSS/猎聘/舆情平台；浏览器验证使用容器内固定 fixture。
- 没有调用用户提供的 LLM 网关，也未通过远程测试向外部收件人发送消息。
- 上述招聘基线记录只覆盖当时的源文件指纹与迁移版本。后续新增模块已单独同步、构建、升级迁移并验收，见下一节；不能沿用基线旧镜像的通过结果宣称整个 V1 完成。
- 远程验证服务保留运行，仅 loopback 可达；独立凭据保存在远程私有 `deploy/server.env`，未出现在本文件或测试输出中。

## 完整业务增量部署（2026-10-05）

主工程冻结后，采用同一允许目录同步脚本更新远端；本机 `.env`、模型密钥和登录会话仍未复制。模型 key 为空、每日预算为 0。备份仅保存在远端私有 `deploy/backups/`：升级前的 PostgreSQL custom dump 与应用文件卷副本。之后停止本项目 API/Web/Worker/Beat、升级迁移并重新创建对应容器；没有停止共享主机的其他业务服务。

后端实际运行镜像为 `sha256:94f128394c109e5f4a5790a12dc4b127d5742642bf96dcacd0dca7c8568a723b`，API/Worker 的容器 `.Image` 与该值一致。运行用户 `10001:10001`，根文件系统保持只读。迁移已升至 `b82644ab8bac (head)`，`alembic check` 再次通过。

该轮构建输入为 85 个源文件，指纹 `40c5a65e923ac3747c4651e73690e3ffd7eb9257d788ca923d0696e771c3cd58`。本地同一允许文件集独立计算得到相同指纹，远端记录为 `deploy/verification-results-source-final.json`。前端随后进行 React 19 / AntD 5 兼容修正，修正后的 Web 镜像与最终整体指纹另列下方，不能将本段的旧前端镜像当作最终版本。

完整模块验收使用 `deploy/verify_business.py`，结果保存在 `deploy/verification-results-business.json`，并已复制到本机 `.runtime/deployment-validation-20261005/`：

| 验收内容 | 实际结果 |
| --- | --- |
| 新模块未登录访问 | 舆情、资讯、通知、维护、助手和用户接口均返回 401 |
| 已登录接口 | 所有上述接口可读取，BettaFish 如实显示 `not_configured` |
| 招聘统计和对话查询 | 无数据时岗位数量保持未知，对话查询不发起采集或模型调用 |
| 跨公司比较任务 | Analysis 队列执行并生成无模型比较报告；覆盖不足时状态为 `partial`，不伪造排名 |
| 舆情资料 | 人工导入 fixture 成功，原文记录可读、收藏与备注可保存 |
| 舆情采集任务 | 绑定未登录小红书 fixture 账号后进入 `waiting_login`，不访问真实平台 |
| BettaFish 任务 | `blocked_configuration / bettafish_not_configured`，没有安装或调用未知上游 |
| 资讯任务 | News 队列执行，来源错误为 `restricted_address`，在连接前拒绝 loopback fixture |
| 通知任务 | Notifications 队列消费 outbox，禁止目的地址返回 `failed / restricted_address`，未外发消息；之后停用 fixture 通道 |
| 维护任务 | Maintenance 队列对未登记上游的猎聘适配器返回 `not_configured`，不会搜索或执行未知仓库 |
| 普通账号权限 | 可读舆情/资讯，不能管理用户、维护或新建通知通道；停用该临时账号后旧会话返回 401 |
| 升级持久性 | 原 PostgreSQL fixture 公司、Redis 标记和加密会话文件/解密内容仍一致 |
| 完整 Worker 队列 | 实测监听 recruitment、analysis、sentiment、news、notifications、maintenance、celery |
| 隔离与成本 | 原 9 个容器的 ID/启动时间未变化；端口仍为 loopback；模型调用、真实通知、真实平台登录均为 0 |

首轮业务脚本把“无数据比较”误设为 succeeded，实际系统正确返回 partial。检查报告生成逻辑后修正测试预期，通过 `--resume` 继续检查同一组已创建任务，没有重新创建任务或触发额外分析。已有任务 ID 保存在 `verification-results-business.json.pending.json`，最终 comparison 为 `1dd2cd90-3652-4753-9c00-30b846de2092`。

已登录浏览器检查覆盖 14 个实际页面：总览、公司、岗位、来源、招聘趋势、舆情、资讯、计划、任务、报告、助手、通知、维护、运行设置。第一轮 Chromium 渲染的 JavaScript 异常和 API 错误均为 0，全部外部页面资源请求被阻止。截图是明确的部署验证样本，不代表用户真实竞品或真实舆情。前端兼容包更新后会再次检查这些页面并保留最终截图。

### 最终前端兼容修正与版本核对

主工程加入固定版本 `@ant-design/v5-patch-for-react-19@1.0.3` 并在入口导入，同时修复截图发现的最近任务名称空白及类型统一显示为单公司的问题。只同步前端及浏览器验收脚本，执行 `docker compose ... build web` 和 `docker compose ... up -d --no-deps --wait web`。没有重建后端镜像、重复数据库迁移或再次创建业务测试任务。

最终运行版本：

| 项目 | 证据 |
| --- | --- |
| API / Worker 镜像 | `sha256:94f128394c109e5f4a5790a12dc4b127d5742642bf96dcacd0dca7c8568a723b`，容器实际 `.Image` 与前一轮相同 |
| Web 镜像 | `sha256:3b9c57fa192e90b42ab4356bff0105807b031761641fb2e353262c69084e5c5a`，容器实际 `.Image` 已核对 |
| 最终 85 文件指纹 | `b1297d722ceffaec7c1084ad6a8d90d527fc3620929e90ac37fff1a2ef20ee34`，本地与远端独立计算一致 |
| 数据库迁移 | `b82644ab8bac`，此次前端修正没有数据库变化 |
| 14 页再次验收 | 全部成功；JavaScript 异常 0、API 错误 0、AntD React 兼容警告 0；最近任务标题非空且不再一概显示单公司类型 |
| 共享主机隔离 | 最终再次比较，9 个原有容器的 ID 和启动时间仍完全相同 |

最终截图与机器可读结果保存在本机：

- `.runtime/deployment-validation-20261005/final-pages/overview.png`：1440px 桌面宽度，已登录总览，全部为部署验证样本。
- `.runtime/deployment-validation-20261005/final-pages/sentiment.png`：人工导入的舆情 fixture 与模块页面。
- `.runtime/deployment-validation-20261005/final-pages/result.json`：14 页标题、零异常与零兼容警告记录。
- `.runtime/deployment-validation-20261005/verification-results-business.json`：完整业务队列、权限及成本结果。
- `.runtime/deployment-validation-20261005/verification-results-source-final.json`：最终源文件 SHA-256 清单。

远端继续保留在原独立目录和 Compose 项目中，只发布 `127.0.0.1:18080`。Docker 测试期间没有使用用户真实模型密钥，真实模型请求、真实通知外发和真实第三方登录均为 0。BettaFish 上游仍未安装且 Bridge 明确未配置，真实账号、真实新闻/舆情来源及收件通道需按实际使用配置；本记录没有把这些配置前提伪装成已经验证的生产连接。

## 检查点与定时摘要主栈更新：中间状态，尚未最终验收

2026-10-05 再次按主工程冻结通知准备增量更新。此次允许清单覆盖 96 个构建输入，本地和远端均得到指纹 `ff689c08eca0787f42850451ac5f3b479ec190262ae8e9ac74d8bd502d48738d`；运行记录为远端 `deploy/verification-results-source-frozen.json`。同步后检查远端仍不存在开发机 `.env`、`.runtime`、`.git`，没有复制真实密钥或登录资料。

增量构建成功，备份保存在远端私有 `deploy/backups/frozen-9d924/`。备份/迁移/启动命令已开始后，主工程 QA 发现 Playwright response EventGreenlet 的上下文继承问题并要求暂停最终发布。为避免中途打断迁移，该已启动命令完整收尾；没有再启动新一轮同步或业务验收。当前镜像是明确的中间版本，不能以健康检查通过代替该问题修复后的完整验收。

已完成的只读核对：迁移到 `9d924bc617c7`，API/Worker/Web/PostgreSQL/Redis 健康，Beat 正常运行，迁移容器退出码 0。容器内实际配置确认如下，未输出其他环境变量秘密：

| 配置 | 实际值 |
| --- | --- |
| checkpoint_max_age_seconds | 3600 |
| checkpoint_max_raw_bytes | 30000000 |
| checkpoint_max_steps | 600 |
| checkpoint_max_records | 500 |
| llm_daily_request_limit | 0 |
| bettafish_license_confirmed | false |

模型 key 为空；主栈仍只绑定远端 `127.0.0.1:18080`。本轮构建前再次记录 9 个无关容器的 ID/名称/启动时间，待最终更新后比较。尚未对本中间版本发起真实浏览器平台采集、模型调用或外发消息，等待修复后的冻结通知再统一增量验证。

## 检查点与定时摘要主栈：修复后最终验收通过

2026-10-05，主工程修复 Playwright response EventGreenlet 的上下文继承问题后重新冻结，完成允许目录同步、后端构建和主栈更新。随后设置页改为读取真实 BettaFish Bridge 状态，长 JD 页面修复横向溢出，两次变更均只重建 Web。上一节的 `ff689c08…` 是中间版本，不作为最终验收依据；本节取代前面各轮的“最终版本”结论，历史证据仍保留。

### 最终构建与运行身份

| 项目 | 最终证据 |
| --- | --- |
| 构建输入 | 96 个文件，本机 Conda Python 与远端独立计算一致 |
| 源码 SHA-256 | `92cc91d868ea5341fcd7005242676c75e71a980bf8fde2b64ccb69368c70f633` |
| API / Worker / Beat 实际镜像 | `sha256:c1e39759b7ca0e60b6f7b8e25e5b2bd76953d0bfa2a3fcbcaedf181c516e8650` |
| Web 实际镜像 | `sha256:a7da0ef50f37741ab030d73ea7314ac8d33b02b54421c02ec23e5c909d5876e2` |
| 数据库迁移 | `9d924bc617c7 (head)`；`alembic check` 为 `No new upgrade operations detected.` |
| 运行权限 | API / Worker / Beat 为 `10001:10001`；Web 为 `65534:65534`；根文件系统只读 |
| 入口与隔离 | 仅发布 `127.0.0.1:18080`；9 个既有业务容器的 ID、名称、启动时间与本轮构建前完全一致 |

主栈 API、Worker、Web、PostgreSQL、Redis 健康，Beat 正常。实际 Worker 队列为 `recruitment,analysis,sentiment,news,notifications,maintenance,celery`。检查点限制仍为 1 小时、30,000,000 原文 bytes、600 步、500 条记录。`LLM_DAILY_REQUEST_LIMIT=0`、模型 key 为空、`BETTAFISH_LICENSE_CONFIRMED=false`。镜像依据容器实际 `.Image` 核对，没有仅凭可变 tag 判断版本。

### 服务与业务验收

在 EventGreenlet 修复后的后端镜像上执行 `verify_stack.py`、`verify_business.py`、`verify_internal.py`。末次仅 CSS 变化后，没有重复创建这些业务测试任务。

| 验证项 | 结果 |
| --- | --- |
| 入口与身份 | 登录、注销、Origin 校验通过；内部路由经 Web 返回 404 |
| 调度与队列 | Beat → Worker → 数据库状态通过；BOSS 无账号任务进入 `waiting_login`，不实际登录平台 |
| 模块鉴权与业务 | 招聘比较在无覆盖时如实为 `partial`；舆情导入、收藏与备注通过；未登录舆情任务等待登录；Bridge 未配置为 `blocked_configuration` |
| 资讯、通知与维护 | 禁止访问的私网目的在连接前拒绝；未登记上游的维护任务为 `not_configured`；普通用户权限和撤销通过 |
| 内部 token | 无 token 返回 403，正确 token 对不存在目标返回 404 |
| 实际任务代次门禁 | 使用隔离 QA 任务：当前 attempt=2、请求 expected_attempt=1 返回 `409 stale_execution`；expected_attempt=2 返回 `409 waiting_login`；缺少 expected_attempt 返回 422 |
| 成本与外发 | 真实 LLM 请求、真实通知外发、真实第三方平台登录均为 0 |

代次验证任务已经取消，其临时公司已归档。没有把 `waiting_login` 或 `blocked_configuration` 当作真实平台采集成功。主栈没有部署可选 BettaFish 实例；其独立上游 Docker 验收见 `BETTAFISH_DOCKER_VALIDATION.md`，与本节主栈隔离验证分别记录。

### 最终浏览器与边界显示

设置页更新后完成 14 个实际页面的 Chromium 验收：总览、公司、岗位、来源、招聘趋势、舆情、资讯、计划、任务、报告、助手、通知、维护、运行设置。JavaScript 异常 0、API 错误 0、AntD 兼容警告 0。页面资源仅允许容器内 Web，请求外部资源被阻止。

随后使用明确标注为 QA 的合成样本补边界截图：公司名称 146 字、岗位标题 189 字、JD 4659 字（包含连续长英文 token）、12 个城市，薪资和来源发布/更新时间均缺失。首次真实截图发现 JD 抽屉横向溢出，主工程为 `.job-description` 增加 `overflow-wrap:anywhere` 并重建 Web。最终定向浏览器验证结果如下：

| 元素 | 修复前 clientWidth / scrollWidth | 修复后 clientWidth / scrollWidth |
| --- | --- | --- |
| 岗位列表页面 | 1440 / 1440 | 1440 / 1440 |
| 岗位详情抽屉 | 760 / 4360 | 760 / 760 |
| JD 段落 | 712 / 4336 | 712 / 712 |

长公司名、长岗位名和全部城市可见；缺薪资显示“未披露”，缺来源日期不会渲染 `Invalid Date`。保留修复前后截图，没有将失败截图冒充最终结果。

根视觉复核进一步发现首张 `detail-top-fixed.png` 截取了 AntD 抽屉入场动画的中间帧；`clientWidth/scrollWidth` 相等本身不能证明元素已经进入视口。因此仅重取同一既有 QA 岗位的顶部/底部截图，没有重新采集或创建业务任务。重取前以 100 ms 间隔确认连续 5 次可见边界稳定，CSS `transform=none`，并在截图后复核坐标不变：1440 px 视口中抽屉 `left=680`、`right=1440`、`width=760`；正文可见边界同样为 680–1440，顶部 `scrollTop=0`、底部 `scrollTop=2007`。两张最终截图已覆盖并视觉复核，未出现右侧裁切。这是截图动画采样问题，未修改产品代码。完整坐标记录在 `result-fixed.json` 的 `drawer_visible_bounds`。

另完成两个新增界面定向检查：

- 日报：待汇总页显示北京时间事件窗口与 09:30 计划时间；新增规则选择“每日定时摘要”后，发送时间默认 `09:00`，显示北京时间及前一天窗口说明。验证过程中一次 AntD 原始 input 点击被选择项遮挡，改用键盘展开选择框完成操作，未修改产品行为、未创建新规则。
- 检查点：用容器内临时 HTTP fixture 实际读取 `/jobs`、`/job/1`，得到 2 个已保存页面、1 个岗位与 partial 结果；任务抽屉显示“已保留 2 个页面证据”及可接续期限。fixture 来源随后停用，临时 HTTP 服务已关闭，没有访问外部招聘网站。

日报展示 fixture 的规则和通道从创建时即关闭，仅人工写入一条用于展示的 pending 项；截图后通过规则停用接口将其取消，数据库复核为 1 条 `cancelled`、规则关闭、通道关闭、该通道 outbox 为 0。最终定向检查 JavaScript / API 异常均为 0。上述合成样本是测试数据，不代表用户真实竞品、真实业务投诉或已经发送的日报。

### 最终证据位置与部署约束

本机证据目录：`.runtime/deployment-validation-20261005/`。

- `verification-results-source-final-core.json` 与 `verification-results-source-local-final-core.json`：远端/本机最终 96 文件清单。
- `verification-results-final-core-metadata.json`：实际容器镜像、绑定端口、无关容器隔离核对。
- `verification-results-stack-postgreenlet.json`、`verification-results-business-postgreenlet.json`：修复后的服务/业务验收。
- `verification-results-deployment-final-fixture.json`：代次门禁与岗位边界样本标识。
- `verification-results-deployment-new-ui-fixture.json`：检查点与日报 UI 样本标识。
- `verification-results-final-core-pages/result.json`：14 页验收结果及同目录页面截图。
- `verification-results-final-core-boundary/result-fixed.json`：修复后的宽度量测、日报/检查点结果、QA pending 收尾状态。
- `verification-results-final-core-boundary/list-fixed.png`、`detail-top-fixed.png`、`detail-bottom-fixed.png`：长字段最终页面。
- `verification-results-final-core-boundary/digest-pending.png`、`digest-rule-form.png`、`checkpoint-detail.png`：新增界面截图。

本轮只在已授权的远端独立 Compose 项目调用 Docker，本机未运行或安装 Docker，本机 Python 校验使用独立 `hr-research-dev` Conda 环境。远端确认没有开发机 `.env`、`.runtime`、`.git`，私有配置仍只留在远端。没有重启或重建其他 9 个业务容器，也未重复已通过的业务、模型或真实采集测试。部署服务继续以 loopback 入口保留运行。
