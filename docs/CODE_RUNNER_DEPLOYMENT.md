# 主栈与独立 Runner 控制网络部署

日期：2026-10-05。**当前线上版本以 [最终 711 部署记录](FINAL_711_DEPLOYMENT.md) 为准**：程序源 `15bd8bc…`，API/Worker 镜像 `b9cfbdf4…`，Web 镜像 `1390d3b5…`。本机没有调用 Docker。下方 701 的双网/E2E 部署记录和更早准备过程均保留为历史；最新增量部署没有重跑这些场景，也没有把真实模型失败写成质量通过。

## 701 版本与双网部署（历史）

最新后端/适配器源码 SHA 为 `8f6bf3dbd7de297ce86c0b02d6309a73356f93c3f627aadb7755038ef4a5782b`。在新独立 QA 项目中先通过真实搜索 API → 5 候选及 Audit 落库，再在同源上通过代码维护四场景。证据分别为 `.runtime/search-api-e2e-0a79bbb7/` 和 `.runtime/code-maintenance-e2e-a1c51a3e/`。

| 最新部署项 | 实际结果 |
| --- | --- |
| Backend（API/Worker） | `sha256:d3b7a726b026364affcec83dfbe84c2592d92689852e97e73d255a3cdb465157` |
| Frontend | `sha256:607170d6b73b29b8b033276686ebd7d8743496fcc188574856f6de079d453b0c` |
| 119 文件构建输入 SHA | `e067748baa4d8cdb5d70fdc66424e476efe57795a24948152458a9b289ecb6c3` |
| 搜索覆盖文件 SHA | `fcb8250d71c4aedc81328d186d0a519c25cfec6d56e0991670288c0363252db7` |
| 镜像内程序源码 | 实际复算与两类 E2E 的 `8f6bf3db…` 完整 SHA 一致 |
| 数据库 | 保持 `7c8ba429a6ea`，Alembic check 无漂移 |
| Runner 控制网 | 精确 API/Worker/Broker 3 成员，真实第一方 health ready、匿名 401、应用无 socket、Broker 无宿主端口 |
| Search 控制网 | 精确 API/SearXNG 2 成员，API 精确 endpoint 和 15 秒预算生效，实际 health 200；Worker 没有搜索连接 |
| 主库业务目标 | 10 家公司、6 个来源绑定的全行摘要更新前后完全相同，没有新建或覆盖目标 |
| 远端主库状态 | ModelCall=0、已发送通知=0、活动任务=0、代码提案=0 |
| 页面与内部通信 | 14 个登录页面通过，原版维护只读脚本通过，JS 错误=0，Worker→API Token 校验通过 |
| 保持不变的容器 | 9 个无关业务容器，加 PG/Redis/Runner/SearXNG/Valkey，合计 14 个 ID/镜像/StartedAt/网络未变 |

最新备份目录为 `deploy/backups/refinement-search-701-20261005/`，只在远端私有保存；包括旧源码/私有配置、177217 字节数据库 dump、3911155 字节应用文件归档。新镜像构建完成后才停止本项目调度与应用服务，执行一致性备份、迁移和启动；没有重建搜索、Runner、数据库或其他业务。

本次没有重跑会向主库添加公司的 `verify_stack.py`。实际设置 GET 显示搜索 `configured=true/enabled=true/origin=deployment`，只读页面显示内部 endpoint；真实公司查询仅在独立 QA 中执行一次。主库目标完整摘要保护与两类真实 E2E 一同构成验收范围。

最新归档为 `.runtime/refinement-deployment-20261005/`：`final.json`、`inputs.json`、`source-manifest.json`、`runner-network.json`、`search-network.json`、`discovery-config.json`、`internal-auth.json`、`all-pages/` 和 `live-read-only/`。设置页截图 `all-pages/settings.png` 已查看。完整前端截图最初位于容器 tmpfs，Docker cp 无法读取时改为容器内 tar 导出；没有为导出问题重跑页面或更改生产代码。

当前维护命令必须合并三份配置，避免后续 recreate 丢失搜索连接：

```sh
docker compose --env-file deploy/server.env \
  -f compose.yaml -f compose.code-maintenance.yaml -f compose.search.yaml up -d --wait
```

