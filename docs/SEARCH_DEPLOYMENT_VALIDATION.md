# 独立招聘入口搜索：部署与真实查询验收

日期：2026-10-05，授权节点 `dabing@10.255.200.16`。搜索服务、主 API 和本地 Conda loopback SSH 已接通。**当前线上版本见 [最终 711 部署记录](FINAL_711_DEPLOYMENT.md)**，本轮没有新增搜索请求。下方真实 FastAPI → SearXNG → QA 候选/Audit 验收属于 701 阶段的源 `8f6bf3db…`；旧部署和准备过程保留为历史，不冒充最新源 `15bd8bc…` 的重复执行。

## 701 阶段 API 链路与主栈结果（历史）

主工程完成 701 项回归和前端构建后冻结。后端/适配器源码 SHA 为 `8f6bf3dbd7de297ce86c0b02d6309a73356f93c3f627aadb7755038ef4a5782b`；实际部署镜像内复算与搜索 API 和代码维护两类 E2E 完全一致。

| 项目 | 结果 |
| --- | --- |
| 搜索 API QA run | `0a79bbb7d7494db68b7f3e82ae36f3ba`，新专用 PostgreSQL/Redis/控制进程，迁移 `7c8ba429a6ea` |
| 真实接口 | 通过实际 HTTP 登录、读取部署配置、创建无官网的“金蝶”，`role=other`、notes 含明确 QA 标记；只执行一次 entry-discovery |
| 真实落库 | 5 个 pending 候选、1 个 `recruitment.entries_discovered` Audit；审计 ID `6507dd9d-e7c7-4c0a-a38c-2a7889419a44` |
| 业务边界 | QA SourceBinding=0、ModelCall=0、TaskRun=0；候选站点请求=0；主数据库写入=0 |
| HTTP 验收方式 | 真实搜索传输，没有替换响应；QA 出网防护只准入一次精确搜索请求，其余 HTTP 在请求前拒绝 |
| 同源代码维护 | `a1c51a3efa2a4f0fbe97e40de909dd94` 四场景全部通过，4 次 Mock、6 次内部 HTTP、0 次真实模型调用 |
| Backend 实际镜像 | `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157` |
| Frontend 实际镜像 | `sha256:607170d6b73b29b8b033276686ebd7d8743496fcc188574856f6de079d453b0c` |
| 119 文件构建清单 SHA | `e067748baa4d8cdb5d70fdc66424e476efe57795a24948152458a9b289ecb6c3`，范围包含前端/构建输入，不同于上面的程序源码集合 |
| 实际网络 | 搜索控制网精确为 API+SearXNG 2 个成员；Runner 控制网保持 API+Worker+Broker 3 个成员；两个服务都无宿主端口 |
| 远端实际配置 | GET 返回 `configured=true`、`enabled=true`、`origin=deployment`、`http://recruitment-search:8080/search`；API 健康访问成功，预算 15 秒 |
| 主库保护 | 原 10 家公司、6 个来源绑定的全行 SHA 前后完全一致；远端 ModelCall/已发送通知/活动任务/代码提案均为 0 |
| 页面检查 | 14 个已登录页面和原版维护页只读验证通过，JS 错误=0；运行设置截图明确显示搜索已启用 |
| 其他服务 | 9 个无关容器及 PostgreSQL、Redis、Runner、SearXNG、Valkey 均未重建，合计 14 个容器保持 ID/镜像/StartedAt/网络不变 |

两个 QA 项目已 down，专用数据库卷和证据保留；没有把 QA 公司、候选、岗位或绑定导入主库。本次主栈验收没有再创建公司或发起真实公司搜索，主库保护通过完整摘要确认。

本地由根使用项目 Conda 执行 `ssh_tunnel.py start`，保持仅 `127.0.0.1:18083` 监听；最小写入两项搜索配置并保留 0600 私有 `.env` 备份，刷新 API/Worker/Beat 后，实际 GET 配置为 deployment、启用、loopback 地址完全匹配。本地没有再次搜索公司；原有 ModelCall=2 保持不变、活动任务=0。此处的本地主库与上表远端主库分别统计。

