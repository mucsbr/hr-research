# 一次真实模型代码修复：未通过验收

日期：2026-10-05。**根已执行唯一一次真实请求，模型输出达到 1200 token 上限，状态为 `output_truncated` / `finish_reason=length`，本次验收未通过。** 不再执行任何新模型调用、重试或换模型；准备、零付费预检停止和正式失败分别保留。

## 实际失败与只读核计

| 项目 | 真实结果 |
| --- | --- |
| run_id / 唯一 QA 库 | `1b50780833a7490ca48b6fab1821b525` / `hr_live_code_qa_1b50780833a7490c` |
| ModelCall | `bd4ba861-b55e-4f59-bb1c-8bcbfb2836cb`，`failed`，错误 `output_truncated`，结束原因 `length` |
| 请求模型 | `glm-5.3`，请求参数 `reasoning_effort=low` |
| 提供方用量 | prompt 5960、completion 1200，合计 7160；reasoning_tokens 提供方报告为 0 |
| 提案与任务 | 都为 failed；尝试数 1、候选上限 1；draft_spec 为空、bundle_sha/release_id 为空、validation_run_refs 为空 |
| 候选产物 | AdapterCodePackage=0、AdapterProgramExecution=0、候选发布=0、带 program 的 release=0 |
| 内置版本 | `ensure_registry` 创建了 1 个 builtin JSON baseline release；它不是模型生成或发布的候选 |
| 后续阶段 | 没有进入候选验证、QA 代码发布或 held-out 解析入库；QA 公司/绑定/岗位/observation 都为 0 |
| Runner 与隧道 | 未见此独立失败 input_id 的运行，临时候选容器=0，临时 Runner SSH 控制 socket/目录=0 |
| 费用边界 | paid intent 和 HTTP intent 各 1，原文件摘要未变；审计新增模型请求=0 |

候选未执行的结论来自这次具体 proposal 的空 draft/bundle/validation refs、零代码包/程序执行记录及候选 release 关联；没有用共享 Broker 的全局历史运行数推断。Broker 只额外核对本实验独立故障输入 `input_41524c56c43996b1911c43054492e5a12feaf3db167c57d3a044b0a59f6eb7ad` 未出现，以及没有临时候选容器。

`model-output.json`、`validation.json` 没有产生；没有完整可用候选，不能评价其修复有效性，更不能把先前 Mock E2E 通过写成本次真实模型质量通过。控制器停止码为 `model_did_not_complete_no_retry`；数据库保存的直接失败原因是 `output_truncated`。

本次用量 7160 token；根核对的此前 2 次/3588 token 保持不变，累计已知 3 次/10748 token。这里报告的是提供方返回的用量，不推算费用金额，也不把 reasoning_tokens=0 解释为模型绝对没有内部推理。

失败证据在 `.runtime/live-code-repair/`：`paid-intent.json`、`http-request-intent.json`、`model-call-audit.json`、`post-failure-db-audit.json`、`post-failure-runtime-audit.json`、`last-stop.json`。当前 `summary.json` 是只读审计生成的 **not_passed** 失败总结，来源标为 `read_only_post_failure_audit`，不是脚本成功路径的通过结果。没有改写原 ledger、主栈配置或任何生产源码。

脚本：`scripts/verify_live_code_repair.py`。默认只生成离线计划、资料和实际生产提示，不读真实模型 key、不连接数据库、不执行候选。准备目录固定为 `.runtime/live-code-repair/`，CLI 没有新建 run_id、替换 ledger 或重置预算的选项。

## 已完成的准备检查（历史）

- 使用最新生产 `catalog_for` / `failure_case` 建立 BOSS 风格的 `.jd-live-qa` CSS 改版资料和独立 oracle；描述长度 3000 字符，明确为合成解析资料。
- 实际生产 `proposal_messages` 生成 15697 字符，未超过 16000；完整 failure case、`selectors` 赋值、后续 DOM 提取循环、返回和失败出口均可见。缺少任一部分会在付费前退出。
- 提示服务源码 SHA：`10aea9607f8c53396f0df754bc7df3f299dea7b1617fc0369e64efedc3a8bab3`；本轮程序源码集合 SHA：`15bd8bc15ea414af7528cbe78bb50f698d308c64ba7121575bdfeeb3d6932325`。
- 计划 run_id：`1b50780833a7490ca48b6fab1821b525`；计划唯一库：`hr_live_code_qa_1b50780833a7490c`，准备阶段尚未创建。
- Ruff、语法编译通过；临时目录自检确认第二次 paid intent 原子拒绝，敏感值不会写入报告。没有连接测试或主数据库。
- `--check-runner` 已通过授权 SSH 临时 loopback 隧道访问真实 Broker health，固定 runtime 为 `sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362`，结束后核对隧道已关闭。没有发布服务器端口、读取本地真实模型 key 或创建候选运行。
- 准备阶段 `paid-intent.json`、`http-request-intent.json`、`state.json` 均不存在，真实模型请求为 0。

## 零付费预检停止及修复（历史）

