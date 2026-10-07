# U34 分类数据保留页面验证

日期：2026-10-05（Asia/Shanghai）。状态：本机真实前端构建及隔离浏览器验证通过；本记录不代表远端部署或真实主数据清理已经执行。

## 实现范围

- `RetentionSettings.tsx` 挂载到运行设置，展示四类保留期限、启停、修订、宽限、调度预算和受保护数据说明。
- 管理员可修改策略、选择分类预览、确认执行本批和查询运行结果。普通用户只读；同时核对当前用户角色与服务端 `can_manage`。
- 保留天数使用文本数字输入，拒绝空值、负数、小数、科学计数及范围外整数；只提交已改分类和原修订号。期限 1–3650 天，本批 1–200 项。
- 预览显示本批候选数、预计字节、截止时间、是否还有未计入的候选及限制说明。停用、宽限、空批或过期预览不能执行。
- 执行前显示明确确认，告知平台不能撤销已经完成的正文/文件清理。发送前保存用户隔离的请求标识，组件内同步锁防止双击。
- 网络错误、5xx、408/425/429 按未知结果处理：保留原 `preview_id` 和 `idempotency_key`，刷新页面后恢复，只允许按原键 GET 查询。查询 404 不重发、不新建请求。明确拒绝的 4xx 才释放意图并要求重新预览。
- 202 表示登记，queued/running 继续保留意图；查询到终态后解除执行锁。页面展示 partial/failed 的明细，不把登记或空摘要写成“清理成功”。
- 通用报告与舆情报告的列表、抽屉显示“正文已按保留策略清理”和清理时间，保留原文链接；BettaFish 已清理报告不再提供下载。已打开抽屉使用轮询取得的最新对象，避免继续显示旧正文。
- 未清理却没有正文的报告明确显示暂无正文说明。
- 新增 `date.ts` 统一 `Asia/Shanghai` 时间显示，接入 App 页头、App 简写日期、ModuleUI、Retention；通知页面由其负责 Agent 同步复用。纯 `YYYY-MM-DD` 保留日期精度，不补出时刻。UTC 入库与输入解析没有改变。

## 验证方式与隔离

使用本项目已有 Conda 环境：`/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python`。没有调用、安装或启动本机 Docker。

`scripts/verify_retention_ui.py` 默认 dry，只有 `--execute` 才启动临时 localhost 静态站点及内存 API。页面使用实际 `frontend/dist`，所有写入正文均经过当前生产 `PolicyUpdate`、`PreviewInput`、`RunInput` 校验；未登记接口拒绝请求，不代理真实后端。浏览器只允许本次 localhost origin，禁用 Service Worker。

内存 API 只模拟策略与运行结果。真实数据库写入、文件删除、任务启动、账号访问、外网请求、通知和模型调用均为 0。截图带有“隔离 QA 夹具，不是真实清理”标识。

浏览器时区设为 `America/New_York`；浏览器和夹具使用同一固定时刻 `2026-10-05T00:20:00Z`，避免固定日期意外让预览过期。此时纽约仍是 10 月 4 日，而产品页头必须为北京时间 10 月 5 日。

## 已通过场景

| 场景 | 检查内容 |
|---|---|
| 默认策略 | 90 / 180 / 90 / 7 天，四类已启用，七天宽限可见 |
| 编辑期限 | 保留其他分类原值；`0`、`3651`、`1.5`、`1e2`、`-1`、`abc`、空值均不提交；有效修改只带变更分类与 expected_revision |
| 无改动保存 | 关闭表单，没有 PUT |
| 预览输入 | 非法批量值、空分类不发送；宽限与停用不能执行 |
| 预览与确认 | 本批计数、has_more 和限制说明可见；确认前与取消后没有执行请求 |
| 未知结果恢复 | DOM 双击只发一个 POST；服务器已登记但返回 500，刷新后保留原键；GET 404 不重发；随后 GET partial 恢复并显示失败原因 |
| 策略冲突 | POST 409 明确要求重做预览，不显示成功 |
| 正向排队 | POST 202 只显示已登记；保存 run_id，禁用新预览；GET 到 succeeded 才释放意图，无重复 POST |
| 预览过期 | 过期提示可见，执行禁用 |
| 普通用户 | 只有 GET，无管理员设置请求，无修改、预览、执行按钮 |
| 报告清理 | 列表与抽屉保留原因和时间、来源 URL；空正文另有说明 |
| BettaFish 轮询 | 已打开抽屉从旧正文切换到清理提示，去掉旧正文与下载链接，保留原来源 |
| 北京时间 | 纽约浏览器下页头、共享日期和清理时间一致；日期精度字段仍为 `2026-10-05` |

