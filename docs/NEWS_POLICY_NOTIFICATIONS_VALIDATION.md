# U33：资讯结构化订阅与政策生效提醒

日期：2026-10-05（Asia/Shanghai）。本轮实现PLAN 13.2、DEC-0040已记录的范围。没有新增通知服务、工单、多租户或真实收件目标；没有调用外部模型、采集网站或真实通知端点。

## 已实现契约

- `NotificationRule.news_filters`保存`regions/industries/topics`三个数组。字段内OR、非空字段之间AND，并与事件、关注对象、关键词条件共同满足；标签去空白、去重、大小写规范化后精确匹配，未知标签不命中，不推断地区/行业层级。
- 非空资讯标签只允许资讯事件；新`news.policy_effective`只允许即时模式。接口与表单均保留默认关闭，选择事件不会自动启用通道或规则。
- `emit_event`附带新闻标签快照，`rule_matches/apply_rule`为即时和日报共用入口。资讯首次路由及claim前重核使用同一份已保存标题/摘录，避免裁剪后的关键词判断不一致；其他模块原匹配方式保持。
- 修改或停用资讯规则时，取消不再有有效规则支持的queued/retrying即时投递；同通道其他规则仍匹配则保留。日报继续按规则取消，各规则互不影响；规则启停有审计与取消计数。不自动补发历史事件。
- 主资料入库时同步政策提醒，仅接受官方`policy_document`、`full_text`且唯一明确的`effective_date`。来源权威性仍基于原有管理员配置，不宣称完成法律效力核实。
- 固定生效日北京时间09:00。首次发现已过去的政策不登记；当天09:00后可生成“今日施行”；原已登记但停机错过时间的提醒恢复时标明“延迟提醒”。提醒不判断政策是否适用于本公司，并继续遵守已配置规则及冷却条件。
- 待处理提醒使用当前主资料标签及主来源关联对象，不把不同转载来源的标签合并为政策适用范围。

## 日期修改、去重与状态

Reminder按`item_id/effective_date`唯一，事件键为`news:policy_effective:<item_id>:<date>`，投递继续按`event_id/channel_id`唯一。

日期A→B→A若尚未生成事件，可以恢复仍在未来/当天的原计划；一旦该日期已生成事件，就不自动复活。日期缺失、歧义、失去全文/官方主资料条件或关键范围依据变化时，旧计划取消；尚未发送的旧投递取消。sent/unknown记录保留，不能因日期重新出现重新排队。手工重试也重新核对政策、规则与目标变化，失效提醒拒绝重发；其他已有unknown确认机制不变。

提醒状态为pending、emitted、cancelled。emitted只表示生成了通知事件，不能解释为实际发送成功；无启用匹配规则时不会创建投递。页面分别显示计划及投递记录，保留原文链接和北京时间。

上下文仅保存subject/source/source-run UUID与依据哈希，不复制密钥、账号会话、原文正文或个人联系方式。政策事件正文使用脱敏标题/摘录；政策日期事件`NotificationEvent.task_id=None`，采集来源运行引用只在上下文中保存。

## 事务与锁顺序

采用短事务advisory gate `notifications-state-v1`，覆盖事件路由、规则/通道改变、摘要生成、政策同步/到期、投递claim和人工重试。新闻在既有`news-ingest`锁之前取得gate；其他业务可以先持Task/自身ingest锁再发事件，因为gate持有路径不会反向取得Task锁。gate会串行化这些短数据库事务，适用于当前单实例V1；没有将网络发送串行化。

规则和通道修改先取得gate，再取得相应行锁。新闻主资料、Reminder变化、原有事件与outbox同事务。到期处理每次最多20条（调用上限100），每条事务重新校验主资料和日期，状态与事件共同提交。

发件在gate内重新核验政策与有效规则，再将Delivery置sending并提交；网络请求始终在事务外。修改发生在已claim之后不能撤回in-flight消息，继续按原有sent/unknown及attempt-CAS收尾，不将未知结果自动重发。

真实PG两Session专门覆盖隐式FK窗口：A持原采集Task行FOR UPDATE并等待gate，B持gate真正生成政策事件。政策事件task_id为空，不取得旧Task的FK KEY SHARE锁，两边均能完成。另在Mock接收器回调内完成政策修改事务，证明网络阶段没有持gate；修改在claim之前则不会调用接收器。

## 迁移与中央接线

迁移：`a14c9b7e2031_news_policy_notifications.py`，`down_revision=7c8ba429a6ea`。只增加规则JSONB列和`news_policy_reminders`表；后者引用NewsItem/NotificationEvent并有唯一、到期索引。由主工程串接后续retention迁移`b25d0c8f3142`，没有另起head。

模型定义放在已有`notification_models.py`，沿用原注册。中央Worker hook：

```python
from app.news_policy_reminders import queue_due_policy_reminders

# 现有 app.deliver_notifications 的最前面：
queue_due_policy_reminders(limit=20)
# 随后沿用 queue_due_digests() 和 process_outbox()
```

返回值是新建事件数，不是外发数。主工程已把该hook接入现有15秒通知调度；本模块没有修改main、workers、queue或config。

## 验证与边界

Conda：`hr-research-dev`。专用PG库：`hr_research_news_policy_test`，未使用主库或integrated测试库。

- 首轮U33专用20项通过，2.19秒；产物`round1-junit.xml`。
- 加既有通知/日报/资讯与此前内容事务回归，101项通过，8.97秒；产物`combined-junit.xml`。
- 补有界调度、时区、关闭后不补发、结构化条件修改后，最终相同范围 **105项通过，9.37秒**（JUnit9.334秒，0失败/错误/跳过），产物`final-junit.xml`。本轮U33专用文件24项；各轮有重叠，不能相加。
- 覆盖接口权限/默认值/非法组合、OR/AND与原条件组合、官方全文日期门禁、首次过期与09:00边界、延迟与双dispatcher、事务回滚、A→B→A、sent/unknown、共享通道、规则/日期修改、手动重试拒绝、Task隐式FK锁及事务外发送。
- Ruff和diff格式检查通过。旧schema到串行head的数据保留升级验证由主工程另做，不将本专库重建测试冒充升级保存测试。

UI脚本为`scripts/verify_news_policy_ui.py`：真实构建React、localhost HTTP内存夹具、生产RuleInput验证。覆盖编辑保留、三类标签规范化、混合非资讯事件拒绝、清空数组、生效提醒切为即时且不启用外发、三个提醒状态及纽约浏览器内固定北京时间显示。没有连接真实API、数据库、接收器或Worker。

证据根目录：`.runtime/news-policy-validation-20261005/`。UI结果和截图位于`ui/`；`initial-probe.json`保留首次虚拟长列表点击未进入视口的验证失败，后改为可见中文标签搜索选择，没有删除断言。最终页面与共享北京时间格式一起构建和验证；源文件、脚本、最终结果的指纹另保存于本目录。

此证据证明工程和受控接收器行为，不代表已经向真实邮箱/飞书投递。最终平台全量回归、真实Worker接线和远端部署由主工程统一签收。
