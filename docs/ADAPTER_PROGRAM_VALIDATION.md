# 程序适配器：第一方边界与远端组件联合验证

日期：2026-10-05。本记录覆盖第一方资料整理、字段/公司/导航验证，以及真实 Broker → `AdapterRunnerClient` → 第一方验证器的组件调用。**不把本轮结果当作适配器发布、真实公司 Worker 切换、恢复入库或回滚全闭环已经完成。**

## 已实现的第一方边界

实现为 `backend/app/adapter_program.py`，协议和受控源码说明见 [adapters/code/README.md](../adapters/code/README.md)。该模块不导入候选，也不联网或操作浏览器。

- `prepare_program_input()` 只把独立的 `context.request` 交给远端；公司绑定、身份和完整导航参数留在第一方内存。JSON脱敏保持数组索引；HTML删除脚本、表单、账号及已识别的招聘者区域，正文保留。
- `input_id` 由协议、平台、操作、脱敏资料、公开链接信息及岗位标识的规范化JSON计算；同内容同操作稳定。链接ID由标准化已观察URL与用途计算，不把原URL或秘密参数交给候选。执行器仍须将任务/来源/代次纳入Runner幂等key。
- 列表字段携带JSON pointer或DOM selector/属性/文本切片证据。第一方重新读取原资料核对值；列表JSON行限定原数组中的单个对象，字段引用相对于该行。DOM岗位区域混入多个可识别岗位时拒绝，不能跨岗位拼接公司身份。
- 公司名/编号必须引用完整字段，不能通过切片冒充另一个主体；归属最终使用第一方 `same_company`，配置平台公司ID时优先核对ID。岗位URL由第一方固定模板或与岗位编号一致的已观察链接生成。
- 候选不能提交URL、观察时间、coverage、headers或passed等额外授权字段。空列表是未识别，不证明零招聘。
- 详情只更新已核验岗位的职责/要求，正文必须有原文证据，身份、URL和观察时间保留。
- 导航只允许已观察链接ID、有限scroll和wait。岗位链接还需属于本次已核验岗位；通用click、任意URL、POST和headers禁止。实际执行前仍要检查DNS/重定向/账号锁/取消/代次及任务总预算。

资料是否为登录页、验证码、访客截断或全文，由原第一方采集器判断。字段证据验证不等于证明全文完整，不替代智联 `guestJobDescClamp` 等来源检查。

当前只登记BOSS、猎聘、智联、前程无忧四个平台的URL/主体边界；不声明任意官网程序已接入。对任意未知网页的字段语义和完整性也不作普遍保证。

## 本机验证

环境：Conda `hr-research-dev`。本机没有调用Docker；不导入或执行 `adapters/code/` 中的候选。测试手工提供受控输出，只执行第一方验证器；源码入口检查仅做AST/schema验证。

```text
python -m pytest -q backend/tests/test_adapter_program.py --tb=short --junitxml=.runtime/adapter-program-junit.xml
python -m ruff check backend/app/adapter_program.py backend/tests/test_adapter_program.py adapters/code scripts/verify_adapter_program_remote.py
```

结果：**33 passed in 0.18s**，Ruff通过。覆盖资料脱敏/索引保留、公司ID优先、字段伪造和跨行引用拒绝、公司编号子串冒充拒绝、DOM多岗位区域拒绝、私有链接参数仅在第一方保留、错误岗位链接拒绝、禁止导航动作/总预算、正文证据、不可改身份/时间、稳定内容标识、旧输出不匹配拒绝、空结果未识别及输入/输出上限。

6个冻结QA输入另经原始资料重新prepare，input/link标识一致，独立expected全部经过第一方验证。expected没有通过执行候选计算。

## 远端真实执行方式

由部署执行方在已授权的Linux节点运行 `scripts/verify_adapter_program_remote.py`。可信控制客户端使用新建的独立容器和62个允许源码文件，未进入或修改现有主栈，也未复制开发机 `.env`。Token仅从远端私有Runner配置在进程内读取；输出和归档不包含Token。

候选只由Broker启动新的禁网、非root、只读容器执行；控制客户端不执行候选。底层隔离、安装缓存、磁盘与清理证明见 [CODE_RUNNER_VALIDATION.md](CODE_RUNNER_VALIDATION.md)。本轮在相关Broker修复和审计回归完成后才启动，没有重启已执行的13项。

实际运行镜像：

`sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362`

Driver保存原请求和幂等key，结果未知时查询原job；不会为了继续验证重新提交未知任务。Root Client核对HMAC、请求/输入/代码/锁/运行镜像/输出绑定、实际隔离和cleanup；签名或绑定错误不能当作预期解析失败。

## 13项联合结果

| 版本与场景 | 实际结果 |
|---|---|
| v1：旧列表、旧正文、无下一页滚动 | 3项均真实执行成功、匹配冻结expected、通过第一方验证 |
| v1：新版复合编号列表 | 进程成功，但仅返回空records；不匹配expected、第一方未认可为有效岗位结果 |
| v1：新版正文区域 | 已验签的真实执行failed，旧解析器找不到正文；不冒充签名错误或基础设施失败 |
| v1：已观察下一页 | 返回scroll/wait，动作本身满足边界，但不匹配要求follow/wait的行为真值 |
| v2：全部6个输入，覆盖3种operation | 全部真实执行成功，expected_matches和first_party_accepted均为true |
| 与v2完全相同源码、空依赖锁 | 已验签 `candidate_exit_nonzero`，exit_code=1、oom_killed=false；签名stderr明确为 `ModuleNotFoundError: No module named 'bs4'` |

