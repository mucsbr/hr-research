# 独立适配器 Runner

仅承担 P8.4 的 Python 解析代码与批准依赖运行，不负责采集网站、登录、发布版本或生成模型候选。API/Worker 通过受限 HTTP 协议使用它，不获得 Docker socket、不导入候选代码。普通网页文本不能提供执行命令、镜像、环境变量、挂载或下载地址。

## 隔离边界

1. Broker 是唯一持 Docker socket 的可信控制面，单实例运行；持 socket 即具有宿主 Docker 管理能力，非 root 用户并不会消除这项权限。
2. 依赖解析只读取固定 PyPI 索引与下载主机，不运行下载的代码；仅允许 beautifulsoup4、soupsieve、typing-extensions、html5lib、six、webencodings、packaging。
3. 安装发生在单独的一次性容器。此容器没有候选源码、任务输入、账号、平台配置、模型 key 或 Broker token。pip 只读 wheelhouse，用完整精确锁与 SHA-256 离线安装到 **64 MiB / 4096 inode 的 tmpfs**，没有宿主可写 bind；禁止 sdist、VCS、额外索引和 `.pth`。安装前还限制所有 wheel 合计展开为 48 MB / 3000 个成员。
4. 固定 bootstrap 导出有界 tar；Broker 拒绝链接、特殊文件、重复/穿越路径，并在持久分配前核对总量和独立缓存配额，随后校验文件摘要并封存。每次调用之前再次核验源码、依赖目录和 wheel 摘要。
5. 候选及已发布包均由新的解析容器执行，只读挂载一个包、对应依赖与一个输入；网络为 none、非 root、只读根、无 Linux capabilities、no-new-privileges、默认 seccomp/AppArmor。可写空间仅为有界 tmpfs。
6. stdout 4 MB、stderr 64 KiB、输入 2 MB；30 秒、256 MiB、1 CPU、32 PID、128 FD。安装阶段单独为 90 秒、512 MiB。解析和安装共用单个执行槽。
7. 超时、取消和输出超限会先断开 attach 通道，再终止整个容器。删除确认后才出具 cleanup 成功回执；不会仅杀掉 Docker CLI 就声称代码已结束。

当前使用 Linux namespaces/cgroups 下的 runc，属于容器隔离，共享宿主内核；不宣称虚拟机隔离，也不把 Conda 当作沙箱。更高隔离需求可在独立节点配置 gVisor/微虚拟机，但当前服务器没有这些运行时。

## 固定协议

所有请求使用 `Authorization: Bearer <RUNNER_TOKEN>`，不接受公开匿名访问。当前部署仅绑定远端 `127.0.0.1:18082`；跨主机连接需要 SSH 隧道或独立 TLS 控制网络。

Canonical JSON：`json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')`。摘要为该字节串的 SHA-256。拒绝重复 JSON key、非有限数字、过深结构和额外顶层执行字段。

### 健康状态

`GET /v1/health` 返回：

```json
{"status":"ready","contract":"python_adapter_v1","runtime_sha256":"sha256:<实际固定镜像ID>"}
```

### 可信依赖锁

`POST /v1/dependencies/resolve`：

```json
{"dependencies":["beautifulsoup4==4.13.5"]}
```

Broker 解析完整传递依赖，处理实际 Python 3.12 补丁版本的 markers，只接受兼容 `py3`、`none-any` wheels。它返回 `lock` 与 `lock_sha256`：

```json
{
  "schema_version":1,
  "python":"3.12",
  "platform":"linux/amd64",
  "dependencies":["beautifulsoup4==4.13.5"],
  "wheels":[{"name":"beautifulsoup4","version":"4.13.5","filename":"beautifulsoup4-4.13.5-py3-none-any.whl","sha256":"<实际下载校验值>"}],
  "resolver_policy":"approved-pure-wheels-v1"
}
```

