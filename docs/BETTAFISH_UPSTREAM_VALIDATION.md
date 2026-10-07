# BettaFish 真实上游运行验证

日期：2026-10-05。已在本机独立 Conda 环境及授权远端可选镜像中执行固定 Git 源码的真实 ReportAgent。模型响应均来自确定性合成网关，付费调用为 0。本记录证明真实引擎、预算门禁及 HTML 产物链路，不代表真实舆情语义质量验收。

最新结果为预算简报 profile 和安全静态导出；早期本地长报告产物单独保留为历史证据，不能混用大小或 SHA。远端环境与镜像完整记录见 [BETTAFISH_DOCKER_VALIDATION.md](BETTAFISH_DOCKER_VALIDATION.md)。

## 独立环境与代码

- 新建Conda环境 `hr-research-bettafish`，Python3.11.13，不修改主平台环境或系统Python。
- Git只拉取固定提交 `b67928ff1ce515dd7672408221fb1e9500d0972d`，路径为 `.runtime/upstreams/bettafish`；验证要求HEAD完全匹配且已跟踪文件无修改。
- 依赖锁为 `backend/bettafish-requirements.lock`，仅报告引擎实际导入所需依赖，不安装MindSpider、机器学习训练包或完整爬虫依赖。HTML报告可运行；PDF与曲线平滑等可选能力未安装。
- 子进程网络禁用，输入/日志/HTML放在独立任务目录；API密钥、数据库密码与代理配置不进入子进程。Matplotlib/XDG缓存也限定在任务目录。
- 本次运行没有改变生产`BETTAFISH_LICENSE_CONFIRMED`，没有宣称已解决上游适用授权。
- 授权远端镜像使用 Python 3.12.15 的独立 `/opt/bettafish-venv`，`pip check` 通过。容器禁网、UID/GID 10001、只读根目录、移除 capabilities，仅任务目录和缓存 tmpfs 可写。没有切换主应用镜像，24 个既有容器的标识与状态保持一致。

## 真实发现的问题及修复

1. 原Bridge传`LOG_FILE="report.log"`，上游初始化调用`os.makedirs(dirname(...))`时会失败。改为`logs/report.log`，不修改上游源码。
2. 上游章节提示词含完整且带缩进的JSON Schema，首次真实管线观察到26919输入字符，超过平台默认16000。仅对明确的JSON Schema标签和整个JSON输入去除格式缩进，保留全部字段和原文字串，降至13799；没有提升模型预算。
3. 原Markdown模板把总标题和两个二级标题都识别为独立章节，至少要5次模型请求。默认模板保留同样的两部分内容，使用一个章和两个子节，最低3次。独立验证另用5次合成响应跑通多章模式，没有改变应用预算。
4. 增加上游预算预检：在任何模型请求前读取真实模板章节数与Schema/证据必要下限。已知输入必超限、任务/每日剩余额度不足时立即拒绝；每阶段仍经过统一网关，不自动加预算。必要下限不是整个报告的费用或成功保证。
5. 可选镜像首次运行发现验证脚本对 venv 解释器调用 `resolve()`，解引用符号链接后绕过虚拟环境，产生 `ModuleNotFoundError`。验证脚本改为 `expanduser().absolute()`，保留入口；生产 Bridge 原本没有此问题。失败与修复后结果均保存。
6. 上游默认长报告字数和密度要求不适用于平台每次 1200 输出 tokens 的预算。Bridge 采用显式 `budgeted_brief`，每章目标 400 字、上限 500 字，密度门槛适配为 200/133 字；保留完整主题、证据范围和 schema，不增加请求次数、输入或输出预算，不修改固定上游源码。
7. 交付的 HTML 需离开平台后仍不能运行上游脚本。Bridge 另存私有 `upstream-result.html`，将正文和 HTTPS 证据链接导出为含内嵌 CSP 的 `result.html`；移除可执行标签、事件属性、表单和危险链接。静态版不宣称保留互动图表或主题脚本。

## 最新固定版本运行结果

专项镜像：`hr-intelligence-backend:bettafish-eval-20261005`，ID `sha256:6b6839c0f6e91eed44bc664703961203d19f13d7bc5b3516b39af437e6ccd2fc`。固定上游 SHA：`b67928ff1ce515dd7672408221fb1e9500d0972d`。源码 HEAD 和已跟踪文件清洁状态均通过验证。

