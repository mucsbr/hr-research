# 2026-10-11 远端网络恢复

## 范围与现场证据

用户已 down 服务，要求恢复且容器不使用 `192.168.*`、`10.255.*`，改用 `172.*`。操作仅在已授权 `dabing@10.255.200.16` 的主栈、Runner、搜索三个独立项目进行，本机仅编辑源码。

恢复前这三个项目没有容器，主库、Redis、应用文件和搜索缓存命名卷仍在；两个控制网络为空，owner 标签属于主项目。它们的旧 IPAM 分别为 `192.168.128.0/20`、`192.168.144.0/20`。基础 Compose、Runner default 和搜索出网/数据网均未固定 IPAM。现场 `172.17` 至 `172.31` 的 `/16` 已被其他 Docker 网络占用。没有获得用户原故障的具体目标地址/报错，不能把自动分配事实当成完整复现原故障。

对全部 Docker 网络及全部 IPv4 路由表（包含 Tailscale）做重叠检查后，选择七个未占用 `/24`：`172.16.240.0/24` 至 `172.16.246.0/24`，分工见 [网络部署约定](../deploy/NETWORKING.md)。三个 Compose 合并配置均通过检查，两个旧控制网络确认空闲及所有权后重建；未执行全局 prune、修改 daemon、重启 Docker 或删除数据卷。

## 部署与运行结果

使用已有镜像，`--no-build --pull never`；后端继续 `hr-intelligence-backend:remote-active-20261005`。私有环境未持久设置前端镜像，原 Compose 将回落到旧 `:local`，因此补充 `FRONTEND_IMAGE=hr-intelligence-web:remote-active-20261005`，复用先前验证的前端产物。没有重新构建应用或升级依赖。

| 检查 | 结果 |
|---|---|
| 实际网络 | 七个指定 `/24` 全部一致；本项目无 `192.168.*` 或 `10.255.*` 容器子网 |
| 主服务 | API、Web、PostgreSQL、Redis、Worker 均 healthy；Beat running；迁移任务退出码 0 |
| 辅助服务 | Runner Broker、SearXNG、Valkey 均 healthy |
| 控制网成员 | Runner 为 API/Worker/Broker 三成员；搜索为 API/SearXNG 两成员；均保留 internal 与 owner 标签 |
| 宿主端口 | 仅 Web 发布 `10.255.200.16:18080`；数据、Runner、搜索没有宿主端口 |
| Web/API | 容器经宿主入口 HTTP 200；两项前端静态资产 HTTP 200；客户端直接访问 HTTP 200、readiness ready |
| 登录 | 用现存管理员和私有环境中的凭据登录通过；验证会话随后注销，无重置密码 |
| 业务数据 | 正式三公司仍在；API 读取宇树 26 岗、智元 50 岗；此前部分覆盖边界不变 |
| Runner | 实际 API、Worker 的第一方客户端均 ready，runtime digest 匹配；匿名请求 401；Worker 无 Docker socket |
| 搜索 | 实际 API 经固定别名访问 `/healthz` HTTP 200；本轮未发公司搜索或访问外部业务网站 |
| 模型网络 | Worker 至既有模型网关 TCP 可连接；未发送模型 HTTP 请求 |
| 数据卷 | 使用原项目命名卷，没有清空、重建卷或更换凭据 |
| 数据库版本 | 实际 `alembic_version=b25d0c8f3142` |
| 调用审计 | 当前 ModelCall 仍为 4 条；数据库已知 prompt+completion 合计 15371 tokens，历史失败保留；enabled plans=0 |
| 其他项目 | 16 个其他容器 ID、镜像、StartedAt、状态类别、网络不变；其他 Docker 网络 ID/IPAM 不变 |
| 宿主路由 | `10.255.200.17` 经 ens33；`192.168.3.1`、`192.168.50.1` 经 Tailscale 的 table 52，均未改变 |

本轮验证限定部署配置、网络、真实登录和已有数据读取；不是新一轮全量应用测试，也不证明登录后真实采集或模型质量。本轮没有创建业务任务、触发分析、发送通知或追加真实模型调用。

## 私有证据与回退

远端证据目录：`/home/dabing/hr-research-verification-20261005/.runtime/network-recovery-20261011-01/`，目录权限 `0700`；三套原 Compose/环境文件按项目备份，秘密文件 `0600`，未下载到本机或提交 Git。

现场材料含 `containers-before.json`、`networks-before.json`、`routes-before.json`、`planned-subnets.json`、`configs-after.json`、`control-networks-replaced.json`、`network-final.json`。原文与数据库历史归档仍在原迁移目录。

后续更新保持主栈三份 Compose、Runner 两份 Compose；固定网段随源码保存。回退时保留原凭据与命名卷，并使用经过重叠核验的 `172.*` 网段；旧 `192.168.*` 网络配置只供审计，不自动恢复。
