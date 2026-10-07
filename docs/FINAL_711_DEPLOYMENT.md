# 最终 711 冻结版本部署记录

日期：2026-10-05。主项目 `/home/dabing/hr-research-verification-20261005` 已按三份 Compose 完成增量部署。此次线上产品变更仅为 `adapter_code_service.py` 提示窗口覆盖和 `SentimentPage.tsx` 来源/时间范围表单；没有重跑搜索、模型或端到端场景。

| 项目 | 实际结果 |
| --- | --- |
| API / Worker 镜像 | `sha256:b9cfbdf4b59b3dc06942a185a51741cc07a40c23dbfe8708163026ff9628b672` |
| Web 镜像 | `sha256:1390d3b5dfa62349b8bdbc5210935139ddda2fc2f692f73971bc9d507a762fd9` |
| 根冻结清单 | 123 文件，指纹 `571723e72c798c8fec447f3bdc4d79661695af7d546ff03134bef9c2932040c8` |
| 部署源比对 | 122 个重叠路径全部匹配；仅 `.env.example` 不部署 |
| 镜像内程序源码 SHA | `15bd8bc15ea414af7528cbe78bb50f698d308c64ba7121575bdfeeb3d6932325` |
| 提示服务 SHA | `10aea9607f8c53396f0df754bc7df3f299dea7b1617fc0369e64efedc3a8bab3` |
| 舆情 UI 源 SHA | `d7ef8ac9edfaef34a37f125ef4aaeec183fbefb54a319262fdf76c0943563d11` |
| 119 文件构建清单 SHA | `ae9e960ca9721c7830635b9fbac6eeb2052b6fe922d999b8cf1cd1fee951cd60` |
| 数据库 | 保持 `7c8ba429a6ea`，应用和调度服务健康 |
| 内部连接 | API/Worker 到 Runner 均 ready；API 到 Search health=200；Runner 网 3 成员、Search 网 2 成员 |
| 主库保护 | 10 家公司、6 个来源绑定的全行摘要前后相同；远端 ModelCall=0、活动任务=0 |
| 其他容器 | 9 个无关业务及 PG/Redis/Runner/Search/Valkey，合计 14 个容器的 ID/镜像/StartedAt/网络未变化 |

舆情页面只读探针打开“发起舆情任务”表单，确认未选对象时来源控件禁用、两个 `datetime-local` 字段及北京时间/历史覆盖说明可见，然后关闭表单。8 个 API 均为 200、JS 错误和被阻断请求均为 0；没有点击“开始”、提交业务写入或启动任务。截图已查看。

一致性备份在远端私有 `deploy/backups/final-711-prompt-scope-20261005/`，包含旧源码/配置、177300 字节数据库 dump、3911181 字节应用文件归档。没有复制本机 `.env` 或真实模型 key 到服务器。

本机证据：`.runtime/final-711-deployment-20261005/`，包含 `final.json`、`source-match.json`、`build-source-manifest.json` 以及 `scope-ui/summary.json` / `sentiment-scope-read-only.png`。远端摘要为 `deploy/verification-final-711.json`。

**部署检查通过不等于真实模型质量通过。** 唯一真实模型请求已因 `output_truncated` 未通过验收，5960+1200=7160 token，没有候选执行或发布；详见 [真实模型失败审计](LIVE_CODE_REPAIR_VALIDATION.md)。先前四场景 Mock 模型 E2E 与真实搜索 E2E 属于此前冻结源码的独立历史证据，本轮没有将旧 SHA 的结论冒充新一轮执行。
