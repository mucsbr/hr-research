# 项目专属招聘入口搜索

此服务将公开搜索结果交给平台作为待核实候选，不执行 LLM、不登录招聘平台，也不访问候选站点。部署只在授权 Linux Docker 服务器进行；本机 Python 检查使用项目 Conda，不调用本机 Docker。

固定镜像：

- SearXNG：`ghcr.io/searxng/searxng@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072`。
- SearXNG 源码：`d48c4b555421e824342c51d68482dd0898e54d0f`，版本 `2026.10.4-d48c4b555`，官方镜像标签与 `version_frozen.py` 已核对；许可 `AGPL-3.0-or-later`。
- Valkey：`docker.io/valkey/valkey@sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11`，来自官方 `9-alpine`。

只保留 Bing 引擎，固定 `https://cn.bing.com`，明确启用 JSON 和中文。默认不配置代理，不修改宿主 Docker daemon。仅有上游引擎返回的 URL、标题、摘要等公开信息；公司归属仍需核实。

## 网络和运行边界

- 独立项目 `hr-search-verify-20261005`，不发布宿主机端口。
- SearXNG 使用官方 UID/GID `977:977`，只读根目录、能力全部移除、512 MB/1 CPU/128 PID 上限；仅缓存命名卷和 tmpfs 可写。
- Valkey 独立于应用 Redis，只有搜索数据内网，64 MB 容器上限、32 MB 数据上限；缓存不持久化。
- SearXNG 加入自己的出网网络、专用内部控制网、Valkey 数据内网。没有主数据库、应用文件、Runner 网络或 Docker socket。
- 专用内部控制网名为 `hr-intel-verify-20261005_search_control`。先独立验证 SearXNG，再在新代码冻结后通过根目录 `compose.search.yaml` 接入主 API；当前已完成，实际网络仅 API+SearXNG，Worker 不增加连接。

主 API 固定接口：`DISCOVERY_INTERNAL_SEARCH_URL=http://recruitment-search:8080/search`、`DISCOVERY_SEARCH_TIMEOUT_SECONDS=15`。搜索专用客户端必须仅准入该完整 endpoint，不得放宽通用爬取器的私网限制。

## 授权服务器操作

将本目录允许文件同步到 `/home/dabing/hr-search-verification-20261005/`，不复制任何本机或主栈 `.env`。用 `python3 init_env.py` 新建专属 0600 配置；已有文件拒绝覆盖，也不输出 secret。

创建控制网络前核对名称没有被其他项目使用，并设置 `org.hr-research.search-control.owner=hr-intel-verify-20261005` 所有权标签。随后执行：

```sh
docker compose --env-file search.env config --quiet
docker compose --env-file search.env up -d --wait
docker compose --env-file search.env exec -T core python -B - --execute < verify_search.py
```

`verify_search.py` 默认只输出计划；`--execute` 只做一次健康读取和一次公开公司查询，输出真实原始 JSON 与摘要，没有候选站点读取、数据库写入或模型调用。搜索引擎失败、HTTP 错误、空结果必须分别记录；健康检查通过不等同于搜索可用。

主 API 接网时合并 `compose.yaml`、现有 `compose.code-maintenance.yaml` 和 `compose.search.yaml`，保留 app/data/Runner 网络。后续更新持续使用三个配置文件。不得把搜索服务加入主数据网络或移除主 API 的现有网络。

实际结果与未执行项见 `docs/SEARCH_DEPLOYMENT_VALIDATION.md`。

## 独立 API 到数据库验收

`verify_api_e2e.py` 只在根确认冻结后使用 `--role prepare --freeze` 生成清单，再在新的 Linux QA staging 和 `search-e2e-postgres` 专用数据库运行。它启动真实 FastAPI 进程，通过 HTTP 创建明确标为 QA 的 `role=other` 公司，只调用一次入口发现；重跑已提交过的发现请求会被拒绝，避免未知结果时重复搜索。

QA API 只允许向固定 SearXNG endpoint 发出一个真实 HTTP 请求；防护层不制造响应，任何候选站点访问、模型请求或第二次搜索均在出网前拒绝。验收检查候选和搜索 Audit 入库、证据摘要一致，同时确保 SourceBinding、ModelCall、TaskRun 均为 0。专用容器/数据库不会复用主库目标。源码、driver 和共享 helper 均校验 SHA。

## 本地 Conda 通过 SSH 使用搜索

本地开发不需要 Docker。运行以下脚本即可把当前远端搜索容器的控制网 IP 转发到本机 loopback；不在服务器发布任何端口，也不读取应用密码或改变本机 `.env`。

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B deploy/search/ssh_tunnel.py start
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B deploy/search/ssh_tunnel.py status
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B deploy/search/ssh_tunnel.py stop
```

默认入口 `http://127.0.0.1:18083/search`。本地 API 可配置 `DISCOVERY_INTERNAL_SEARCH_URL` 为该精确地址，超时 15 秒；脚本本身不写应用配置。需要启动或刷新本地 API 后，新配置才会生效。

`start` 每次通过已授权 SSH 核对固定容器、镜像和当前 IP，已有正确隧道时复用；容器重建换 IP 后再次运行 `start` 会关闭本脚本的旧连接并建立新连接。`status` 会报告目标变化。连接只监听 `127.0.0.1`，控制 socket 和状态文件位于项目 `.runtime/search-ssh/`，权限分别受私有目录和 0600 文件保护；停止时只操作该独享 SSH control socket。

SSH 使用现有密钥认证、BatchMode、ExitOnForwardFailure 和 15 秒 keepalive。连续三次保活失败后连接退出；网络恢复后重新执行 `start`，不会安装系统服务或修改全局 SSH/Docker 配置。可用 `--port` 选择空闲的非特权端口；本机实际验证过启动、健康检查、重复启动复用、仅 loopback 监听及停止，验证期间新增上游查询为 0。