最新证据：`.runtime/search-api-e2e-0a79bbb7/`、`.runtime/code-maintenance-e2e-a1c51a3e/`、`.runtime/refinement-deployment-20261005/`。后者包含 `final.json`、`source-manifest.json`、`runner-network.json`、`search-network.json`、`discovery-config.json`、14 页截图与维护页只读结果。实际设置页截图为 `all-pages/settings.png`，已查看。

远端一致性备份位于主项目 `deploy/backups/refinement-search-701-20261005/`，包含更新前源码、私有配置、数据库和应用文件；没有下载私有备份到本机。运行方式使用 `compose.yaml + compose.code-maintenance.yaml + compose.search.yaml`，后续更新同样须保留三份覆盖关系。

## 实际部署

| 项目 | 值 |
| --- | --- |
| 远端目录 | `/home/dabing/hr-search-verification-20261005` |
| Compose 项目 | `hr-search-verify-20261005` |
| SearXNG 镜像 | `ghcr.io/searxng/searxng@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072` |
| SearXNG 源码 | `d48c4b555421e824342c51d68482dd0898e54d0f`，版本 `2026.10.4-d48c4b555`；镜像 OCI 标签和 `version_frozen.py` 均已核对 |
| Valkey 镜像 | `docker.io/valkey/valkey@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11` |
| 部署/验证文件汇总 SHA | `3246374ce26642fa916090f4552157a642e7ce1f1480558e2ebad6268d6dd03d` |
| 服务入口 | `http://recruitment-search:8080/search`；没有 query，不发布宿主端口 |
| 控制网络 | `hr-intel-verify-20261005_search_control`，internal bridge，owner label 绑定主项目 |
| 活动引擎 | 仅 Bing，固定 `https://cn.bing.com`；`/config` 实测只有一个 enabled engine |

镜像已在授权服务器真实拉取，不是仅引用文档中的标签。私有 `search.env` 在服务器新生成，0600；不复制主栈或开发机的密码、模型 key、浏览器会话。本机没有调用 Docker，Python 语法与 Ruff 检查使用 `hr-research-dev` Conda 环境。

SearXNG 以 UID/GID `977:977` 运行、只读根目录、无 capabilities、无 Docker socket；上限 512 MB、1 CPU、128 PID。Valkey 使用非 root 用户、64 MB 容器上限、32 MB 内存缓存上限，独立于应用 Redis。搜索后观测 RSS：SearXNG 约 88.19 MiB、Valkey 约 3.65 MiB；这是一次低并发观测，不是压力测试容量承诺。

SearXNG 仅接搜索控制网、搜索私有数据网及自己的出网网络；Valkey 仅接搜索数据内网。没有主数据网络、Runner 控制网或主应用文件挂载。独立服务初始部署时，原有 16 个运行容器的 ID、镜像、StartedAt 和网络均未变化，主 API 当时尚未接入；随后主栈接入结果见文首。

## 一次真实公开公司查询

通过固定 DNS 别名调用真实 `/healthz` 后，执行且仅执行一次查询：

```text
GET /search?q="金蝶" 招聘 官网&format=json&language=zh-CN
```

结果为 HTTP 200、耗时 0.302 秒、10 条结果、`unresponsive_engines=[]`。第一条为 `https://www.kingdee.com/job`，标题为“金蝶招聘-为什么选择金蝶-金蝶官网”；第二条为金蝶官网 careers 页面。没有访问这些候选页面，没有新建关注公司或来源绑定，没有调用模型或真实平台登录。

原始响应 SHA-256：`df01abeb5b51c3e391d1568c1fed75239647fb2a2f8eef81e6e61bbfb1c9f168`。

真实响应顶层字段：

```json
["query", "results", "answers", "corrections", "infoboxes", "suggestions", "unresponsive_engines"]
```

当前版本**没有** `number_of_results`，调用方不应把它作为必需字段。结果项包含：

```json
["url", "title", "content", "engine", "template", "parsed_url", "img_src", "thumbnail", "priority", "engines", "positions", "score", "category", "publishedDate", "iframe_src"]
```

平台可保留 URL、标题、摘要、engine/engines、positions、score、category、查询词、实际时间和响应摘要。搜索排名与引擎结果不能证明企业身份，不应自动绑定来源。

