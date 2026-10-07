# 代码维护中央接线：统一回归记录

日期：2026-10-05（Asia/Shanghai）。本记录对应代码维护组件接入平台后的统一回归；远端执行与主栈部署另列，不以本地测试替代。

## 本轮实际结果

| 检查 | 结果与范围 |
|---|---|
| 后端最终全量 | **644 passed，132.06秒，0失败**；包含既有业务、代码包/执行客户端、程序边界、发布/回滚、真实PostgreSQL状态与实际Worker并发测试 |
| Ruff | `backend/app`、`backend/tests` 通过 |
| 开发库迁移 | head `7c8ba429a6ea`；Alembic check 无新增操作 |
| 前端 | TypeScript与Vite生产构建通过，最后一次3.34秒；公共依赖主包仍有体积提示 |
| 本地服务 | 核对旧进程cwd与主库无排队/运行任务后刷新API、Worker、Beat；最终岗位跳转修复后再次刷新API，readiness返回ready，Worker使用完整七队列且新维护任务已登记 |
| 实际模型成本 | 主开发库仍2条历史ModelCall；本轮回归使用明确Mock，没有新增真实模型请求 |
| 本机执行边界 | Python使用项目专用Conda；没有调用本机Docker，没有在API/Worker/本机测试进程执行候选Python源码 |