上面仅示意单条 wheel；实际返回包括全部传递依赖。模型只能提出批准包的精确版本，不提供 hash。上传时必须使用 Broker 已登记且经过实际离线安装的完整锁。空依赖返回空 wheels；更换 runtime 后旧依赖安装证据不能沿用。

### 上传包

`PUT /v1/bundles/{bundle_sha256}`，body 为整个包：

```json
{
  "schema_version":1,
  "contract":"python_adapter_v1",
  "kind":"boss",
  "files":{"adapter.py":"def run(operation, payload):\n    return {'records': []}\n"},
  "dependencies":[],
  "lock":{"schema_version":1,"python":"3.12","platform":"linux/amd64","dependencies":[],"wheels":[],"resolver_policy":"approved-pure-wheels-v1"}
}
```

路径摘要必须与 canonical body 一致。最多 8 个 `.py` 文件、合计 100 KB 源码、160 KB 包；必须包含 `adapter.py`，辅助文件允许简单文件名或 `lib/<文件名>.py`，不接受路径穿越、启动钩子或任意档案。返回 `{bundle_sha256,lock_sha256,created}`，同内容重复上传不会覆盖旧包。

### 提交与查询

`POST /v1/runs`：

```json
{
  "idempotency_key":"repair:incident-id:candidate-1:sample-1",
  "bundle_sha256":"<包摘要>",
  "operation":"parse_list",
  "input":{"html":"<h1>公开岗位样本</h1>"}
}
```

operation 仅 `parse_list`、`parse_detail`、`navigation`。入口固定为同步函数 `run(operation, payload) -> dict`。navigation 只返回计划数据，不获得浏览器或网络操作权；平台必须再次核对已观察链接、身份和动作范围后才能执行。

首次提交先事务持久登记，再交给执行线程。返回 HTTP 202，含 `run_id/status/request_sha256`。相同 key 与完整请求复用原任务；内容不同返回 409。key 允许 `[A-Za-z0-9_.:-]`，最多 160 字符。

- `GET /v1/runs/{run_id}`：查询原任务。
- `GET /v1/runs/by-key/{idempotency_key}`：找回提交响应丢失的任务；路径可 URL encode。404 仅说明当前无记录，不能证明从未执行。
- `DELETE /v1/runs/{run_id}`：请求取消，不自动新建任务。

状态为 queued / running / succeeded / failed / cancelled / unknown。Broker 重启先回收本服务独占标签下的执行容器，把未决记录置为 unknown；不会重新执行。未知结果重交原 key 仍只返回原记录。持久记录达到 1024 条时返回明确限制，需要管理员归档或更换经过审计的保留策略，不静默丢弃幂等映射。

候选输出上限为 4 MB / 24 层；完整内部状态独立限制为 4.5 MB / 32 层，HTTP 响应最多 5 MB。GET 只返回必要结果和回执，不回传原 input。恢复逐条处理状态，损坏记录隔离后为 unknown，不阻塞其他记录或自动重新执行。

缓存按至少 4 KiB 分配块与目录开销计费，写入前分别限制 wheels 64 MB、依赖环境 256 MB、源码包 32 MB、隔离区 8 MB；另限 64 个依赖锁、256 个源码包，容量不足返回 507。原子暂存提交配合 Broker 签名 manifest，允许启动/重试核验并收编“已 rename、未写 DB”的孤立产物。坏 wheel 校验失败后隔离并从固定源重新取得，不能跳过 hash。启动只清理自身 `.tmp-*` 与 builds 下 32 位十六进制临时目录，不操作其他目录。

## 签名回执

终态 `receipt` 包含 run_id、operation、status、request/input/bundle/lock/runtime/output 摘要、实际依赖环境与安装证据摘要、limits、actual_limits、exit_code、oom_killed、耗时、终止原因和 cleanup。

`request_sha256` 包括完整 POST body 中的幂等 key；`input_sha256` 只计算 input。runtime 是 Docker 实际固定镜像 ID。输出为候选返回的非可信 JSON；测试预期、公司匹配、字段验证与是否发布由平台决定，不能采信候选自报的 `tests_passed`。

