# P8.4 独立代码 Runner 验证

日期：2026-10-05。执行节点为已授权的 `dabing@10.255.200.16`，独立目录 `/home/dabing/hr-adapter-runner-verification-20261005`，独立 Compose 项目 `hr-adapter-runner-verify-20261005`。本机未运行或安装 Docker；本机 Python 只使用项目 `hr-research-dev` Conda 环境。

## 本轮交付边界

新增 `deploy/adapter_runner/`：可信 Broker、固定镜像 bootstrap、严格协议/签名、批准包依赖解析、持久幂等记录、Docker 执行控制器、独立 Compose、部署说明与验证脚本。没有修改主 Compose、API/Worker 镜像或主栈运行配置。

这是代码/依赖级解析执行能力的真实容器验证；平台任务→维护候选→第一方校验→发布/恢复的中央集成另由后端工作项验证，不能仅凭本报告声明 P8 或完整 V1 全部完成。navigation 在 Runner 内只返回 JSON 计划，不获得真实浏览器或网站访问权。

## 固定版本与隔离环境

| 项目 | 记录 |
| --- | --- |
| 宿主能力 | Docker 29.2.1 / API 1.53，Linux 6.17，cgroup v2/systemd，AppArmor、默认 seccomp |
| 运行时 | runc；未安装 runsc，不宣称虚拟机隔离 |
| Parser runtime 镜像 | `sha256:dcf4eb99272d4c780199c89a2961c51167fc3ee1cdaace6395e042ae1b176aa2` |
| 最终 Broker 镜像 | `sha256:36d0b60d19770643a33ea7f1310ffd3ff292db91b69c72a75bef6fdc83a4cb50` |
| 代码与部署配置快照 | 9 文件 SHA-256 `a7c86b30f00375126ef31f3c10035754def5f223018bda27355ff8bc8576f086`，本机逐文件与远端一致 |
| 控制入口 | 仅绑定远端 `127.0.0.1:18082`，Bearer 专用随机 token |
| 执行容器 | UID/GID 65532，network none，只读根，无 capabilities，no-new-privileges，AppArmor/default seccomp |
| 解析硬限制 | 1 CPU、256 MiB 内存且无额外 swap、32 PID、128 FD、30 秒；stdout 4 MB、stderr 64 KiB、输入 2 MB |
| 安装硬限制 | 独立容器，1 CPU、512 MiB、32 PID、90 秒；无网络，只读 wheelhouse，仅构建自己的依赖目录 |
| 数据持久性 | 独立目录下 SQLite WAL/FULL，幂等 key 唯一且事务先登记；单实例文件锁 |

Broker 是唯一持有 Docker socket 的可信控制面，此权限等同于管理宿主 Docker；不能因为 Broker 以非 root 用户运行就声称该控制面低权限。候选容器和主平台 API/Worker 均没有获得 socket。依赖安装容器不挂候选源码、任务输入或主栈数据。

9 文件快照包括执行源码、Dockerfile、依赖锁及部署配置；其中 `.dockerignore` 为构建上下文保护配置，追加后没有因此重新执行已通过用例。实际运行身份以上述容器镜像 ID 和每次签名回执为准。

## 本机协议验证