第一次明确 `--live` 在 SSH、key、数据库和付费动作之前因 `prepared_prompt_changed` 停止：内存字典插入序与 canonical JSON 落盘后的键序不同，导致提示字符串 SHA 不同，但用户 JSON 载荷语义相同。

原停止记录、旧 plan/material/messages 保留在 `preflight-stops/prepared_prompt_changed-f864cdf78db8/`。修复仅涉及 driver：离线先使用真实 create 校验函数，再 canonical 化以保证跨进程稳定；live 创建后强制 `db.refresh(proposal)`，用真正重读的 JSONB 对象产生提示，与离线计划校验语义一致后，冻结其准确字节摘要交给唯一付费 gate。没有假定 PostgreSQL 键顺序等于普通 sort_keys。

两个独立进程的离线验证通过，证据为 `offline-restart-validation.json`；只有对象键顺序可以忽略，system、数组顺序、源码文字和 case 值改变均被拒绝。修复后 driver SHA 为 `9c657e6724acc0890be3c3484bf1bb255e036ef4a154bbc829f0c427f832087e`，离线提示 SHA 为 `f58ab0eae5b8c547756b7b041e1eda88a2dcaedd1e64060a6cd7826e32e8f413`，语义 SHA 为 `52afa5ab11aa1a4244279c57f31c7d37b013528381c2f8c471a87a896e404313`；同一 run_id、预算和 ledger 没有重置。

## 付费和秘密边界

模型固定 `glm-5.3`、`reasoning_effort=low`；输入最多 16000 **字符**，输出 `max_tokens=1200`。唯一 QA 库的日预算和任务预算均为 1，候选上限为 1，不使用备用模型。

发送受到三层约束：

1. 固定实验目录进程锁及不可覆盖的 `paid-intent.json`，先于生产网关调用持久化；跨进程、跨 run 或跨日期不会自动恢复额度。
2. 实际生产 `ModelGateway` 的 PostgreSQL `ModelCall` 预留审计及任务/日预算。已有真实调用在失败、结果未知或完成后都计入预算。
3. 真实 HTTP 传输层核对 POST 地址、模型、low、1200、非流式及精确提示摘要；只有一个匹配的 reserved ModelCall 才可原子写入 `http-request-intent.json` 并发送。底层 transport retries=0，生产网关本身也不重试、不自动换模型。

真实 key 只在 `--live` 中从本机项目 0600 `.env` 读入内存，不放入 argv、报告或 SSH 子进程环境；不复制 `.env`。Broker token 从服务器固定 0600 `runner.env` 经加密 SSH 的捕获管道进入控制进程内存，不输出。SSH 子进程使用只包含系统/认证代理所需字段的环境。模型响应在进入生产处理前检查敏感值回显，报告写入再次检查。

任一阶段失败保存安全错误码并停止；不自动重发模型请求、不重新生成候选、不换模型。脚本还保守禁止已启动实验的自动恢复：即使付费尚未发生而数据库创建中断，也需先审阅原证据，不能盲目重跑或手工删除 paid ledger。

## 实际验证流程

`--live` 仅允许项目 Conda Python。脚本先复核计划、源码和自身摘要及提示完整性，再建立临时 Runner 隧道、核对健康。只有这些步骤通过后，才在本机 PostgreSQL 创建计划指定的新唯一 QA 库并执行真实迁移；库已存在则拒绝，不借用现有测试库或主库。

生产代码维护服务以增量 baseSpec、真实 ModelGateway 和真实远端 Runner 生成及验证一次候选。所有候选源代码只作为文本保存在控制进程，实际执行仅发生在 Runner 的隔离容器；本机不 import/exec 候选，也不运行 Docker。

候选全部验证通过后，只在该唯一 QA 库的 registry 内发布。然后生成一个**未交给模型的新随机 JobID、不同正文、同 `.jd-live-qa` 布局**，通过实际 `ProgramRuntime` 执行列表和详情解析、第一方校验及 `ingest`，核对新增 JobObservation 和正文。随机标记使用纯字母，避免偶然数字被生产 PII 清洗规则识别为电话导致假失败。若模型只是硬编码训练答案，这一步不能通过。

这些是解析资料夹具，不代表浏览器采集、BOSS 登录或真实官网访问；网站请求和平台登录均为 0。主栈、主 registry、真实竞品及现有数据库配置均不改动。QA 数据库、不可变候选和签名回执保留供审计；SSH 隧道在退出时关闭。

## CLI（历史执行方式；付费额度已用完）

```sh
# 默认离线准备，真实模型请求为 0。
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B scripts/verify_live_code_repair.py

# 只核对真实 Runner 健康，结束关闭临时隧道；不读模型 key、不建库。
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B scripts/verify_live_code_repair.py --check-runner

# 该命令已由根执行一次；额度已用完，禁止再次执行或删除 ledger 重试。
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -B scripts/verify_live_code_repair.py --live
```

计划和提示位于 `plan.json`、`prepared-material.json`、`prepared-messages.json`；Runner 准备证据为 `runner-readiness.json`。实际执行后才可能产生 `state.json`、两份 intent、`model-call-audit.json`、`model-output.json`、`validation.json`、`summary.json`；停止原因在 `last-stop.json`。不存在的结果文件不能视为执行通过。
