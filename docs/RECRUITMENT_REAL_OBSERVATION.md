# 腾讯官网第二次真实观察

日期：2026-10-05（Asia/Shanghai）。本次通过运行中的真实本地API和Worker执行一次有界腾讯官网采集，保存第二次观察及前后差异；没有追加采集或扩大预算。

## 对象、范围与执行身份

- 公司：`腾讯（开发验证样本）`，ID `a78b1d08-9336-4d84-8600-831521d83361`，`role=other`。仍是开发样本，未创建或替用户确认正式竞品。
- 来源绑定：`e1bddf7e-3451-4015-914d-7c077b5e9968`；`kind=careers`、`adapter=tencent`，无账号。
- 入口：`https://careers.tencent.com/search.html?query=at_1`。
- 批次：`69d476d4-0ce5-4fe7-9028-7d3549bde8a6`。
- 任务：`e4025482-693a-4ca0-be59-7b10a4b65575`；来源执行：`d6926876-3718-4d1e-bf82-8f21a0e369e2`。
- 检查点：`81a8667d-144e-4ee6-bcb6-2e94af8ef275`。
- API只提交一次带唯一幂等键的 `/api/v1/recruitment/runs` 请求；预算 `max_pages=1`、`max_details=2`，来源仅 `careers`。
- 实际采集时间窗：`2026-10-05T05:27:38.833458+08:00` 至 `2026-10-05T05:27:38.987386+08:00`。任务终态为 `partial`，未请求网络重试。

## 真实结果

| 指标 | 本次结果 |
|---|---:|
| observed | 2 |
| baseline_seen | 0 |
| first_seen | 0 |
| content_changed | 0 |
| salary_changed | 0 |
| suspected_removed | 0 |
| reappeared | 0 |
| 新增Observation | 2 |
| JobVersion数量 | 2 → 2 |
| 变化事件 | 0 |
| 本次任务ModelCall | 0 |
| 全局ModelCall数量 | 2 → 2 |
| 本次通知事件 / 投递记录 | 0 / 0 |

来源状态和coverage均为 `partial`，`complete_observation_applied=false`。停止原因为“达到本次列表页预算，已保存腾讯页码”；检查点保存下一页 `page_index=2`，源站响应总数为2235。本次只观察2条岗位，不将其解释为腾讯全量岗位数，也不能据此推断其他岗位是否下架。

检查点共有3个新HTTP步骤、0个缓存步骤：

1. 1个列表请求：`/tencentcareer/api/post/Query?pageIndex=1&pageSize=2&language=zh-cn&area=cn`。
2. 详情请求：岗位 `1980938696614109184`。
3. 详情请求：岗位 `2072596952738082816`。

三个步骤都保存原始JSON、SHA-256、来源URL和采集时间。详情步骤同时保存解析岗位，原始证据共5537字节。

## 两条岗位的前后对比

| 岗位 | 来源ID | 内容hash | first_seen | last_seen（上海时间） |
|---|---|---|---|---|
| 腾讯会议-iOS 研发工程师 | `1980938696614109184` | 不变 | 保留 | 00:48:20.995647 → 05:27:38.912500 |
| 《王者荣耀》IP在研项目-海外商业化游戏运营 | `2072596952738082816` | 不变 | 保留 | 00:48:21.003949 → 05:27:38.987386 |

对应内容SHA-256分别为：

- `9e3b600eccea2b5109f6e663a979eb164519d6815e2464c824f92fe521c6e81f`
- `ee51e0381b1450dedd10c2e8442fd52d77ed317867b6ff080e03402eee3bee03`

两条来源记录均保持 `status=observed`、`missing_count=0`；新增的是观察记录，继续引用已有JobVersion，没有因再次观察重复创建岗位或版本。首次基线证据曾记录 `baseline_seen=2`、`first_seen=0`；这次两项均为0，不将重复观察误判为新发布。

首轮与本轮均为部分覆盖。当前来源scope按新版来源指纹保存，不能据两个部分范围的无变化结果计算完整公司招聘增减率。

## 成本、通知与原证据保持

执行前确认该公司仅一个启用的官网绑定，使用确定性的腾讯公开HTTP/JSON采集路径；该路径不进入模型学习分支或平台自动修复分支。执行前无同时启用的通知规则与渠道组合。

- 全局ModelCall从2保持为2；这2条是此前已有记录，本次增量为0。
- 该任务的通知事件和投递记录均为0，没有实际发送邮件或Webhook。
- 首轮任务 `927c6461-f1da-43da-ae1d-74571f472e11` 的产物 `.runtime/recruitment-smoke.json` 没有修改。
- 首轮文件执行前后 SHA-256 均为 `58ea399d41067a760930b2be8b9718eadd8c9863fd3913308ba46830c4882717`。
- 未使用本机Docker，未增加正式目标、通知规则或真实账号。

## 复核产物与边界

- `.runtime/recruitment-smoke-second.json`：任务、来源、检查点步骤、前后岗位、观察和版本增量、模型/通知数量及首轮文件hash。
- `.runtime/recruitment-second-preflight.json`：执行前范围和成本检查。
- `.runtime/recruitment-second-dispatch.json`：唯一提交与预算。
- `.runtime/second_tencent_observation.py`：一次性执行脚本，已有结果时拒绝重复提交。
- 界面侧复核见 [FRONTEND_VALIDATION.md](FRONTEND_VALIDATION.md)。

本记录证明当前腾讯确定性适配器的两次真实观察、幂等入库和历史保留。本次没有发生真实岗位新增、修改或下架，因此这些变化判定仍以对应自动化回归证明；也不据此宣称BOSS、猎聘、任意企业官网或已登录账号全部通过真实采集验收。