API 保留 app/data/Runner 网络并增加 Search；Worker 保留 app/data/Runner。搜索服务的固定镜像、独立数据网和本地 SSH 接入详见 [搜索部署记录](SEARCH_DEPLOYMENT_VALIDATION.md)。

## 前一版本部署与服务验收（历史）

最终源码先在全新的独立 QA 项目通过真实失败、代码生成替身、Runner 验证、CAS 发布、恢复入库、坏候选和回滚四场景，详见 [E2E 记录](CODE_MAINTENANCE_E2E.md)。源码 SHA：`48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96`。

部署前确认主库没有 queued/running 任务，模型 key 为空、每日预算 0。旧源码和私有配置已备份；新镜像构建完成后停止本项目 Beat/Worker/API/Web，备份 PostgreSQL 和应用文件卷，再执行迁移与启动。备份只保存在远端 `deploy/backups/code-final-7c8-20261005/`，没有复制本机配置或将秘密写入日志。

| 最终项目 | 实际值/结果 |
| --- | --- |
| Backend（API/Worker 同一实际镜像） | `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89` |
| Frontend | `sha256:1458187644d93d621690a69f72e0736e76b282e2a21b3b0ae4d694c4da4c42b5` |
| 数据库迁移 | `9d924bc617c7` → `a918eb1a67d2` → `8b7fd5e97379` → `7c8ba429a6ea`；Alembic check 无漂移 |
| 镜像内源码 | 实际复算 SHA 与最终 E2E 相同 |
| 118 文件部署源清单 SHA | `75aa288cec2fb48cd8011582f6791c78c01f5a420d42fe2a82665fb5f73ea6b8`；包含前端/后端/迁移/适配器/构建输入，集合不同于 E2E 程序源码集合 |
| 控制网覆盖文件 SHA | `78690a95e3467a95fcfe977d58ef46117b510f7607053390df9f6860cf978c8d`，补充记录在 `inputs.json` |
| Runner runtime | `sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362` |
| Broker 镜像 | `sha256:68f708c4e814c7cedd48827d288b8fa203170ebfd18a5ab477218c5a40b5a33f`，仅应用网络覆盖配置 |
| 控制网成员 | 精确为实际 API、Worker、Broker；internal bridge，非 host network |
| API/Worker 实际检查 | 第一方 `AdapterRunnerClient.health()` 均 ready；runtime 一致；匿名请求均 401；无 Docker socket |
| Broker 暴露边界 | 宿主端口映射为空，固定内部别名 `adapter-runner-broker:8090` |
| 凭据范围 | Runner token 只在 API/Worker，Beat/migrate/Web/PG/Redis 没有该 token |
| 现有业务影响 | 9 个无关容器的 ID、镜像、StartedAt 未变；本项目 PostgreSQL/Redis 也保持原容器 |
| 最终主库状态 | ModelCall=0、已发送通知=0、代码提案=0、queued/running 任务=0；临时候选容器=0 |

真实 Runner 在应用覆盖配置前已确认没有排队或运行任务，随后重建 Broker 以移除调试端口。QA 服务先行清理，没有占用最终控制网。数据库备份为 162668 字节、应用文件备份为 3911091 字节；摘要在远端私有备份目录 `manifest.json`。

`verify_stack.py` 验证前端和 SPA、就绪、匿名拒绝、内部路由隐藏、管理员登录注销、Origin 保护、零模型预算以及 Beat → Worker → waiting_login；没有外部招聘请求。`verify_internal.py` 从真实 Worker 验证内部 API 无 token 拒绝、有 token 对不存在任务返回 404。没有重复运行不相关的完整业务样本或压力测试。

原版 `scripts/verify_maintenance_ui.py --mode live-read-only --execute` 在部署后通过：实际 5 个来源、15 个代码维护开关、6 个 API 返回 200、JS 错误 0；只建立后台登录会话并发送 GET，没有调整开关、发布或回滚。为访问明确的 loopback 地址，此次**可信 UI 探针**临时使用 host network；它不是候选容器，也不是控制网连通性证明。管理员变量只通过远端临时 0600 文件注入，用后删除；探针容器已移除。

本机部署证据：`.runtime/code-maintenance-deployment-20261005/`。`final.json` 为最终状态，`control-network.json` 保存实际 API/Worker 证据，`stack.json` 为队列 smoke，`live-read-only/summary.json` 和同目录 PNG 为维护页及版本抽屉。维护页截图已人工查看。