| 场景 | 实际结果 |
|---|---|
| compact 默认单章、三阶段预算 | 真实 ReportAgent 规划、章节校验、IR 装订和 HTML 渲染成功；3 次合成响应 |
| 三次输入长度 | 4124 / 2672 / 14096 字符，均低于 16000；每次最大输出仍为 1200 tokens |
| 预检必要下限 | 至少 3 次请求、必要输入 11908 字符；不是后续所有调用都成功的保证 |
| 交付安全静态 HTML | 175,457 字节，SHA-256 `bc5be342fa00ae4690c2fa84461c707d53a294915cdb4d4dcb63e27d39130a7b` |
| 私有原始上游 HTML | 2,196,756 字节，SHA-256 `166c44ce2586d9069d11dc2e2c081cdc48a3c148bb9d4728f08d75a186cad2ac` |
| compact 任务 | `7a21399c-cd26-4cc9-b333-51f90aa9fbe4` |
| multi 在默认三阶段预算下 | `bettafish_call_budget`，必要下限 5 次；合成响应 0 次，在任何模型阶段前拒绝 |
| multi 任务 | `bee6f08b-ea87-4a48-ab69-92336d0793ed` |
| 生产配置与真实成本 | license=false、真实 Key 未配置、运行环境日预算 0、Bridge health=`license_pending`；付费模型调用 0 |

SyntheticGateway 使用独立测试预算；这不会激活生产配置。单章产物已经在本机保存副本后重新计算字节数及两个 SHA，与 `validation/summary.json` 一致。

静态导出具体检查：存在内嵌 CSP；没有 script/iframe/object/embed/form/base 标签、事件属性或危险 href；保留正文、HTTPS 证据链接及私有原始产物。原始与静态版本按任务目录保存，不读取“最新文件”。

本机证据根目录：`.runtime/bettafish-docker-validation-20261005/remote-evidence/`。主要产物为 `validation/summary.json`、`validation/output/<上述任务ID>/validation.json`，以及 compact 任务目录中的 `result.html`、`upstream-result.html`、章节与 IR。

最终 149 个允许构建输入文件的聚合 SHA 为 `03a5b54f26305581c2aae314545243eee8284956373a8a22e6671dd05e19fbc6`。该指纹属于可选 BettaFish 专项快照，不能当作之后招聘回调/其他模块更新后的整个平台最终镜像指纹。

## 早期本地运行证据

| 场景 | 结果 |
|---|---|
| 修复前真实源码启动 | 失败，尚未发生模型阶段，证据目录a3d7c39b-2ab4-45b3-9ddc-160ec860ca67 |
| 默认单章＋两子节 | 成功生成2196859字节HTML；3次合成响应，输入3827/2379/13799字符 |
| 多章模式显式允许5次合成响应 | 成功生成2200615字节HTML；5次合成响应，最大输入14353字符 |
| Schema压缩 | 单测验证解析后的JSON完全相同、前后指令及正文空白保持原意 |
| 预算必超限 | 单测确认在第一笔模型请求前拒绝，ModelCall无新增 |

默认报告证据：`.runtime/bettafish-validation/a3614c5a-29c3-4ba3-ba1e-650b9a6d1282/validation.json`；多章证据：`.runtime/bettafish-validation/b3af9fee-9edc-4208-b50d-cf363496090a/validation.json`。每份目录有独立原始请求、worker日志和对应HTML，不读取“最新文件”。

这些早期 HTML 由真实上游的模板、规划节点、章节校验、IR 装订和 HTML 渲染产生，但属于预算简报/静态导出改动之前的原始上游结果。只有模型回复是标明用途的合成数据，不能据此保证真实模型语义质量、把合成内容当成公司舆情，或声称四万字报告已经通过验收。

## 复现命令

以下只用于本机专用Conda环境；不会调用Docker或真实模型：

```sh
conda create -n hr-research-bettafish python=3.11 pip -y
conda run -n hr-research-bettafish python -m pip install -r backend/bettafish-requirements.lock
git init .runtime/upstreams/bettafish
git -C .runtime/upstreams/bettafish fetch --depth=1 https://github.com/666ghj/BettaFish.git b67928ff1ce515dd7672408221fb1e9500d0972d
git -C .runtime/upstreams/bettafish checkout --detach b67928ff1ce515dd7672408221fb1e9500d0972d
conda run -n hr-research-dev python scripts/verify_bettafish_upstream.py --repo .runtime/upstreams/bettafish --python /absolute/path/to/hr-research-bettafish/bin/python
```

已有本项目环境可复用；路径应使用该环境的实际解释器。脚本固定使用合成网关，不提供隐式切换到真实模型的选项。正式接入仍须配置独立环境路径、固定版本及适用授权；不能把测试授权标志写入真实平台配置。
