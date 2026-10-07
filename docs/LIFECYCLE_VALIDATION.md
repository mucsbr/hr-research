# 资讯订阅、政策日期提醒与数据保留验收

日期：2026-10-05（Asia/Shanghai）。补齐需求审计U33、U34。用户最新要求节约账号额度并仅在必要时使用子Agent；最终收尾由主Agent执行，本轮新增真实模型请求0。

## 实际功能

- 资讯规则支持行业、地区、主题精确筛选，字段内OR、字段间AND，并保留既有对象/关键词条件。即时和日报使用同一匹配入口。
- 政策提醒只登记官方来源、全文、唯一明确施行日期的资料，固定北京时间09:00；首次过期不补发，停机延迟明确显示。日期、规则变更和手动重试均重新检查，已发送或结果未知不自动复活。
- 生效提醒显式选择且默认不启用外发。复用已有通知队列/outbox，稳定事件与通道去重；不判断政策是否适用于本公司。
- 原文快照90天、报告180天、任务日志90天、已过期会话7天；首次启用、缩短期限或重新启用有7天宽限。管理员可修改、预览、执行并查看运行记录，普通用户只读。
- 清理保留业务行、来源/原始哈希及清理元数据，排除活动任务、岗位历史、模型用量、通知记录、代码包、检查点和账号Profile。已清理报告仍可查JSON元数据，原HTML下载返回410。
- 每小时调度有界保留任务；手动任务持久登记后入maintenance队列，发布结果未知时按原幂等键核对。目录FD与no-follow防止越界/符号链接；文件删除前持久意图，失败留下partial与可重入记录。
- 全部日期显示统一Asia/Shanghai；仅日期资料不补造时间。已打开报告抽屉会随清理标记更新正文和下载入口。

## 验证

**最终完整回归：765 passed，147.16秒，0失败/错误/跳过。**

使用项目Conda `hr-research-dev` 与独立 `hr_research_integrated_test`。产物`.runtime/lifecycle-final-junit.xml`、`lifecycle-final-tests.log`。Ruff通过。

131个实现/部署输入测试前后逐项相同，指纹`4c586689c88caeefcb37db02e36e75d20550f86cb895e2accd5995cb958dd7d7`；记录`lifecycle-source-before.json`、`lifecycle-source-after.json`。原711基线和镜像保留历史范围，不签收本次新功能。

- 资讯相关最终105项定向通过；真实PG、模拟接收器，包含Task隐式FK锁、双dispatcher、事务回滚、A→B→A、规则修改、未知结果及网络阶段无数据库锁。详见[资讯通知验证](NEWS_POLICY_NOTIFICATIONS_VALIDATION.md)。
- 保留服务26项定向通过，并纳入765全量；durable intent提交后任务重启/原文引用变化先红2项，修复最终刷新门禁后4场景通过。嵌套BettaFish章节、IR、报告、请求与日志按固定上游布局清理，缓存/未知文件保护。
- 中央应用22项通过；新路由鉴权、普通用户只读、跨站写入阻断和过期会话有效性保持。
- Worker闭环2项通过：真实API预览/登记→队列载荷→注册Celery任务执行→临时文件/数据库结果及幂等；真实资讯入库→到期Worker→模拟outbox发送。队列发布为受控替身，任务主体实际执行，不声称此用例经过真实broker。
- 旧7c8 schema种入Task/Channel/Rule/RawDocument/Report后升级b25，原列摘要不变；新策略/7天宽限正确，再次升级不改策略ID或宽限，Alembic check无漂移。独立证据`lifecycle-migration-9f01b05df0e4/summary.json`。
- 资讯页面6组真实浏览器检查通过，结构化字段交给生产RuleInput；保留页面13组通过，生产Policy/Preview/Run输入校验。均为localhost内存夹具，无真实清理、任务、外发或模型调用。
- 抽屉截图采样已等待入场动画结束，最新13组及完整抽屉图位于`retention-ui-stable-validation-20261005/`；原功能验证及动画中间帧保留。根已查看默认策略和稳定报告抽屉。

## 部署与运行

迁移串行：`7c8ba429a6ea → a14c9b7e2031 → b25d0c8f3142`。本机使用Conda及原生PostgreSQL/Redis，Docker仅在已授权服务器独立主栈执行。

本轮部署保留私有配置、源码、一致性数据库和应用文件备份；不向主数据执行手动清理，也不创建通知目标或新竞品。实际部署完成结果另附本节，不以文件编写或测试库迁移代替部署通过。

实际部署已通过，证据`.runtime/lifecycle-deployment-final.json`：130个宿主部署路径匹配（131清单仅.env.example不部署），99个镜像内后端/适配器输入匹配；API/Worker/Beat镜像`sha256:71625d437e37e227889a190982feef1a5c9a73821f0bc174b0ea7dc312387ef3`，Web镜像`sha256:649ad78fe264feb9836bc0981c02b96f5812f21729c66eadf984351a845ec73d`。迁移b25、Worker新任务注册及真实readiness/策略/运行/事件类型接口通过。

10公司、6绑定、4原文、2报告、16任务及28日志的原列摘要前后一致，远端ModelCall/活动任务/已发通知为0；14个非更新容器未变化。策略四类均启用并处于7天宽限，清理原文/报告0，手动清理运行0。备份在远端`deploy/backups/lifecycle-765-20261005`。备份工具旧镜像引用失效与错误Alembic入口均在实际迁移前停止，修正后从已有进度恢复；日志保留，未重复构建或重跑业务任务。

本机也已用Conda升级b25并刷新API/Worker/Beat，readiness与Worker注册通过，主开发库仍为历史2条ModelCall，原文/报告清理0。证据`.runtime/lifecycle-local-runtime.json`；本机一致性数据库备份`.runtime/config-backups/lifecycle-765/database.dump`，没有调用本机Docker。

## 剩余边界

真实招聘/社交账号、正式名单与收件目标、BettaFish适用授权仍需实际配置。上一轮唯一真实代码修复请求输出截断，质量仍未验收成功；本轮没有重试、切换模型或删除付费ledger。相关记录见[模型验收](LIVE_CODE_REPAIR_VALIDATION.md)。