13个检查全部通过，Driver退出0。这里包含预期失败和不匹配的负对照，不表示13个候选进程均成功。

v1没有第三方依赖；v2锁定 `beautifulsoup4==4.13.5`，实际传递wheel为 `soupsieve==2.10`、`typing-extensions==4.16.0`。新源码真正调用 `BeautifulSoup.select_one/get_text` 生成经原文验证的正文，同源码空锁负对照证明这一依赖必需。

这是“无BS4 → 引入固定BS4”的依赖变化证明，**不是同一库两个小版本的行为差异证明**。独立Runner的其他版本安装/回退证据另见其专项文档；后续发布/Worker切换仍需证明实际选中的复合代码/锁版本。

导航验证的是计划生成和第一方链接/动作核验；本轮没有真的打开下一页，也没有采集招聘平台。

| 固定产物 | SHA-256 |
|---|---|
| v1代码包 | `3a77db61edc45045b59d3f4c49228fd437102b04eae4d5ec4c073cc6441ecdc2` |
| v1/负对照空锁 | `a0e4afd437d067fe23a9aad265f74849c313513b3d8bf35a8fdd92f15b4d8235` |
| v2代码包 | `f3dbfdf6543bd9f8879a94a71940a6a7f246523c4aa292230d81c3121d7e18d0` |
| v2依赖锁 | `48a07700c84a9fc2e7bd441ed58158b37ba2c51b40184edf221106b8bb285010` |
| v2与空锁负对照的相同源码 | `1e50c8937bdc5b48129ee24d3c14204e5e486028c8899425f264af1e703cf5fb` |

本轮真实LLM调用、招聘平台读取及通知外发均为0。依赖解析/下载属于Broker受限依赖通道，不将候选容器禁网误写为整个构建过程完全不访问网络。

## 版本与归档

证据根目录：`.runtime/adapter-runner-validation-20261005/first-party-program/`。

| 文件 | SHA-256 |
|---|---|
| `summary.json` | `df42ba2c8f89cc75d30d1b16f1ec50d8813d594828bfa9ec93909734eab8b569` |
| `state.json`，含13份请求/结果/回执 | `d9e8d492a28f93ad96e44e5d0ce4acb39eeba499eece332442699f524480e913` |
| `source-manifest.json` | `0f9fd2f9af989f04c4561b896d5888e7978fc7d650ff82d36b782ba7890ee76f` |

62个staging文件的清单聚合SHA为 `8d495e0dc98b520bb5191394a4581a0350b69bd969d025a4ce4524cecfe95995`。13项归档复核时，已复算上述三个文件SHA，并确认第一方边界、Root Client、Driver、两份候选和可信cases均与该清单一致。随后进行的JSON私有键修复单列在下节，不能继续沿用旧helper指纹冒充新版本。

本轮实际边界模块SHA：`86a12b6babb3438ba6ee64f961ce7d616e1f081677d407c21852277f173c28af`。Driver SHA：`96685661d5fcfc8741214e5a8c3e42280760a805930fbb30c33af2a00e6aeda6`。

这一staging清单不是整个平台的生产部署指纹。Broker只监听宿主loopback时，可信控制客户端可达并不证明主栈bridge网络内API/Worker也已可达；控制网络、复合发布、实际Worker使用新包、恢复入库、回滚以及登记上游真实变更仍由后续中央集成单独验收。QA源码明确为受控改版样例，不冒称已经升级真实开源仓库。

## 13项之后：JSON私有字段命名变体修复

后续审阅发现，原私有键匹配覆盖 `userInfo/bossName/securityId` 等camelCase，但可能漏掉 `user_info/boss_name/security_id` 等命名变体。已修复为仅在私有字段匹配时移除 `_` 与 `-`；保留下来的原键不改名，数组不压缩，原始输入不被修改，证据pointer仍引用原路径。

新增定向用例同时检查user_info、user-profile、boss_name、security_id、access-key、__session_token__被移除，job_id/job_title/company_id/company_name/user_research正文仍保留，并以带连字符的 `/data-set/jobs/1` 和原字段pointer完成第一方核验。

- 修改后整个边界测试文件：**34 passed in 0.19s**，Ruff通过。
- 当前helper SHA：`7f96981bb988b07f3f4d1a5ead8e4fd74eb6f164a90ee433838bcc0c81f9ddd3`。
- 原6个QA输入重新prepare后内容标识未变，独立expected再次通过第一方验证。
- 没有重新执行13项远端候选，也没有在本机执行候选；33项旧helper+13项远端与34项新helper本地验证分别记录，后续中央集成统一复验。
- 证据：`.runtime/adapter-program-key-normalization.json`、`.runtime/adapter-program-key-normalization-junit.xml`。
