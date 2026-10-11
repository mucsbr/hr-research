# 远端容器网络约定

2026-10-11 用户要求修复容器网段冲突并恢复服务。容器网络统一使用私有 `172.16.0.0/12` 中的明确子网；不得依赖 Docker 自动分配，也不使用 `192.168.*`、`10.255.*` 作为容器子网。宿主机地址 `10.255.200.16` 与 Web 入口保持不变。

## 当前分配

已在授权宿主机核对全部 Docker IPAM 及全部 IPv4 路由表（含 Tailscale）。现有 `172.17` 至 `172.31` 的 `/16` 已被其他网络占用，因此选取当前未占用的以下七个 `/24`。这不是适用于所有部署机器的全局可用保证。

| 用途 | 子网 | 私有配置变量 | 配置位置 |
|---|---|---|---|
| 主应用出网、Web/API | `172.16.240.0/24` | `APP_NETWORK_SUBNET` | 主栈 `deploy/server.env` |
| 主应用数据内部网 | `172.16.241.0/24` | `DATA_NETWORK_SUBNET` | 主栈 `deploy/server.env` |
| Runner Broker 出网 | `172.16.242.0/24` | `RUNNER_NETWORK_SUBNET` | Runner `runner.env` |
| Runner 控制内部网 | `172.16.243.0/24` | `ADAPTER_RUNNER_CONTROL_SUBNET` | 主栈 `deploy/server.env`；Runner 留同值备查 |
| 搜索数据内部网 | `172.16.244.0/24` | `SEARCH_DATA_NETWORK_SUBNET` | 搜索 `search.env` |
| 搜索出网 | `172.16.245.0/24` | `SEARCH_EGRESS_NETWORK_SUBNET` | 搜索 `search.env` |
| 搜索控制内部网 | `172.16.246.0/24` | `DISCOVERY_SEARCH_CONTROL_SUBNET` | 主栈 `deploy/server.env`、搜索 `search.env` 同值 |

应用、Runner 出网、搜索数据和搜索出网子网写入对应 Compose IPAM。两个控制网仍是 `external: true`；IPAM 由 `docker network create --subnet ... --internal` 配置，保留已有 owner 标签。不能给 Compose 的 external 网络附加 IPAM。Runner 准备脚本检查子网一致性，遇到旧网段会拒绝复用。

服务之间继续使用 `postgres`、`redis`、`adapter-runner-broker`、`recruitment-search` 等 DNS 名称，不把具体容器 IP 写进业务 URL。PostgreSQL/Redis/Runner/搜索不发布宿主机端口；数据网和控制网保持 `internal`。

## 创建或变更前

仅在已授权远端执行；本机不运行 Docker。

```sh
ip -4 route show table all
docker network inspect $(docker network ls -q) \
  --format '{{.Name}} {{json .IPAM.Config}} {{json .Labels}} containers={{len .Containers}}'
```

检查目标子网与所有非 default 路由、Docker 子网都不重叠，并相互不重叠。先备份三套 Compose、私有环境文件、网络/容器/卷清单，备份目录权限 `0700`、秘密文件 `0600`。切换已用子网时只停止本项目服务，保留数据卷；禁止 `down -v`、全局 prune、全局 Docker 重启或修改其他项目网络。

已创建网络不能直接改 subnet。确认网络 owner 为本项目且 Containers 为空，才能删除并按相同名称、标签和 internal 标记重建。仅有文档记录或名称相似不足以确认所有权。

## 恢复顺序

1. 检查现存卷和镜像，核对三套配置；持久配置必须明确固定后端与前端镜像，避免临时环境变量丢失后恢复旧前端。
2. 重建两个已确认空闲且属于本项目的控制网。
3. 启动 Runner（基础加 control 两份 Compose）和搜索。
4. 主栈持续使用基础、代码维护、搜索三份 Compose，启动 PostgreSQL、Redis、迁移、API、Web、Worker 和 Beat。
5. 核对实际七网段、默认出口、宿主 LAN/VPN 路由、Web 登录、API readiness、Runner 认证健康、搜索健康、Worker 和原业务数据。

当前实例可用已有镜像恢复，使用 `--no-build --pull never`；没有借网络修复升级依赖或运行新的模型分析。详细现场结果见 [网络恢复验证](../docs/NETWORK_RECOVERY_20261011.md)。

回退优先保持停止状态并修正不冲突的子网。旧 `192.168.*` 配置只为审计保留，不应自动重建已造成冲突的旧网络。凭据和命名卷保留原值；需要回退代码时也保留本次固定网段。

官方依据：[Compose 网络/IPAM](https://docs.docker.com/reference/compose-file/networks/)、[Docker 创建网络](https://docs.docker.com/reference/cli/docker/network/create/)、[Compose 服务发现](https://docs.docker.com/compose/how-tos/networking/)。