结果：**13 组通过**。48 个 API 响应：44 × 200、1 × 202，以及故意模拟的 500、404、409 各 1 个。9 次仅内存写请求，包括策略修改、预览与执行意图；不代表 9 次真实清理。JS 错误、越界请求、夹具未登记请求均为 0。

最终浏览器运行前后核对 8 个相关源码/脚本 SHA-256 不变。完整请求、结果和摘要见 [summary.json](../.runtime/retention-ui-validation-20261005/summary.json)。

## 命令与构建

```bash
cd frontend
npm run build
```

最终构建：TypeScript 通过；Vite 3.21 秒，3176 modules。RetentionSettings 静态/动态重复导入警告已消除；仍有既有主包超过 500 kB 的体积提示，本轮没有为此调整依赖。

在项目根目录：

```bash
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check scripts/verify_retention_ui.py
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m py_compile scripts/verify_retention_ui.py
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python scripts/verify_retention_ui.py --execute
```

Ruff、编译和浏览器脚本均通过。此前测试调试遇到 Ant Design 中文“取消”自动插空格与关闭按钮本地化导致的定位超时，已修正夹具选择器；没有将这些定位失败写成业务测试通过。

## 已查看截图

根复核发现`cleared-report.png`曾捕获Drawer入场中间帧。已在截图前等待transform结束，重新运行相同13组，最新成功结果和稳定截图位于`.runtime/retention-ui-stable-validation-20261005/`；没有修改产品或追加测试范围。根已查看完整已清理报告抽屉，提示、北京时间和来源链接可见。

- [默认策略与宽限](../.runtime/retention-ui-validation-20261005/default-policies.png)
- [本批预览](../.runtime/retention-ui-validation-20261005/bounded-preview.png)
- [未知结果保留原标识](../.runtime/retention-ui-validation-20261005/unknown-result-preserved.png)
- [部分完成与原因](../.runtime/retention-ui-validation-20261005/partial-result.png)
- [普通用户只读](../.runtime/retention-ui-validation-20261005/ordinary-user-readonly.png)
- [报告列表、北京时间页头及日期精度](../.runtime/retention-ui-validation-20261005/cleared-report-list.png)
- [已清理报告](../.runtime/retention-ui-validation-20261005/cleared-report.png)
- [BettaFish 打开抽屉后的清理更新](../.runtime/retention-ui-validation-20261005/cleared-bettafish-live-update.png)

## 实施决策与边界

1. 未知清理结果只查询原键，不使用普通重试按钮。依据是已实现的后端持久幂等运行契约与“响应丢失时服务器可能已登记”的故障场景；上述 500→刷新→404→partial 场景验证了前端行为。`sessionStorage` 保证当前标签页刷新恢复，服务端运行列表仍为权威历史；本记录不承诺清空浏览器存储后恢复本地意图。
2. 输入使用受控文本和严格整数校验，避免数值输入组件把非法编辑静默恢复为旧值。没有让页面的宽松解析覆盖后端范围限制。
3. 清理标记优先于旧正文与下载入口，来源链接继续可见。抽屉随报告查询更新，不把原先点击时的对象永远保留为正文来源。
4. 时区使用同一原生 Intl 实现。该调整只影响显示；日期精度信息不转换成时间戳。可回退组件引用，但不应恢复跟随浏览器时区造成的日期偏差。

这里验证的是页面与输入协议，**不代替**后端真实 PostgreSQL、文件系统、任务锁、并发、调度与删除恢复验证。远端构建、迁移、主栈发布和真实后端只读页面检查应有独立部署记录；本轮未改 `.env`、未重建远端镜像、未清理主数据。
