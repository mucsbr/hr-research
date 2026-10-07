# 官网页内检查点恢复

## 实现范围

公开官网的三个已有采集路径现在支持可选检查点：结构化 JSON-LD、经过验证的 CSS 计划、腾讯公开招聘 API。没有改动 BOSS/猎聘/智联/前程无忧的 `platform_collectors`；不传 `checkpoint` 的原官网调用仍走原路径。

这不是只记录“哪个来源失败”：列表页、详情响应、页内岗位位置、待处理详情、下一页、已验证岗位及规则分别持久化。中断后从未完成位置继续，已验证列表和详情不重新发 HTTP。达到每次页面或岗位预算时返回 partial 并保留游标，下一次尝试可以继续推进，不能由此判断岗位下架。

支持的是显式下一页链接、结构化页面、已验证 CSS 规则和腾讯页码/详情 API。任意站点的无限滚动或未经验证的脚本分页仍不在本轮完成范围；无法证明结束时保留 unknown/partial。

## 模型、原文与预算

新增 `OfficialCheckpoint`、`OfficialCheckpointStep` 两个表，迁移由主工程以新文件 `9d924bc617c7_official_collection_checkpoints.py` 注册，未修改已执行旧迁移。

- 检查点保存 task、binding、source_run、attempt、范围指纹、授权来源配置指纹、验证后的规则及游标。
- 步骤保存请求/最终 URL、加载方式、`captured_at`、原文 SHA-256、文件引用、大小、结构化岗位和解析元信息。
- 原文写入 `storage/checkpoints/<checkpoint-id>/<sha256>.txt`，不塞进 JSON 游标或模型上下文。文件读取校验边界、大小和内容哈希；证据损坏时拒绝复用，不能将检查点提交成 complete。
- 原文默认总预算 30 MB、最多 600 步、500 个岗位；游标/元信息上限 200 KB。已有缓存也必须满足当前预算，降低预算不会绕过检查。单页仍有 3 MB 限制。
- 网站适配仍只给模型最多两个 6500 字符的网页摘录。验证后的计划在继续采集之前保存；续跑复用该计划，不重复申请适配。
- 模型请求结果未知时，持久的适配预留阻止自动重复调用。明确未发送请求的 not_configured/预算等错误可以释放预留。若已经观察部分岗位后规则失效，保持旧检查点并要求新采集，不将新旧规则混入同一来源快照。

## 新鲜度和代次边界

默认集合窗口为一小时，可通过 `checkpoint_max_age_seconds` 配置。检查点打开、复用、新写和最终提交均检查最早步骤的 `captured_at`；过期返回 `checkpoint_expired`，需要重新发起不带接续指针的新采集，不能把过期内容当作新鲜完整基线。

`JobDocument.observed_at` 是程序记录的真实响应时间，必须带时区，不参与岗位内容 hash 或 data 差异。接续保留旧岗位的原观测时间，新读取详情使用新时间。`CollectionResult.checkpoint_info` 返回时间窗口、是否恢复、前驱任务、缓存/新增步骤数和状态。主工程 ingest 使用原观测时间更新 first/last_seen，旧缓存不能覆盖更新的岗位记录；若快照遗漏在其开始后另一次采集才发现的岗位，降级覆盖，不据此推断下架。

同任务接管用 `expected_attempt`；显式新任务接续仅通过 `resume_from_task_id`。网址、公司身份/筛选范围、来源配置或规则指纹不匹配时拒绝旧检查点。普通手动或定时新轮不寻找或复用其他任务的快照。跨任务复制只复制通过校验的引用和结构化记录，不把文件重新读取日期冒充 HTTP 观察日期；前驱关系在后续代次中继续保留。

每次推进都检查任务 running、取消标记、当前 attempt 和来源配置。网络读取期间也在响应块之间检查，使取消、时间窗口过期或新代次接管能够阻止旧结果推进。`guard_in_session(db)` 可加入 Worker 已持有的事务，不会另开连接争抢同一 Task 行锁。