命令：

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check backend/app backend/tests
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python .runtime/run_integrated_tests.py \
  --junitxml=.runtime/code-platform-integration-round3-junit.xml
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m alembic -c backend/alembic.ini check
# frontend目录
npm run build
```

统一测试使用独立 `hr_research_integrated_test`，通过测试启动器在内存派生连接，不向报告写入密码。API/Worker并发用例保留真实HTTP、Redis锁、PostgreSQL与屏障断言；网站传输和模型按用例用替身，不称为真实账号采集。

## 真实发现与修复

1. 新冻结版本会被旧JSON恢复快照继承，导致修复发布后仍使用旧解析规则。新恢复任务删除旧adapter_versions与resume指针，重新冻结发布版本。
2. 原JSON恢复仅使用收尾时看到的generation，无法防止收尾之前已完成的A→B→A。真实PostgreSQL负例先失败；现在事件和恢复来源快照持久保存原发布代次，准备与回滚均核对它。缺失历史证明时拒绝猜测，保留明确原因。
3. 官网已有规则失败原先会先进入另一套规则学习模型，代码维护队列拿不到原解析错误。独立代码维护启用时，已有规则的失败直接交给代码维护；初次无规则仍保留原学习流程。真实Worker用例验证两页资料、零模型调用即可进入可信代码提案。
4. 官网原职责oracle要求职责区域等于整页文本，正常的“标题+职责”详情页被拒绝。现在接受唯一具有明确职责语义的区域，多个独立职责块继续拒绝；expected由第一方建立。
5. 发布后的恢复状态曾仍显示“取消提案”；该操作不能取消实际恢复。服务端拒绝已发布恢复阶段的提案取消，UI隐藏按钮，实际任务通过任务入口取消。取消发生在Worker认领之前也会由补偿扫描结算。
6. 前端failed状态原文案“失败，保留旧版”不适用于发布后的恢复失败。最后改为“失败，请查看版本记录”，不推断实际活动指针。
7. 最后新增岗位跳转负例，实际复现程序采集把同平台另一岗位页面的正文附到原岗位。现在详情调用Runtime前核对最终URL岗位ID与列表核验ID；不一致在执行详情程序前拒绝。来源的第一方检查失败时，保留已执行run IDs，但不把整轮code_execution标成verified。负例先红，相关51项通过，最终纳入644项全量。

## 第一轮失败没有被隐去

第一轮统一套件结果为 **635 passed、3 failed，157.14秒**。三个失败均是既有真实并发测试的collector夹具未接受新增 `descriptor` / `runtime_scope` 参数，抛出TypeError而没有进入屏障；并非通过删除并发断言解决。

修复仅更新夹具合同，增加Task、SourceRun、attempt及完整冻结版本核验，返回实际冻结fingerprint。独立数据库文件复验4项通过、2.42秒，第二轮统一回归643项通过、135.89秒。随后发现并修复上面的详情跳转问题，第三轮最终644项通过。第一轮原JUnit保留为 `.runtime/code-platform-integration-round1-junit.xml`；具体复现和修复结果为 `.runtime/runtime-contract-reproduction-junit.xml`、`.runtime/runtime-contract-final-junit.xml`。

## 版本与证据

- 最终全量JUnit：`.runtime/code-platform-integration-round3-junit.xml`。
- 最终测试前后113个显式实现文件清单：`.runtime/code-integration-round3-source-before.json`、`.runtime/code-integration-round3-source-after.json`；逐文件完全一致，两份聚合SHA均为 `22bfb331773ea04dbd1a8ad51a918dd9c8e8637e23a406bf37e152895678042e`。
- 第二轮历史清单为 `code-integration-round2-source-before/after.json`；它们的backend与adapters一致，唯一变化曾是 `frontend/src/MaintenancePage.tsx` 中性文案，之后已重新构建。最终第三轮包含这份最终前端源码，不再存在当轮清单差异。
- 详情跳转红/绿证据：`.runtime/program-redirect-red-junit.xml`、`.runtime/program-redirect-green-junit.xml`；最后的来源verified=false断言也在第三轮完整套件验证。
- 代码编排77项专用证据及锁竞争取舍：[ADAPTER_CODE_ORCHESTRATION_VALIDATION](ADAPTER_CODE_ORCHESTRATION_VALIDATION.md)。专项测试与全量存在重叠，不叠加数量。
- 官网程序与原生失败桥：[CODE_MAINTENANCE_VALIDATION](CODE_MAINTENANCE_VALIDATION.md)。
- 独立Runner与控制网络：[CODE_RUNNER_VALIDATION](CODE_RUNNER_VALIDATION.md)、[CODE_RUNNER_DEPLOYMENT](CODE_RUNNER_DEPLOYMENT.md)。

## 远端联合证据与剩余边界

首轮真实远端E2E属于详情跳转修复前的历史快照，已单独保留。最终源已用新QA项目重新执行四场景并全部通过：两次真实代码发布/恢复入库，坏候选两次可信执行被拒绝，CAS回滚后实际执行旧代码和beautifulsoup4 4.13.4依赖锁并新增Observation；影子业务表摘要不变。最终证据为 `.runtime/code-maintenance-e2e-dd39b8ca/`，driver后端源集SHA `48d50c98eea6658923973a03f8f2a9b0cd62ebf86435c1f8e76d26bff1627b96`；它的文件集合与本记录113文件清单不同，不能直接比较两种聚合SHA。

该真实E2E包含6次Worker到内部HTTP调用、4次Mock模型回复，真实模型/通知/平台登录均0。最新主栈已部署并验收：API与Worker实际镜像 `sha256:e57dc6d7852b3957c73e9ed0928082bf959f441d933de61797c083bccb29ce89`，Web镜像 `sha256:1458187644d93d621690a69f72e0736e76b282e2a21b3b0ae4d694c4da4c42b5`，迁移7c8且无schema漂移。API/Worker真实第一方Runner健康检查、匿名401、控制网仅三服务、无Docker socket与Broker宿主端口关闭均验证通过；九个无关容器及本项目PostgreSQL/Redis未变化，临时候选容器0。

部署允许清单为 `.runtime/code-maintenance-deployment-20261005/source-manifest.json`，118文件聚合SHA `75aa288cec2fb48cd8011582f6791c78c01f5a420d42fe2a82665fb5f73ea6b8`；主实施方已逐路径重新计算本地文件并全部匹配。它包含额外构建/部署配置，与本地113文件及E2E后端源集各有明确范围。最终机器结果为同目录 `final.json`、`control-network.json`、`stack.json`，备份在授权节点 `deploy/backups/code-final-7c8-20261005`。

管理页面已完成真实本地只读、隔离API夹具交互及部署后远端只读走查；远端五来源/十五开关/六个GET接口200、JS错误0，主实施方已视觉查看远端总览截图。队列smoke返回真实waiting_login，明确验证队列和内部HTTP认证，不冒充真实平台采集。远端主库ModelCall、已发通知、代码提案、运行/排队任务均0；本地仍只有历史2条ModelCall。细节见 [MAINTENANCE_UI_VALIDATION](MAINTENANCE_UI_VALIDATION.md)、[CODE_RUNNER_DEPLOYMENT](CODE_RUNNER_DEPLOYMENT.md)。

真实账号、真实通知目标、BettaFish适用授权及代码生成的真实模型质量仍属于运行接入或后续验收依赖，不由这些Mock回归与受控网站E2E证明。

已有可信计划和明确分页可采集；匿名官网任意交互式无限滚动不在当前已验证范围。恢复准备遇到来源行锁竞争会记录明确失败，不倒退Task.attempt、不突破一次恢复约束、不据此再付费或触发解析回滚；发布阶段的暂时锁竞争则保留同一候选等待下轮推进。
