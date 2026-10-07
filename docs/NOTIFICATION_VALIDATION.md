# 通知链路独立验证

## 环境与边界

2026-10-05，使用项目 Conda 环境 `/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python`，Python 3.11.13，原生 PostgreSQL 16.15。独立测试库为 `hr_research_notification_qa_test`，没有复用其他 Agent 的测试库或应用库。

测试通过 HTTPX MockTransport、内存 SMTP fixture、固定 DNS fixture 执行；没有调用真实模型、访问 Webhook 接收端或向邮箱发送真实消息。通知函数在 `APP_ENV=test` 下拒绝无显式模拟 transport/factory 的发送。

测试入口 `.runtime/run_notification_qa.py` 从私有开发配置读取数据库连接信息，仅改变测试数据库名，不输出口令。它创建专用库后调用 `pytest`；业务表清理只发生在这个明确的 `_test` 库中。

```sh
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python -m ruff check backend/app/notification*.py backend/tests/test_notifications.py
/Users/songbingrong/miniforge3/envs/hr-research-dev/bin/python .runtime/run_notification_qa.py
```

## 已发现并修复的问题

1. **旧发送覆盖新尝试。** 发送在事务外执行，原实现结束时无条件更新 delivery。在超时恢复为 unknown、管理员确认重试后，旧 Worker 的晚到结果可能覆盖新 Worker 的成功结果。现在完成写入同时匹配 delivery ID、`sending` 状态和该次 attempts，拒绝陈旧结果。并发回归实际制造晚到 503、人工重试成功、旧 Worker 返回的顺序，最终仍保持第二次投递成功。
2. **未知结果没有服务端确认。** unknown 状态人工重试需要 `{ "acknowledge_duplicate": true }`，否则 409；failed/cancelled 仍可按原协议重试。全程保留同一个事件 ID/Idempotency-Key，审计标记重复风险。前端已有风险确认，并已由主 Agent 接入该字段。
3. **SMTP 拒收和未知状态混淆。** `SMTPRecipientsRefused` 明确发生在未接受收件人阶段，现在 450 等临时拒收可有限重试，550 等永久拒收失败，不再当作可能已送达的 unknown。
4. **邮件收件人只在表单校验。** 新增发送前再次验证一个裸邮箱，拒绝逗号多邮箱、注释变形和 CR/LF 注入；SMTP envelope 显式传递单元素 `to_addrs`，避免邮件头解析产生额外收件人。
5. **TLS 不可用仍按普通网络重试。** TLS 证书失败或 STARTTLS/AUTH 不受支持时，在发送前返回固定错误且不自动重试；fixture 证明既未认证也未发送。发送后的不确定错误仍进入 unknown；成功后 QUIT 异常仍保持已发送。
6. **内部主机白名单过宽。** 精确主机授权只允许公网和 RFC1918/ULA 业务地址，拒绝 loopback、链路本地、云元数据、保留/文档、组播及受测 IPv6 特殊地址。实际连接继续固定已验证 IP，保留 Host/TLS SNI；不跟随重定向。
7. **验证失败也可能泄露凭据。** 主 Agent 在应用全局移除了 422 的 input/ctx；通知路由进一步将 urllib URL/端口解析错误替换为固定文字，避免 `msg` 自身回显 token。测试覆盖 URL 路径、非法端口和非法 IPv6 字面量中的敏感标记。列表、创建结果与密文存储也不会返回 Webhook token/签名密钥。
8. **异常发送结果不应伪装成配置失败。** 解密前失败标记配置错误；开始发送处理后遇到未分类异常，保守标记 unknown，避免自动重复投递。Webhook 请求使用 identity 编码和有界原始响应读取，防止无界解压接收内容。

无需数据库结构迁移或中央 Settings 新增字段。Compose 已补充现有 SMTP/通知白名单配置的环境变量转发，默认不配置真实邮箱和凭据；最终业务镜像同步尚待主 Agent 通知。

## 覆盖与结果

`backend/tests/test_notifications.py` 的 30 项测试通过，Ruff 通过。测试覆盖：

- 业务事务回滚时不产生 outbox、相同业务事件不重复投递；相同通道多规则去重。
- 三个并发生产事务共用冷却时间，只产生一条 delivery；两个发送 Worker 只认领一次。
- 读超时进入 unknown 后不自动重试；429 延迟重试保留事件 ID，永久拒绝终止。
- 陈旧 sending 回收、明确风险确认、晚到发送结果不能覆盖新尝试。
- Webhook 签名、IP 固定、Host/SNI、单次发送；302 到元数据地址不会产生第二次请求。
- 飞书时间戳签名与 HTTP 200 下业务拒绝识别。
- 已授权内部主机解析到 127.0.0.1、::1、169.254.169.254、100.100.100.200、fd00:ec2::254、64:ff9b::7f00:1、192.0.2.1、224.0.0.1 均拒绝；正常 RFC1918 fixture 可发送。
- TLS 前置、固定 Message-ID、单收件人 envelope、TLS 失败不认证不发送、450/550 明确拒收、保存配置的多收件人/注释/换行注入拒绝。
- API 成功与失败响应的秘密隐藏，以及测试模式禁止未模拟网络。

## 限制

未验证真实 SMTP 服务或飞书机器人接收，这需要部署者配置实际接收通道。unknown 表示收件方可能已经收到；邮件及通用 Webhook 无法承诺全局 exactly-once，平台通过 outbox 去重、稳定事件 ID、禁止自动重投未知结果和人工风险确认降低重复风险。

这份记录证明通知模块的上述实现和本地隔离回归，不代替最终业务镜像的远程部署验证，也不表明其他仍在开发的模块已全部完成。
