# 舆情资料保留与资讯部分结果：补充验收

日期：2026-10-05（Asia/Shanghai）。本轮只补齐独立审计确认的三组原需求缺口：舆情详情不被后续摘录覆盖、资讯到达时间预算后保留已读文章、资讯页面显示资料范围/日期不确定性并接入行业筛选。没有增加平台范围、外部账户、真实模型或通知调用。

此前644项及远端主栈部署属于修复前冻结版本。本轮新增修复已纳入统一 **701项通过，133.90秒** 的最终源码回归，123个实现文件前后SHA均为 `7991a3b9cc7df37490aedef6e9146c0ff3c07764725124d443a31eec1ecc60cd`；Ruff及Alembic通过。新主栈部署仍待签收，不能用701项签收旧镜像。

## 发现与复现

1. `ingest_documents` 原来无条件覆盖主记录。同一帖子先有256字详情，后只采到17字搜索摘录时，正文被缩短，资料形式变为 `search_excerpt`，已知发布时间及作者变为空。独立审计最初以临时ORM对象复现，后续以真实PostgreSQL与导入API重现。
2. `collect_news_source` 在读取下一篇前达到时间预算时直接抛异常；调用方在进入 `ingest_news` 之前终止，当前来源已经读取的文章没有保存。Feed读取完成后到入库之间达到预算也存在同样问题。
3. `NewsPage` 未消费 `classification`；Feed摘要显示为“正文”，后端已有的日期候选/不确定性不展示，行业筛选参数未接页面。

初始审计结果：`.runtime/content-independent-audit-20261005/reproduction.json`。此文件是修复前结果，不作为修复后验证。

## 修复行为与自主取舍（供主工程登记DEC）

### 主资料与每次观察分别保留

- 资料形式优先级为 `detail_body` 高于 `source_text/video_description`，后二者高于 `search_excerpt`。这里只衡量可读资料的完整程度，不代表外部内容事实已经核实。
- 同等资料形式的新观察可以替换旧正文，即使新内容更短；不以长度最大原则永久保留已被编辑的旧内容。更完整的详情也可以替换更长的搜索摘录。
- 较低资料形式不覆盖主正文、标题、分类和主出处。每次观察仍记录本次原始正文、形式、出处和独立RawDocument，不把保留主记录解释成跳过观察。
- 解析结果中缺失的作者/发布时间不清空已有值；未出现的互动量字段不补0。明确取得的新字段按观察更新。
- `last_seen_at`仍是帖子最近被观察时间；`classification.content_observed_at/content_source_run_id`记录主正文的取得时间与来源运行，`latest_observation_extent/source_page_url`描述最新观察。分析证据同时携带这些字段，避免把新搜索摘录时间当成旧详情重新取得时间。
- 旧记录没有上述主正文时间或运行引用时保持未知，不用当前摘录时间补造历史。无新表或迁移。
- `counts.retained_primary`单列保留主资料数量；不把较低资料形式当成主正文发生变化。收藏/已读/忽略和既有通知去重保持原行为。

### 时间预算允许部分结果，取消与过期执行不允许

- 采集函数遇到 `time_budget` 且已经取得文章时，返回带 `stopped_code`、限制与错误说明的部分结果。
- 入库前在同一提交事务内以Task行的 `FOR UPDATE` 重新读取并检查取消、执行代次和运行状态；仅允许时间预算已过，不放宽取消/旧Worker拒写。
- `check(for_commit=True)`不提交心跳释放行锁。时间预算partial路径一直持锁到文章、来源状态和Task终态一并提交；取消/时间异常分支也不提前提交已有行锁。
- 栏目页中途停止或Feed读取后到达预算，都能保存已取得文章并将来源/任务标为 `partial`。后续来源不再开始，摘要明确记录请求来源数与未尝试来源数。
- 没有任何已读文章时仍明确失败；取消与代次变化不会以“保留部分结果”为由提交当前未入库资料。

### 页面表达原文边界

- 列表与详情展示原文正文、Feed摘要、Feed内容等资料形式；未标注数据不推断为全文。
- 展示后端不确定性说明及原文提到的发文/生效日期候选，不替用户从多个日期中选择一个。
- 行业筛选使用既有 `industry` API参数，与地区/主题筛选并列；详情同步展示行业标签。

## 先红后绿与真实数据库证据

环境：`/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python`；独立PostgreSQL库 `hr_research_content_fixes_test`。没有使用或清空 `hr_research_integrated_test` 和开发主库。

