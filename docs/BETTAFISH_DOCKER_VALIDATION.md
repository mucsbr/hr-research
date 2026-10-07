# BettaFish 可选容器验证

日期：2026-10-05（Asia/Shanghai）。在用户授权的 `dabing@10.255.200.16` 执行，仅构建和运行独立验证镜像；没有切换或重启现有平台服务。

## 结果

可选镜像成功构建，真实固定版本 ReportEngine 在 **禁网、非 root、根文件系统只读** 的容器中生成了 HTML。默认三次模型阶段预算正确拒绝需要五次阶段的多章模板，拒绝前没有任何合成模型回复，更没有真实模型请求。

| 项目 | 实际证据 |
|---|---|
| 远端独立目录 | `/home/dabing/hr-research-bettafish-eval-20261005` |
| 最终临时镜像 tag | `hr-intelligence-backend:bettafish-eval-20261005` |
| 最终镜像 ID | `sha256:6b6839c0f6e91eed44bc664703961203d19f13d7bc5b3516b39af437e6ccd2fc` |
| 固定基础镜像 | `sha256:94f128394c109e5f4a5790a12dc4b127d5742642bf96dcacd0dca7c8568a723b`，另外建立 base-eval tag，原 `:local` tag 未改变 |
| 固定上游提交 | `b67928ff1ce515dd7672408221fb1e9500d0972d`，Git HEAD 匹配、已跟踪文件干净 |
| 独立解释器 | `/opt/bettafish-venv/bin/python`，Python 3.12.15，`sys.prefix=/opt/bettafish-venv` |
| 锁定依赖 | OpenAI 2.54.0、Pydantic 2.5.2、Matplotlib 3.9.0、NumPy 2.4.6、json_repair 0.53.0；`pip check` 退出 0 |
| 运行身份与隔离 | UID/GID `10001:10001`，NetworkMode `none`，ReadonlyRootfs `true`，CapDrop `ALL`，no-new-privileges |
| 可写范围 | `/tmp` 256MB tmpfs；`/work` 128MB tmpfs，owner 10001；对 `/app` 写入被拒绝 |
| 生产激活/成本 | license=false、LLM key 未配置、运行环境 daily budget=0；Bridge health=`license_pending`；真实模型调用 0 |
| 既有服务 | 验证前后 24 个已有容器的 ID、名称、StartedAt、镜像及运行状态全部一致 |
| 清理 | 任务创建的临时运行容器均已移除；保留验证镜像和证据，不删除其他镜像/卷 |

构建依赖与固定 Git 提交通过服务器普通网络成功取得，本轮没有使用 SOCKS5，也没有改变共享主机的 Docker daemon/proxy 配置。本机没有执行 Docker、没有读取或复制本机 `.env`。

## 真实发现的兼容问题

首次镜像构建成功，但离线运行在模型阶段之前报告 `ModuleNotFoundError`。直接使用 `/opt/bettafish-venv/bin/python -I` 导入真实 ReportEngine 成功，`pip check` 也通过，因此继续检查启动路径。

根因是验证脚本使用 `args.python.resolve()`：标准 venv 的 `bin/python` 是指向系统 Python 的符号链接，resolve 后子进程绕过了虚拟环境。脚本已改为 `expanduser().absolute()`，保留虚拟环境入口。生产 Bridge 原本就不 resolve 解释器，没有修改其这项行为。

只同步该脚本并利用缓存重建后成功。首次失败、修复后成功和最终简报 profile 的各阶段记录均保留，失败没有被覆盖成成功。

## 最终简报 profile 与预算

随后只同步主任务的 `backend/app/bettafish_bridge.py` 小改并再次缓存构建；没有同步仍在开发的其它业务代码。

运行时适配采用 `budgeted_brief`：当每次输出上限为 1200 tokens 时，每章目标 400 字、上限 500 字。保留全部模板主题、证据范围与 JSON schema；将上游按长报告设计的正文密度门槛适配为该简报 profile 的 200/133 字。没有增加任务次数、每日预算、输入或输出 token 上限，也没有修改上游 Git 源码。

它是预算内简报运行适配，**不是四万字报告验收，也不是真实模型语义质量验收**。

| 场景 | 实际结果 |
|---|---|
| compact 模板，默认3阶段上限 | 成功；真实 ReportAgent 的规划/章节/IR/HTML流程，3次合成回复 |
| compact 三次输入长度 | 4124、2672、14096 字符，均低于 16000 |
| compact 每次输出预算 | 1200 tokens，未提高 |
| compact 安全静态 HTML | 175,457 字节，SHA-256 `bc5be342fa00ae4690c2fa84461c707d53a294915cdb4d4dcb63e27d39130a7b` |
| 原始上游 HTML（私有证据） | 2,196,756 字节，SHA-256 `166c44ce2586d9069d11dc2e2c081cdc48a3c148bb9d4728f08d75a186cad2ac` |
| compact 任务 ID | `7a21399c-cd26-4cc9-b333-51f90aa9fbe4` |
| multi 模板，仍用默认3阶段上限 | `bettafish_call_budget`，必要下限5次；0次合成回复，未进入模型阶段 |
| multi 任务 ID | `bee6f08b-ea87-4a48-ab69-92336d0793ed` |
| 最终验证容器总耗时 | 29.03秒，退出0（包含依赖/环境检查及两个场景） |