所有终态都绑定最终 output 的 canonical SHA-256，包括失败、取消和未知结果的 `null`。status 与 error_code 同样被签名。`execution_started` 始终为布尔值；创建前已取消可明确为 false、cleanup=true，曾尝试执行但清理不明时不能声称已完成清理。

签名算法：

```python
key = HMAC_SHA256(token_utf8, b"hr-adapter-runner-receipt-v1").digest()
receipt["signature_algorithm"] = "hmac-sha256-v1"
receipt["signature"] = HMAC_SHA256(key, canonical(receipt_without_signature)).hexdigest()
```

平台应同时核验签名、原任务绑定、已登记 runtime、依赖证据、实际安全限制与 cleanup；不能只验证签名。成功解析的 limits / actual_limits 都包含：network=none、read_only=true、user=65532:65532、cap_drop=[ALL]、no_new_privileges=true、pids_limit=32、memory_bytes=268435456、cpus=1、timeout_seconds=30。

`cleanup={container_removed:true,process_group_terminated:true}` 表示容器删除已确认。异常发生在创建之前或 Docker 不可用时可能为 false，平台不得使用该结果发布。

## 部署与验证

开发 Mac 只运行 Conda 协议测试，FakeBackend 不运行任何候选：

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m pytest deploy/adapter_runner/tests -q
```

将本目录允许文件同步到已授权 SSH 主机的独立目录。以下命令全部只在远端 Linux Docker 主机运行：

```sh
python3 init_env.py
docker compose --env-file runner.env build runtime broker
python3 init_env.py --pin-runtime
docker compose --env-file runner.env up -d --wait broker
python3 verify_remote.py
```

首次全量验证前需保存其他容器的 ID、名称、StartedAt 到 `containers-before.json`，脚本会检查它们未变化。`verify_remote.py` 保存自己的步骤、幂等 key 和结果，可继续未完成验证而不重新提交已执行任务。

`runner.env` 权限 0600，生成独立随机 token；Docker 构建上下文排除配置、数据和证据。Broker 使用独立 data 目录，不能挂载主栈 `/data` 或复制主栈 `.env`。Compose 只启动 Broker；runtime 服务仅为 `build-only` profile，候选容器由 Broker 固定策略创建。不要给平台 API/Worker 挂 Docker socket，也不要设置多个共享数据目录的 Broker 副本。

可信源码：`common.py`、`resolver.py`、`docker_backend.py`、`service.py`、镜像内 `bootstrap.py`。当前独立项目不会修改主 `compose.yaml` 或应用源码。

官方依据：[Docker 安全](https://docs.docker.com/engine/security/)、[运行参数](https://docs.docker.com/reference/cli/docker/container/run/)、[pip 安全安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。

## 接入主栈控制网络

使用根目录 `compose.code-maintenance.yaml` 与本目录 `compose.control.yaml`，将 API/Worker 和独立 Broker 接到专用 internal bridge。Broker 仍是独立高权限服务，业务服务不获得 Docker socket；最终覆盖文件移除 Broker 的宿主端口。

`prepare_control_network.py` 只准备网络、验证认证并在远端私有文件间配置变量，不更新业务镜像或迁移。主工程冻结并部署后，再执行 `verify_control_network.py` 从真实 API/Worker 内验证第一方客户端可达。完整步骤与阶段证据见 [CODE_RUNNER_DEPLOYMENT.md](../../docs/CODE_RUNNER_DEPLOYMENT.md)。

2026-10-11 起出网网络默认 `172.16.242.0/24`，控制网络默认 `172.16.243.0/24`；准备脚本读取主栈的 `ADAPTER_RUNNER_CONTROL_SUBNET`，拒绝复用不同子网。创建前核对所有宿主路由与 Docker 网络，不能自动分配到 `192.168.*` 或 `10.255.*`。见 [网络部署约定](../NETWORKING.md)。