远端主目录仍为 `/home/dabing/hr-research-verification-20261005`，Web 仍仅绑定 `127.0.0.1:18080`。维护 UI 截图为 `deploy/verification-results-maintenance-ui-code-final/live-read-only/maintenance-live-read-only.png`。模型和所有代码维护自动开关维持关闭，未借部署验证启用真实业务采集。

## 准备阶段读取的运行状态（历史）

本轮读取了 live health 和容器实际 `.Image`，没有沿用锁文件推断服务版本：

| 项目 | 实际值 |
| --- | --- |
| 主项目 | `hr-intel-verify-20261005`，目录 `/home/dabing/hr-research-verification-20261005` |
| Runner 项目 | `hr-adapter-runner-verify-20261005`，目录 `/home/dabing/hr-adapter-runner-verification-20261005` |
| Broker 状态 | healthy；带专用 token 的 `/v1/health` 返回 ready / python_adapter_v1 |
| 实际 runtime | `sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362` |
| 实际 Broker 镜像 | `sha256:68f708c4e814c7cedd48827d288b8fa203170ebfd18a5ab477218c5a40b5a33f` |
| 准备时主 API/Worker 镜像 | `sha256:c1e39759b7ca0e60b6f7b8e25e5b2bd76953d0bfa2a3fcbcaedf181c516e8650`；最终已替换，见文首 |

准备前 API/Worker 仅连接主项目 app/data 网络，挂载仅 `/data`，没有 Docker socket。另记录 9 个其他业务容器的 ID、名称和 StartedAt，供最终接入比对。

## 网络与凭据边界

```text
主 API ───┐
          ├─ internal bridge：hr-intel-verify-20261005_runner_control
主 Worker ┘                    │
                       adapter-runner-broker:8090
                               │
                       独立可信 Broker
                               │
                      一次性 network-none 解析容器
```

- 控制网络为独立 external/internal bridge，owner label 绑定主项目；最终只有 API、Worker、Broker 三个服务加入。
- API/Worker 保留原有 app/data 网络，不挂 Docker socket。Beat、migrate、Web、数据库、Redis 不加入控制网络，也不需要 Runner token。
- Broker 保留自己的默认网络，用于可信依赖解析访问固定 PyPI；额外加入控制网络，不加入主 app/data 网络，不挂主栈数据。
- Broker 是独立高权限控制面，只有它持 Docker socket。解析/安装容器依旧 network none，不加入控制网络。
- 控制请求仍需专用 Bearer token，内部网络不替代认证。URL 使用固定服务别名，不走宿主端口或 host networking。
- 最终 Broker 覆盖文件已移除所有宿主端口映射。准备阶段曾保留的 `127.0.0.1:18082` 调试入口已关闭。

## 新增配置文件

1. 根目录 `compose.code-maintenance.yaml`：只为主 API、Worker增加 `adapter_control` 网络，保留 app/data。
2. `deploy/adapter_runner/compose.control.yaml`：为独立 Broker 增加固定别名 `adapter-runner-broker`，并用 `ports: !reset []` 清除宿主映射。当前远端 Compose v5.0.2 已实际通过该配置解析；旧 Compose 版本须先确认支持此标签。
3. `prepare_control_network.py`：仅在授权远端 Linux 运行，核对 live Broker、项目与私有配置；创建/复用本项目专属网络，连接现有 Broker；用一次性非 host-network 探针检查认证与实际运行时，再在远端私有文件间写入配置。不会对主服务执行 up/build/migrate。
4. `verify_control_network.py`：只在冻结部署完成后执行；在真实 API、Worker 容器内调用第一方 `AdapterRunnerClient.health()`，核验控制网、认证、固定 runtime、无 socket、无宿主端口、没有额外网络成员和其他业务容器未变化。准备阶段未执行；最终已通过，结果见文首，未把早期探针冒充实际服务验收。

主 `compose.yaml` 的环境变量映射由主工程集成负责，仅映射到 api/worker 服务。最终已验证该映射生效，源同步允许清单也已包含根目录新增的 `compose.code-maintenance.yaml`；准备阶段曾单独同步该文件。

## 主栈所需环境变量

以下值已通过准备工具写入远端 `deploy/server.env`，保留其他配置与文件 0600 权限，未输出 token，也未复制任何本机 `.env`。