## 错误响应与引擎故障边界

额外两个请求不含查询词，在源码的引擎调用前返回，新增上游查询为 0：

| 请求 | 实际响应 |
| --- | --- |
| `/search?format=json` | HTTP 400，`application/json`，`{"error":"No query"}` |
| `/search?format=csv` | HTTP 403，`text/html`；CSV 没有启用 |

固定源码 `webapp.py` 625–642 行在建立 `SearchWithPlugins` 之前校验格式和空查询，因此这两个失败例没有再访问 Bing。

`webutils.get_json_response` 将 `unresponsive_engines` 编码为 `[[engine_name, translated_error_message], ...]`。错误文本可能有 `Suspended: ` 前缀并受 locale 影响，不能直接作为平台稳定 error_code。本次真实成功响应中该字段为空；非空形状来自固定源码核对，未伪造一次真实引擎失败。

建议平台状态：有结果且存在引擎故障为 partial；无结果但存在故障为 engine_unavailable；无结果且无故障才是正常 no_candidates。HTTP 错误、超时、非 JSON、顶层 error、results 类型错误独立记录，不能变成“零招聘”。健康检查只能证明服务存活。

## 实现文件与剩余步骤

- `deploy/search/compose.yaml`、`settings.yml`：固定版本、网络、资源与唯一引擎。
- `init_env.py`：Linux 上生成专属私有配置，已有文件拒绝覆盖。
- `verify_search.py`：默认不执行；执行时只查询一个公开公司，并返回原始 JSON。
- `verify_deployment.py`：不读取容器环境中的秘密，验证镜像、健康、资源、无端口、无 socket、网络成员及其他服务未变；后续 `--check-api` 才核对实际 API 的精确 endpoint 和 15 秒配置。
- 根目录 `compose.search.yaml`：只给 API 增加 `DISCOVERY_INTERNAL_SEARCH_URL`、`DISCOVERY_SEARCH_TIMEOUT_SECONDS` 与专用搜索内网；Worker 不增加权限。该覆盖文件已用于最终主栈。

最终已经在客户端冻结后保留原有两个 Compose 文件，再叠加 `compose.search.yaml` 完成部署。内部搜索使用专属精确 endpoint 客户端，未放开通用 PublicFetcher 私网校验；候选链接继续遵守公共网络边界。

独立阶段本机证据：`.runtime/search-deployment-20261005/verification-search.json`、`verification-service-only.json`；远端同名文件位于上表部署目录。实际主 API 接入结果为远端 `verification-api-connected.json`，同时归档为 `.runtime/refinement-deployment-20261005/search-network.json`；未覆盖独立服务阶段的证据。

## API 验收与本地接入准备（历史）

准备阶段 `deploy/search/verify_api_e2e.py` 先通过 Conda Ruff 和语法检查，当时尚未执行；后续已按文首记录完成。验收使用全新专用数据库和真实 FastAPI HTTP，样例名字为“金蝶”、`role=other`、notes 明确 QA、没有官网；所有正式目标保留在主库，QA 不接主数据网络。

本机 `deploy/search/ssh_tunnel.py` 已实际完成：start → health/status → 再次 start 复用 → `lsof` 验证只监听 `127.0.0.1:18083` → stop → not_running。远端目标来自固定容器当前控制网地址 `192.168.144.2:8080`，不是服务器发布端口；后续地址由脚本动态核验，不能把此 IP 写成永久配置。新增上游搜索、模型调用和本机 Docker 调用均为 0。

准备阶段测试结束时曾停止隧道、没有改 `.env`；这不是当前状态。当前根已启动隧道、最小更新私有配置并刷新本地服务，精确 endpoint 为 `http://127.0.0.1:18083/search`，预算 15 秒，实际配置读取已核对。脚本说明与重连边界见 `deploy/search/README.md`。

## 自主决策记录

采用官方固定镜像和自有 Valkey，避免依赖用户提供第三方搜索账号及共享业务密码。初始只启用真实可用的 Bing 中国站；此前公开连通性检查中百度进入验证码页、DuckDuckGo 直连超时，未尝试绕过。主服务不公开、JSON 由平台后端访问；平台的数据来源和身份校验仍独立保留。