验证脚本的 SyntheticGateway 使用独立合成设置；生产环境每日预算始终为0。脚本没有隐式真实模型模式，容器禁网提供了额外边界。PDF/曲线平滑为未安装的可选能力，本轮只验证 HTML，不宣称其余格式完成。

## 安全静态导出验证

最后冻结的 Bridge 保留私有 `upstream-result.html`，交付 `result.html` 为静态安全版。下载HTML离开HTTP响应后仍有内嵌CSP，不能依赖原HTTP头。容器内真实生成结果检查通过：无script/iframe/object/embed/form/base标签，无事件属性，无危险href；有script-src/connect-src none的内嵌CSP，正文与HTTPS合成证据链接保留。原始与静态产物分别记录SHA。

这是静态阅读导出，不宣称上游互动图表、主题切换或下载脚本仍然可用。

## Compose 合并核验

在独立目录运行 `deploy/init_env.py` 生成新、权限0600的验证配置，只用于 `config` 检查；没有读取生产凭据，也没有执行 `compose up`。

`compose.yaml + compose.bettafish.yaml` 合并后，migrate/api/worker/beat 四服务均满足：

- `BETTAFISH_REPO_DIR=/opt/bettafish-src`、`BETTAFISH_PYTHON=/opt/bettafish-venv/bin/python` 非空。
- `BETTAFISH_LICENSE_CONFIRMED=false`。
- LLM每日0、任务3、最大输出1200、最大输入16000与基础配置逐项相等。
- LLM key/Base URL保持为空；数据库配置与基础文件一致，但具体值没有写入公开证据。

结果保存在 `compose-merge-validation.json`。这证明覆盖文件的合并行为，没有将该覆盖文件应用到运行中的基础栈。

## 输入指纹与镜像内文件核验

安全同步仅允许 backend/frontend/adapters/deploy/scripts 及 Compose/Dockerignore，排除 `.env`、`.runtime`、数据库、Profile、日志和依赖缓存。最终记录149个允许源码文件，指纹：

`03a5b54f26305581c2aae314545243eee8284956373a8a22e6671dd05e19fbc6`

镜像内读取并与远端输入清单逐项比较：

| 文件 | SHA-256 |
|---|---|
| `backend/app/bettafish_bridge.py` | `3a8f0d831305f2d4b43b80f0325f10f668995fe743eb527fd4fdcac464913e59` |
| `backend/bettafish-requirements.lock` | `c6e8487854a9218186599dc76cf2728df6fa6f0393c7d663313aee1678fe2d5a` |
| `scripts/verify_bettafish_upstream.py` | `aee624d3780f4f0587880a263ef5fbdb04ba56fb1b273d550689192c0ca3dcae` |

输入是本次同步的独立快照，随后仅更新解释器路径脚本及冻结后的预算简报/安全静态导出 Bridge。官网 checkpoint 与其他模块仍在并行集成，**本结果不能当作当前整个平台的最终 Docker 部署验证**。

## 证据位置与复现

远端证据目录：`~/hr-research-bettafish-eval-20261005/verification/`。

本机非秘密副本：`.runtime/bettafish-docker-validation-20261005/remote-evidence/`，包含：

- `source-input-final.json`、`image-source-validation.json`、`base-image.json`。
- `build.log`、`build-fixed-script.log`、`build-brief.log`、`build-static.log`及各自结果。
- `container-runtime.json`、`validation/summary.json`、`validation/pip-check.txt`。
- `validation/output/7a21399c-cd26-4cc9-b333-51f90aa9fbe4/result.html`（交付静态版）和 `upstream-result.html`（私有原始上游证据），以及 `validation.json`、章节和IR；本机下载后分别核对两个HTML SHA。
- `validation/output/bee6f08b-ea87-4a48-ab69-92336d0793ed/validation.json`：默认预算预拒。
- `first-validation/`：虚拟环境路径修复前失败；`before-brief-validation/`、`before-static-validation/`：此前成功版本。
- `containers-before.json`、`containers-after.json`、`isolation-result.json`：24个原有容器没有变化。

构建命令只在授权远端独立目录执行：

```sh
docker build --progress=plain \
  --build-arg BASE_IMAGE=hr-intelligence-backend:bettafish-base-eval-20261005 \
  -f backend/Dockerfile.bettafish \
  -t hr-intelligence-backend:bettafish-eval-20261005 .
```

验证 driver `verification/run_container_validation.py` 固定使用 `docker run --network none --read-only --user 10001:10001`、tmpfs及合成脚本，导出结果后仅移除其创建的临时容器。主要执行入口是：

```text
python /app/scripts/verify_bettafish_upstream.py \
  --repo /opt/bettafish-src \
  --python /opt/bettafish-venv/bin/python \
  --output /work/output
```

最终仍需实际使用方处理生产授权、配置真实模型预算与确认语义质量；本轮没有替用户激活这些条件，也没有向现有平台写入合成舆情内容。