| 变量 | 值/来源 |
| --- | --- |
| `ADAPTER_RUNNER_URL` | `http://adapter-runner-broker:8090` |
| `ADAPTER_RUNNER_TOKEN` | 从远端私有 `runner.env` 的 RUNNER_TOKEN 读取，先与 live Broker 比对，仅在远端私有文件间处理 |
| `ADAPTER_RUNNER_RUNTIME_DIGEST` | 上表 live runtime SHA-256 |
| `ADAPTER_RUNNER_TIMEOUT_SECONDS` | `90` |
| `ADAPTER_RUNNER_CONTROL_NETWORK` | `hr-intel-verify-20261005_runner_control`，同时写入 Runner 私有配置 |

准备阶段检查旧主 Compose 时曾缺少 api/worker 前四项环境变量映射；该项已随最终主工程集成、部署及实际客户端检查完成。

## 已完成的准备验证（历史）

- 创建 internal bridge 并连接现有 Broker，固定别名可解析；Broker 容器 ID、StartedAt、镜像未变，没有重启。
- 一只受限可信 probe 容器仅连接控制 bridge，使用同一固定 Python runtime；它没有 Docker socket、主应用数据或 host network。
- probe 未认证请求为 401；认证请求得到真实 ready、contract 和实际 runtime，随后 probe 被删除。
- 准备时主 API/Worker 保持旧镜像与原网络；当时 Broker 之外的 15 个现有容器（包括主 API/Worker）的 ID、镜像、StartedAt、网络与挂载未变化。最终部署影响范围见文首。
- 两套 Compose 合并配置通过检查：主栈只有 API/Worker 将加入控制网；Broker 的最终覆盖配置不发布宿主端口。
- 本机新增 3 项配置文件/非 Linux 防护测试通过，Ruff 通过。没有重跑 Runner 的 12 组隔离压力测试。
- 真实 LLM、通知、平台登录与候选运行均为 0。

远端证据：`verification-control-network.json`、`verification-control-network-before.json`；本机归档在 `.runtime/adapter-runner-control-20261005/`。

## 冻结后的部署顺序（最终已执行）

以下全部在授权远端执行，最终已按此顺序完成。保留命令供同一部署后续维护参考；源码变更需重新冻结和验收。

1. 按主工程冻结清单同步源文件、完成其备份与迁移流程；确认 api/worker 四项 Runner env 映射已包含在实际合并配置中。
2. 确认 Runner 没有尚在排队或执行的任务，再应用独立 Broker 覆盖文件。它可能重建 Broker，因此不能在未知任务仍执行时随意操作。

```sh
cd /home/dabing/hr-adapter-runner-verification-20261005
docker compose --env-file runner.env -f compose.yaml -f compose.control.yaml up -d --no-deps --wait broker
```

3. 主业务镜像、迁移版本按主工程冻结结果准备好后，用两个配置文件启动主栈。以后操作该部署应持续带上覆盖文件，避免下次 recreate 丢失控制网络。

```sh
cd /home/dabing/hr-research-verification-20261005
docker compose --env-file deploy/server.env -f compose.yaml -f compose.code-maintenance.yaml up -d --wait
```

4. 执行真实主服务检查，可传入冻结业务镜像的完整 `sha256:` ID，避免检查旧镜像。

```sh
cd /home/dabing/hr-adapter-runner-verification-20261005
python3 verify_control_network.py \
  --main-dir /home/dabing/hr-research-verification-20261005 \
  --expected-main-image sha256:冻结后的实际业务镜像ID
```

最终已得到 `verification-main-control-reachable.json`：API/Worker 真实第一方客户端健康检查通过，匿名访问仍为 401；控制网络只有三服务，Broker 宿主端口为空，API/Worker 没有 socket，其他业务容器未变化。独立中央维护链路 E2E 已在部署前通过；网络检查本身不创建候选或消耗模型预算。

## 回退边界

连接异常时先保留原业务版本与任务状态，修复网络/env 一致性；不要把连接故障送入模型修复循环。需要撤销接入时，仅移除 API/Worker 的 Runner env/覆盖网络并按部署流程重建这两个服务。外部控制网络仅在确认无成员且确实不再使用时清理，不对其他 Docker 网络执行全局清理。