命令：

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m pytest deploy/adapter_runner/tests -q --tb=short
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check deploy/adapter_runner
```

结果：**29 passed，0.30 秒；Ruff 通过**。本机使用 FakeBackend，从未导入或执行候选源码，也未调用 Docker。

覆盖：canonical JSON 与签名绑定、重复 JSON key/非有限数字/深度拒绝、批准包精确版本、未知依赖锁与包摘要拒绝、目录穿越/启动钩子拒绝、24 个并发重复提交只创建一个任务、按 key 查询、key 内容冲突、重启 unknown 不重跑、排队取消、存储源码/依赖篡改、无效/超限候选输出、禁止请求覆盖命令/镜像/挂载/URL/env、wheel `.pth` 拒绝。

## 远端 12 组真实验证

使用仓库内 `deploy/adapter_runner/verify_remote.py`。候选均为本项目编写的明确 QA 源码，真实运行地点为上述独立容器；不是用 Conda、普通子进程或测试替身冒充代码隔离。状态文件保留每步、原始请求与幂等 key，恢复验证时不重复已通过用例。

| 验证组 | 实际结果 |
| --- | --- |
| 控制面鉴权 | 无 token 请求返回 401 |
| 批准依赖边界 | 非批准包、版本范围、URL 依赖返回 400 |
| 容器内真实隔离探针 | UID=65532；CapEff=0；NoNewPrivs=1；Seccomp=2；pids.max=32；memory.max=268435456。写根/源码/依赖失败，Docker socket、Broker 数据库、Runner token 不可见；外网和宿主地址连接均失败 |
| 实际依赖安装与两版本 | BeautifulSoup 4.13.4 与 4.13.5 分别从固定索引解析完整传递锁，校验 wheel/hash，在 network-none 安装容器中安装；实际解析返回各自 `bs4.__version__` 和 HTML 标题。`lib/helper.py` 辅助模块可用；两份 lock 摘要不同 |
| 旧产物继续可调用 | 再次使用旧不可变包与 4.13.4 依赖成功，说明新依赖不会覆盖旧版；这项是 Runner 产物回用能力，不替代平台活动指针的事务回滚验收 |
| 并发幂等与未知提交查询 | 12 次并发相同 POST 返回同一 run_id；URL encode 后的 key 查询命中同一记录；相同 key 的不同输入返回 409 |
| navigation | 返回 operation 与输入中的观察链接，Runner 没有访问链接或执行浏览器动作 |
| 输出洪泛 | 达 stdout 4 MB 后终止整个容器，结果 `failed / output_limit`，exit=137，1.033 秒，cleanup 两项均 true |
| 内存限制 | 尝试分配 400 MiB 被 cgroup OOM 终止；`failed / oom_killed`，exit=137，OOM=true，1.098 秒，容器已删除 |
| PID 限制 | fork 到容器 PID 上限后得到拒绝，子进程数量不超过 30，最后全部回收；解析成功返回受限证据 |
| 超时与取消 | 无限等待并派生 setsid 子孙的任务于 30 秒到限，含清理共 30.340 秒，`failed / timeout`，exit=137；显式取消为 cancelled，均确认容器及命名空间已删除 |
| Broker 重启 | 正在执行的容器在 Broker 重启恢复时被回收，原记录为 `unknown / broker_restarted`；再次提交原 key 仍返回原 unknown 记录，没有重跑 |

所有成功解析回执均由外部可信 Broker 生成并用独立 token 派生 HMAC 签名。验证脚本逐项核对 run/status/request/input/bundle/lock/output 绑定、实际镜像、实际隔离限制和 cleanup，不采信候选自报测试成功。

最终执行容器剩余 **0**。构建前记录的 **15 个**既有容器 ID、名称、StartedAt 与收尾完全一致；没有执行全局清理、修改 daemon 配置或重启其他项目。

## 实际发现并修复的问题

输出洪泛首轮真实发现：达到输出限额后停止读取 attach，但保留着 Docker CLI 的 attach 连接，导致 Docker kill 等待流收尾而超时；最终强制删除仍完成，但原 `output_limit` 被覆盖为 `docker_unavailable`。旧 run `1e4561ec-bb4e-4726-884b-9756c3c6031f` 的失败回执保留，不改写为成功。

修复为：先终止独立进程组中的 attach CLI，使传输断开，再强杀整个容器、读取状态并确认删除；保留原始边界错误。只新建 `output-limit-v2` 受控回归任务，没有重复此前通过的隔离/依赖/幂等用例。修复后 1.033 秒返回明确 `output_limit`，容器清理已确认。另给独立 Compose 增加健康检查，验证脚本等待服务就绪后再继续，避免把刚重建时的连接 reset 当作业务失败。

## 证据位置

本机归档目录 `.runtime/adapter-runner-validation-20261005/`：

- `verification-state.json`：所有检查、可信锁、原始请求、run_id、结果、签名回执；包括旧输出洪泛失败与修复后回归。
- `verification-metadata.json`：源码/配置指纹、实际 runtime/Broker 镜像、控制入口、Docker CLI 版本与通过项。
- `containers-before.json`：独立项目创建前的既有容器 ID/启动时间，用于隔离比对。

远端同名证据保存在独立项目目录。私有 token 只留在远端 `runner.env` 与 Broker 配置，没有打印到工具输出或写入本文，也未复制到本机证据。真实 LLM 调用、真实通知外发、真实第三方登录、未知上游采集器源码执行均为 **0**。

Broker 继续保留运行，供随后第一方 `AdapterRunnerClient`、`adapter_program` 与中央维护流程联合验证。后续集成结果应追加记录，不用本节独立组件结果代替尚未完成的业务链路。

## 研究依据

- Docker 官方说明 socket/daemon 是高权限控制面，普通容器共享宿主内核：[Docker Engine security](https://docs.docker.com/engine/security/)。
- 非 root、只读根、capabilities、PID/内存/CPU/日志限制：[docker container run](https://docs.docker.com/reference/cli/docker/container/run/)。
- pip 要求所有直接与传递依赖完整 hash，并推荐拒绝源码包：[Secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)。

## 审计修复后的最终版本

本节取代前述首轮版本身份；旧回执、首轮 12 组结果及中间缺陷保留，不改写历史。审计发现四个实际边界问题后，在同一独立项目增量更新，没有重跑首轮 12 组压力验证，也未改变主栈。

| 项目 | 最终记录 |
| --- | --- |
| Runtime 镜像 | `sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362` |
| Broker 镜像 | `sha256:68f708c4e814c7cedd48827d288b8fa203170ebfd18a5ab477218c5a40b5a33f` |
| 10 文件代码/配置快照 | `abccd65750adfc4264d5317424c528fd14ee9ea542727234d8654bf742eabd80`，本机与远端逐文件一致 |
| 本机回归 | **45 passed / 0.68 秒**，Ruff 通过；新增测试不在本机执行候选或调用 Docker |
| 最终隔离核对 | 原 15 个容器的 ID、名称、StartedAt 不变；候选/安装容器 0，临时可信控制客户端容器 0 |

修复内容：

1. 所有终态绑定最终 output 的 canonical SHA-256，包括失败、取消、未知结果的 `null`；status/error_code 也签名，execution_started 始终为布尔值。只有验签正确的旧版 null 回执才补齐摘要并重新签名，带升级标识，不重跑任务。
2. 候选输出 4 MB / 24 层与完整状态 4.5 MB / 32 层分开限制；保存前校验完整封装，HTTP 上限 5 MB。GET 不回传原 input。重启按条读取；坏记录隔离为 unknown，其他结果保留。深度临界样例也能经状态封装后正常 GET/恢复。
3. 安装 `/deps` 改为 **64 MiB / 4096 inode 的内核 tmpfs 硬限制**，安装容器不再获得任何宿主 RW bind。固定 bootstrap 导出有界 regular-file tar；Broker 先验证路径、类型、数量和总量，再按配额写入自身缓存。所有 wheels 合计展开上限为 48 MB / 3000 成员。
4. wheels、依赖环境、源码包、隔离区分别设 64 MB、256 MB、32 MB、8 MB 配额，按至少 4 KiB 分配块与目录开销统一核算，分配前检查；另限 64 个锁、256 个包。wheel 使用临时文件/fsync/rename；依赖和源码目录带 Broker 签名 manifest，启动/重试能核验并收编 rename 后 DB 未提交的产物，坏件隔离后允许同 SHA 正常恢复。构建准备全程 finally 收尾，启动只清本服务 builds 下 32 位十六进制目录，保留其他命名的文件。

新增本机文件故障回归覆盖原子 rename 中断、部分 wheel 修复、签名孤立 lock/bundle 收编、同 SHA 坏包恢复、分配前配额拒绝、小文件隔离区分配核算、构建准备中断与启动清理、tar 穿越/链接拒绝、组合 wheel 展开预算。缓存字节检查与 tmpfs 硬限额分别验证，不把事后统计冒充安装硬隔离。

### 七项有限真实审计回归

使用 `verify_audit_regression.py`，只新增与缺陷直接相关的受控案例：

- 真实失败解析的 output 为 null，签名摘要等于 canonical null 的 SHA-256。
- 3999900 字符输出形成超过 4 MB 的完整结果封装，可 GET，响应小于 4.5 MB，且不回显 request/input。
- Broker 重启后，上述大结果和签名完整保留。
- 只损坏本轮一个明确 QA 状态行，重启隔离该行为 `unknown / stored_state_corrupt`，不影响其他行。
- 只移除本轮另一个 QA 源码包的 DB 行，重启根据签名 manifest 和文件摘要重新登记孤立产物。
- 对损坏行重交原幂等 key 返回原 unknown 任务，不再次执行。
- 使用我们编写的合成 wheel 直接检查同一安装控制器的内核上限：尝试解压 66 MiB 数据至 64 MiB `/deps`，得到真实 `No space left on device`、exit=1、OOM=false、3.223 秒并完成清理。该测试 wheel 不经过或扩大生产批准清单，未登记、发布或 import；其用途仅是对硬文件系统限额做受控故障验证。

真实安装策略回执记录 `install_tmpfs_bytes=67108864`、`install_tmpfs_inodes=4096`、network none、只读根、UID65532、cap_drop ALL、no-new-privileges、512 MiB 内存与 90 秒上限。没有让大文件写入宿主后再判失败。

### 第一方客户端联合验证：13 项通过

使用 `scripts/verify_adapter_program_remote.py`、`adapters/code/qa_cases_v1.json` 及两个明确 QA 源码版本。将 62 个第一方源码/QA文件固定到独立 `qa-client/` staging，未复制 `.env` 或 `.runtime`。

可信控制 driver 使用已有后端镜像另起一次性容器运行，没有进入或修改正在运行的主栈。其通过真实 `AdapterRunnerClient` 调用 Broker，并使用真实 `adapter_program` 再次验证来源证据；候选源码只作为文本上传，始终由 Broker 隔离执行。driver 仅在远端读取 0600 的 Runner 配置，token 不出现在输出或本机归档中。

| 组合 | 实际结果 |
| --- | --- |
| 旧源码 × 6 个案例 | 旧列表/旧详情/滚动回归通过；复合 ID 改版列表输出不符合预期且被第一方拒绝；移动后的详情发生真实执行失败；观察到下一页时旧版仍返回 scroll，与期望的 follow 不符 |
| 新源码 × 同 6 个案例 | parse_list、parse_detail、navigation 全部与可信预期一致，且通过第一方证据检查 |
| 相同新源码 + 空依赖锁 | 真实 `failed / candidate_exit_nonzero`，签名回执 OOM=false，stderr 明确 `ModuleNotFoundError: No module named 'bs4'`；不会把超时、签名问题或基础设施故障当作依赖有效性的证明 |

新源码实际调用 `BeautifulSoup.select_one/get_text`，依赖锁精确包含 `beautifulsoup4==4.13.5` 及其完整传递依赖。新源码与空锁负对照的 source_sha256 完全相同，只有依赖锁不同。13 份运行结果全部经过 HMAC、请求/输入/源码/锁/运行时/输出绑定、真实限制和 cleanup 校验。driver 总退出码 0；真实 LLM、网站采集、通知均为 0。

这是 **Broker + 真实平台客户端 + 第一方校验器** 的联合验证，不包含平台发布指针和真实采集恢复事务；后者仍需中央集成证据，不能用本节代替。

### 最终新增证据

均位于 `.runtime/adapter-runner-validation-20261005/`：

- `verification-audit-state.json`：本轮 null/大结果/损坏恢复的原始请求、回执和结果。
- `verification-audit-tmpfs.json`：真实 ENOSPC、安装硬限制及清理证据。
- `verification-audit-metadata.json`：最终源码/镜像/隔离比对与 62 文件 staging 清单。
- `first-party-program/summary.json`：13 项联合结论。
- `first-party-program/state.json`：冻结计划、可信依赖锁、包、幂等 key、原始结果和全部签名回执。
- `first-party-program/source-manifest.json`：实际运行的第一方源码/QA文件 SHA-256。
- `verification-audit-artifact-sha256.json`：上述最终证据文件的 SHA-256 清单。

最终 Broker 继续以 `127.0.0.1:18082` 保留运行，供后续中央集成使用。本机 Docker 与真实模型调用均为 0。
