# 模型连接与成本配置

更新：2026-10-12。用户指出运行设置只读、供应商/模型默认写死；本轮交付管理员Web配置Base URL、API Key、模型名及预算，保存不调用模型。当前入口：<http://10.255.200.16:18080/settings>。

## 使用

1. 用管理员登录，运行设置 → 模型与成本控制 → 配置模型。
2. 填服务商的Base URL、API Key、模型名/部署ID。模型名是自由文本，没有固定品牌清单。支持Chat Completions兼容接口；Base URL按服务商填写，例如`https://provider.example/v1`，完整`/chat/completions`地址也会规范化，避免重复拼接。
3. 每日请求、单任务调用、单次输出tokens/输入字符、超时均可配置；每日上限0暂停发送请求。失败/未知结果也计入请求数，更换模型不清除历史或重置预算。
4. API Key不回显，留空保留。更换Base URL必须填写新服务的密钥，或清除原密钥；不会把原密钥默认发给新地址。清除密钥时关闭启用，之后可以重新配置。
5. 更多参数包含可选reasoning_effort、max_tokens/max_completion_tokens及同连接备用模型；按接口能力设置，默认不推测品牌能力。失败/超时不自动重试或切换备用模型。

后续调用从共享数据库读取保存的配置，不需要改.env、重启Worker或依赖当前聊天。单次请求使用一致的连接/模型参数；预算预留时在全局锁内再次读取最新限额，若连接或单次预算已改变，在发送前停止，请用户重新发起。管理员修改和请求预留共用锁，旧配置不能越过刚降低的限额。

普通用户可查看状态、模型和成本上限；只有管理员可读连接配置或修改。API Key通过项目既有Fernet密钥加密入库，GET、审计和验证错误均不返回秘密。关闭模型、清除密钥或密钥损坏不会静默退回旧环境变量凭据。

## 工程范围

- `app/model_settings.py`：唯一default配置、加密密钥、修订号CAS、管理员GET/PUT `/api/v1/settings/models`、有效配置读取。
- 新表`model_configurations`和迁移`f52a8e1d9306`，父`e41f0ca928c7`；不改既有调用记录。
- 统一网关、预算预留、系统状态、BettaFish预检/输出、代码维护输入及超时看门狗使用有效配置。没有模型名品牌判断，默认模板不预置供应商模型。
- `ModelSettingsPanel.tsx`提供完整表单，秘密只输入不回填，409保留编辑供刷新处理。正常保存没有网络连通/付费模型测试。
- 不跟随模型HTTP重定向，不把Bearer凭据转交另一地址；允许管理员明确设置可访问的内网HTTP/HTTPS兼容服务。
- 新增配置是全局连接。按角色的独立model_profiles和多个供应商连接仍未实现，PLAN原设计保留；本轮不把全局配置当作多角色配置已完成。原生非兼容协议尚不支持，不能只换URL就宣称适配。

## 验证

- `model-settings-tests.log`：相关76项通过，40.56s，真实独立PG与HTTP、Mock模型；自定义模型/URL/密钥及加密、权限、CAS、预算不重置、默认不注入推理参数、可选输出参数、密钥损坏关闭发送均覆盖。
- `model-settings-full-tests.log`：816 passed，434.29s。随后仅补维护超时看门狗的配置接线及发送前并发重检；新增两项超时用例和两项配置竞态，分别以专项验证，不拼接为818/820项全量通过。
- `model-settings-watchdog-tests.log`：43 passed，42.36s，包括新超时用例；只改看门狗配置读取，未发模型/Runner请求。
- 最终预算重检的网关/模型设置/编排/BettaFish联合`model-settings-final-tests.log`：64 passed，49.32s，含配置在读取与预留之间改变的两项有界竞态；Ruff通过。
- TypeScript/Vite构建通过，`model-settings-web-build.log`；已有大chunk提示保留。Web镜像第一次受通用dist忽略规则影响失败，上传并明确远端Web Dockerfile专用构建清单后成功；构建上下文只含产物及nginx配置。
- `model-ui-qa/summary.json`：实际新API/PG/Web/Chromium表单，两次保存任意模型名/留空密钥、一次新URL无新密钥明确422；JS错误0、1440/1440无溢出、模型调用账本不变。首次QA UI早于API完成启动遇到502，核验脚本增加有界readiness等待后通过。写入仅独立`hr_probe_test`，不创建正式样本。
- `model-ui-production/summary.json`：正式只读打开/关闭表单、密钥空白、连接/模型字段、账本不变，业务写0、JS/API错误0、1440/1440无溢出。

所有验证真实模型请求0，无收费连通测试、真实供应商质量验证或外发通知。

## 部署与保全

远端仍为`/home/dabing/hr-research-verification-20261005`，保持基础+代码维护+搜索三份Compose和probe profile，固定172容器网段。公开URL不变，探针包不变；更新Web后重启TLS网关以重新解析Web容器地址，HTTPS健康核验通过。

部署前保存私有env/Compose/数据库及13表摘要；实际原连接参数和密钥加密保存为私有迁移材料。先增量迁移，再将原配置原样导入唯一数据库记录，最后更新服务；没有替用户选择新的服务商或填写假密钥。

API与实际Worker独立核对所有连接/模型/限额参数及原密钥一致，13业务表全摘要一致，ModelCall仍4条，正式岗位和账号归档保持。Alembic check无差异。私有证据：`.runtime/model-settings-deployment-20261012-01/`及app-data的`maintenance-backups/model-settings-deployment-20261012-01/`，禁止提交Git。

最终Backend为`model-settings-final-20261012`，Web为`model-settings-20261011`。816全量后的生产差异限定看门狗配置读取和锁内预算重检，43/64专项证明其行为；没有重新宣称当前为820项全量。对应HTTPX锁为0.28.1，官方[客户端配置](https://github.com/encode/httpx/blob/0.28.1/docs/advanced/clients.md)和[重定向行为](https://github.com/encode/httpx/blob/0.28.1/docs/quickstart.md)经核对，不跟随重定向的选择符合该版本API。

最终Backend镜像`sha256:6743acd5013205283a6d5cbfb5279053dcbb16c1a92ae1b3f900c69763393b0f`，Web镜像`sha256:8b671fb33158cc01702fc8b5c7311de8f87b309d8d4cfb58d9793ff158b71fcf`。133个产品输入逐项匹配本机与生产目录，其中108个后端/迁移/适配/锁文件另与实际API镜像匹配，差异0；清单文件SHA`f795985bd4bffff0586d5bf736163ce0b9ec1d8c30be5a82f7e3201f8e635109`。此范围不含后加核验脚本/测试，不冒充816测试前后同源清单。最终二次数据核验仍13表一致/ModelCall4，HTTPS健康200，临时QA服务停止。

新增配置/审计有影响但可回退：可在Web重新保存原参数，或恢复旧已验证应用镜像/原Compose；保留新表与所有ModelCall，不删除或换run_id绕过额度。配置的适用性由实际兼容接口决定；本轮没有替用户发真实模型请求来“验证成功”。