`backend/tests/sentiment/test_content_fixes.py`先新增8项参数化用例，随后补2项真实PG两Session的最后事务窗口回归。覆盖详情遇三种较低形式时主资料与两份原文观察分离；同等形式真实变短及摘要升级详情；栏目与Feed到达预算保留文章/不读取下一来源；已提交取消/新代次拒写，以及最终行锁与partial终态原子提交。

| 轮次 | 结果 | 产物 |
|---|---|---|
| 修复前8项 | 6失败、2通过，1.29秒 | `red-junit.xml` |
| 首次修复后8项 | 3失败、5通过，1.44秒 | `green-targeted-junit.xml`（名称不表示通过） |
| 最后事务窗口修复前的所属组 | 113通过，控制台7.93秒；JUnit7.781秒，0失败/错误/跳过 | `green-suite-junit.xml` |
| 最后事务窗口复现 | 2失败，0.60秒；另一Session已提交取消/新attempt，旧执行仍落库1篇 | `commit-fence-red-junit.xml` |
| 行锁与相关资讯回归 | 28通过，2.97秒；两个竞争更新均等待完整partial事务提交 | `commit-fence-final-junit.xml` |

中间3项失败是测试期望原始中文查询URL，而既有 `safe_url` 会规范化URL编码。改为解码后比较同一出处，保留全部原文、日期、观察、模型和通知断言；没有修改生产URL规则来迁就测试。

最后事务窗口回归在真实ingest入口建立屏障。修复前竞争Session观察到Task/Source均running、Observation0，成功提交取消或新attempt后旧执行仍写入文章。修复后竞争Session受Task行锁阻塞，直到同一次提交后才看到Task/Source均partial及1条Observation，再更新取消或新attempt；旧执行不会随后覆盖其新状态。测试不在持锁期间join对方线程，并设置有限lock_timeout，避免测试自身死锁。

以上产物均位于 `.runtime/content-independent-audit-20261005/`。Ruff检查通过；前端TypeScript/Vite构建通过，3.42秒，已有公共依赖体积提醒仍在。测试组包括同目录原有舆情、资讯、Bridge、统计与助手回归；不是全项目全量，不和旧644相加。

## 实际页面验证

验证脚本 `.runtime/verify_content_fixes_ui.py` 使用真实构建、真实Chromium和仅本机的内存API夹具；所有非同源或非GET请求直接阻止，不代理到实际业务API。

- 修复前构建实际显示缺少行业控件、Feed范围、不确定性与日期候选，保留 `ui-red/summary.json` 和截图。
- 新构建10项检查全部通过，包括发出 `industry=制造业`、第二个行业结果不再出现、Feed摘要标题、未验证全文提示、两个候选日期、未知时间和横向无溢出。
- 6个API响应均200，JS错误0、越界请求0、实际数据库写入0。页面只有显式夹具数据，不冒充真实公网采集。
- 已实际查看 `ui-green/news-detail.png` 和 `ui-green/industry-filter.png`，日期说明、行业标签、摘要标题和筛选结果均可读。

`.runtime/content-independent-audit-20261005/source-final.json`是补最后Task行锁之前的历史清单，不能用它签收最终news_service。最终文件SHA：

- `backend/app/news_service.py`：`16a2b36842dfb54ed17913a153f78716d3cbc15be8c023a2afc1d7b16a3b4106`。
- `backend/tests/sentiment/test_content_fixes.py`：`441b2ab70e9d8406f3f59f79b28a18541707902a0e02c00e57ba0626bf253cd4`。
- `backend/app/sentiment_service.py`：`d166dbd2df109adfc92ba3fed57114fb5f6b7d22c3a37603377824e79337a388`。
- `frontend/src/NewsPage.tsx`：`dce53ef8e03d6bfb67379e76e95e895eea6c0c27399dc5781111ecfd948ca999`。

统一最终证据为 `.runtime/refinement-integrated-junit.xml`（701项、0失败/错误/跳过，XML133.862秒），以及 `refinement-source-before.json` / `refinement-source-after.json`（123个实现文件、同一7991…SHA）。实现清单不含测试文件，测试版本以上述单文件SHA及JUnit另行留证。所属文档/模块任务没有改 `App.tsx`；该文件的计划表单由另一任务修改并纳入统一回归。没有调用本机Docker、外部网站、真实LLM或收件端；本模块增量的最终主栈部署另待统一签收。