统一入库仍由 Worker 完成。已观察岗位的重复 JobObservation/JobChange 不重复创建，同一来源运行的完整缺失判定最多应用一次；已提交 partial 结果后续跑会保留已入库观察并加入新岗位。检查点的 complete 表示本次遍历已结束且证据可用，来源覆盖仍单独使用 complete/partial/unknown，不能互相替代。

## 实际验证

环境：Conda `hr-research-dev`，Python 3.11.13，真实 PostgreSQL 16.15，专用库 `hr_research_checkpoint_qa_test`。HTTP 请求全部由本机随机端口 fixture 提供；腾讯路径通过本地映射读取模拟 API，没有连接真实腾讯或其他外部站点。模型仅使用模拟对象/MockTransport，没有真实 LLM 请求。

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check backend/app/checkpoint*.py backend/app/official_checkpoint.py backend/app/website_agent.py backend/app/collection.py backend/tests/test_official_checkpoint.py
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python .runtime/run_checkpoint_qa.py
```

主回归 **31 passed**，包含检查点、原 `test_website_agent.py` 和 `test_collection.py`；完整迁移与 `alembic check` 通过。随后对前驱信息持续保留及“以步骤 captured_at 为缓存观察时间权威”做定向回归，末次 **3 passed / 29 deselected**；现在共有 32 个不同用例的证据。未因定向元信息变更再次重复整套 Worker 场景。

已验证的具体行为：

- 第二个详情返回 503 后，恢复不重读第一个列表和详情，仅重试失败详情并继续下一页。
- 每次仅一个详情/列表页的预算能逐批推进；JSON-LD 一个页面内多个岗位也能在页内继续。
- 腾讯页码、待处理 PostId 与已成功详情分开保留，三次推进的每个成功请求只发生一次。
- 规则学习后途中失败，恢复只使用已保存规则；未知模型结果不自动再次调用。
- 网址、筛选或规则改变拒用旧检查点；已采部分后页面规则失效不混入新规则。
- 跨北京时间午夜接续，旧岗位的 observed_at 和集合窗口起点保持原值。
- 超过一小时、降低原文预算、原文内容被修改都拒绝复用；没有假 complete。
- 取消时保留此前完成的步骤；旧 attempt 不能写入新进度。

还启动了**真实独立 Celery Worker 子进程**，监听随机命名的 `checkpoint-qa-*` 专用 Redis 队列，使用上述专用数据库。没有监听生产队列，没有启动额外 Beat；结束后退出该子进程并删除自己的队列和 fixture 公司锁。

该 Worker 联合场景实际完成：HTTP 失败 → 保存列表/详情 → 重新入队 → 只读取未完成 HTTP → 正常 ingest；JobObservation 保持 3 条、JobChange 不重复，已完成来源再次入队不发 HTTP。另在响应途中模拟新代次取得数据库所有权，旧 Worker 返回后没有覆盖新代次的进度、summary 或来源状态，也没有提交新岗位。随后用另一个普通新任务验证“已入库 partial 后继续”：三个代次的观察数依次为 1、2、3，每个 HTTP 页面/详情只请求一次，第一个岗位的 last_seen 不被后两次缓存读取刷新，没有疑似下架事件。

## 使用和限制

- HTTP 请求已发出但尚未通过解析并提交检查点时被取消/崩溃，该未验证步骤可能需要重新读取；不把它宣称为已完成步骤。
- 过期、配置变更、证据损坏或规则在部分观察后失效时，应发起新的完整采集，而不是不断接续旧检查点。
- 模型适配结果未知时保留明确状态，不能为了恢复自动重试可能已经计费的请求。
- 本轮没有在本机调用 Docker，也没有更新远端部署。新代码的容器部署与界面状态走查须在主工程集成冻结后另行验证。
