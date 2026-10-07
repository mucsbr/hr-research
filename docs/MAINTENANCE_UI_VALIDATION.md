# 代码维护管理页面走查

日期：2026-10-05。当前结果为通过。浏览器检查使用 Conda `hr-research-dev` 中的 Playwright 与项目已有 Chromium，桌面视口为 1440 × 1100。本机未调用 Docker，未下载浏览器。

## 两种证据范围

| 模式 | 数据来源 | 已验证 | 没有声称验证 |
| --- | --- | --- | --- |
| 本地现网只读 | 刷新后的真实本地 API、既有主库 | 管理员登录、五来源列表、15 个代码维护开关、版本抽屉、真实空态、接口与 JavaScript 错误 | 主库没有代码提案，未据此声称真实提案/发布/采集验收 |
| 隔离 API 夹具 | 独立随机回环端口、内存中的固定 API 契约数据、当前前端构建 | 开关联动、提案详情、diff、依赖变化、验证表格、15 种阶段、CAS 请求与模拟冲突提示 | 未使用真实数据库、模型或 Runner；不证明后端 CAS、实际代码执行或发布成功 |

隔离模式不会把未知 API 转发给真实服务；未登记请求返回 405。画面固定标示“隔离 QA API 夹具 · 非真实模型、采集、发布”。夹具不写主库，也不创建额外 PostgreSQL 数据库。

本地现网模式仅使用私有配置完成一次登录会话，之后在浏览器层拦截所有非 GET 请求和非本机 UI origin 的请求。没有点击登记、开关、模型、检查上游、发布、回滚、采集、邮件或 Webhook 操作。凭据未输出、未写入截图/结果文件。

## 实际结果

本地现网模式：

- 企业官网、BOSS 直聘、猎聘、智联招聘、前程无忧均显示。
- 每个来源的代码模型、固定上游升级、代码自动发布三个开关均为关闭，只读取未修改。
- 打开 BOSS 版本管理抽屉。主库尚无登记历史版本与代码提案，页面如实显示空态。
- 登录后页面及抽屉共 6 个 API GET 响应，全部 200；JavaScript 错误 0，未授权/外部请求 0。
- 总览和版本抽屉截图已实际打开检查。

隔离 API 夹具模式：

- 验证 BOSS 独立启用代码模型后才可操作代码自动发布与上游开关；关闭代码模型清除两个从属开关。旧 JSON 模型开关保持关闭。官网未登记固定上游时该选项禁用。
- 代码提案详情显示基线/当前代次、依赖从无额外依赖到 `beautifulsoup4==4.13.5`、两份源码 diff、截断标记及列表/详情/导航三条验证记录。
- 长连续标识的 diff 可以折行，抽屉内容 `clientWidth=860`、`scrollWidth=860`，没有水平溢出。
- 发布、回滚各一次模拟 409 冲突和一次成功回应。独立夹具端点校验请求确实携带页面读取到的 `expected_active_release_id` 与 `expected_generation`；浏览器等待对应 POST 响应后验证错误提示，避免把其他操作的旧提示当作本次结果。
- 发布模拟成功后显示“已发布，等待恢复采集”。回滚模拟成功后活动版本更新，不再把当前版本作为可回滚目标。
- 逐一验证 15 种阶段的中文状态文案及终态没有发布按钮：等待生成、生成中、候选已生成、锁定依赖、隔离验证中、等待发布、已发布、等待恢复、恢复中、已恢复、结果未知、待配置、失败、基线冲突、取消。
- 已发布后恢复失败的夹具实际显示“失败，请查看版本记录”，不再承诺一定保留旧版。
- 共 114 个 API 响应，其中 2 个为刻意模拟的 409，其余成功；7 次改变状态的请求全部仅作用于内存夹具；JavaScript 错误 0，未授权/外部请求 0。

抽屉截图等待入场动画和边界稳定；错误提示截图等待对应响应及提示动画稳定。固定抽屉使用视口截图，避免全页截图把固定浮层错置到长页面中间。提案详情、发布冲突、回滚冲突及现网页面截图已实际查看。

## 本轮发现与处理

生产页面原先将通用 `failed` 显示为“失败，保留旧版”。已发布后的恢复失败也使用该状态，并且某些失败不会回滚或回滚可能被 CAS 阻断，因此该表述过度承诺。该问题已报告并由主实施方将文案改为“失败，请查看版本记录”；本轮浏览器夹具确认新文案。此走查没有修改其他生产源码或状态机。

脚本开发时遇到两个工具采样问题，均已修正且未改产品：复用已安装的完整 Chromium（`channel="chromium"`），以及匹配 Ant Design 自动插入空格的两字按钮可访问名称。初始动画中间帧截图未用于最终结论。

自动修复事件列表中的部分 `code_` 前缀状态目前仍可能显示原始状态名，作为低优先级显示项记录，本轮没有扩大修改范围。

## 复用脚本与证据

脚本为 [scripts/verify_maintenance_ui.py](../scripts/verify_maintenance_ui.py)。默认只输出走查方案；显式传入 `--execute` 才启动浏览器：

```bash
conda run --no-capture-output -n hr-research-dev python scripts/verify_maintenance_ui.py
conda run --no-capture-output -n hr-research-dev python scripts/verify_maintenance_ui.py --mode fixture --execute
conda run --no-capture-output -n hr-research-dev python scripts/verify_maintenance_ui.py --mode live-read-only --execute
```

本轮私有结果目录为 `.runtime/maintenance-ui-validation-20261005/`，包含：

- `live-read-only/summary.json`、`maintenance-live-read-only.png`、`versions-live-read-only.png`。
- `fixture/summary.json`、`maintenance-fixture.png`、`proposal-detail-fixture.png`、`publish-cas-conflict-fixture.png`、`rollback-cas-conflict-fixture.png`、`published-recovery-failed-neutral-fixture.png`。

证据摘要：

| 项目 | SHA-256 |
| --- | --- |
| 本地现网 summary | `3d11e7f27a708ad2f2c3d17ae053da85abf0fb7e618e1230a8e173b032a0647e` |
| 隔离夹具最终 summary | `6ec78cb5757c702676eb93e008340a3d1ab0faf161efae6484870a7ccd0ed3ed` |
| 当前维护页源码 | `83017f69f8323cdb45878e4e6584d19eab7b5ce5b4dc4018b93f08936c506158` |

两份 summary 各自保存当次 QA 脚本 SHA：现网通过后仅加强了夹具的响应等待和提示截图采样，最终夹具使用新的脚本 SHA；不把两个不同脚本执行混称为同一个指纹。最终 QA 脚本 Ruff 通过。

后端真实 PostgreSQL 并发 CAS、恢复与隔离 Runner 执行证据分别见相关后端测试记录、[ADAPTER_CODE_ORCHESTRATION_VALIDATION.md](ADAPTER_CODE_ORCHESTRATION_VALIDATION.md) 和 [CODE_MAINTENANCE_E2E.md](CODE_MAINTENANCE_E2E.md)。
