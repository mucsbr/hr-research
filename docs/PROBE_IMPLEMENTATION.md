# 桌面探针实施与交付记录

更新：2026-10-11。用户已授权实现，明确接受内部自签HTTPS及未签名Win/Mac分发。服务端和三个下载包已经部署；原生安装、真实Chrome及真实账号采集仍未验收，不能用协议模拟通过替代。完整范围保留在 [方案](DESKTOP_PROBE_PLAN.md)，U37按此边界保持incomplete。

## 当前入口及操作

- Web：<http://10.255.200.16:18080/devices>；新增“采集设备”导航。
- 探针HTTPS/WSS：<https://10.255.200.16:18443>，同时提供整个平台页面。
- 三个下载包：Windows x64 NSIS、macOS Intel/Apple Silicon ZIP中的`.app`，均为0.1.0内部未签名包。
- 使用：下载安装 → Web生成十分钟有效的一次性配对配置 → 粘贴到探针并确认业务采集授权 → 已连接 → 绑定账号的采集浏览器并打开登录 → 专用Chrome人工登录 → 确认保存 → Web绑定真实来源并触发任务。
- 既有BOSS备注“招聘信息浏览账号”保留，尚未绑定设备。可以在Web给它选择已配对设备的浏览器，再点击“在设备Chrome登录”；安装包发布不代表账号已登录。探针本身也可创建绑定设备的新登录记录。

普通用户无需Python、Docker、开发依赖、CDP端口或终端入站网络配置。探针连接不依赖当前聊天里的Codex继续在线。安装/退出/重新配对见 [probe/README.md](../probe/README.md)。本机只编辑源码，没有启动测试、Chrome、探针或应用环境。

## 实现范围

服务端新增六张表及迁移`d39a7e6c8102`（父`b25d0c8f3142`）：配对、设备、profile、账号绑定、浏览器会话、命令。一次性配对绑定内部用户；设备凭据独立、服务端仅存摘要，可撤销。命令/结果加密持久化，回执摘要不可覆盖；连接代次、任务执行代次和绑定修订拒绝旧执行写入。

探针主动WSS，连续三次连接失败后切同合同HTTPS长轮询。心跳、暂停、重连、系统凭据存储、命令日志、托盘、用户登录后启动及解除配对已实现。首版同设备浏览器任务串行，人工登录与采集互斥。仅原先waiting_device、尚未投递浏览器命令且没有ModelCall的原任务允许设备恢复后自动继续；已投递而结果未知的命令不盲目重放。

Electron44.7.0、Playwright-core1.63.0、ws8.22.0、TypeScript5.9.3由锁文件固定。秘密通过safeStorage使用Windows DPAPI/macOS Keychain保护。Renderer启用sandbox/contextIsolation，无Node权限；不提供Shell、任意JavaScript、通用文件或其他应用控制。系统Chrome使用专用持久UUID资料目录和仅loopback CDP，复用默认context；不复制日常Chrome资料，只清理任务自有标签页/监听。重用本机Chrome端口前核对browser endpoint GUID。

招聘`probe_browser`通过Playwright形状提供器复用解析器；舆情`ProbeSentimentFetcher`复用同设备浏览器。账号/设备/profile修订冻结入任务快照，内部接口复核。探针模式不另起服务器Chromium、不导出Cookie、不静默换出口。离线加入waiting_device，部分岗位保留，覆盖不足不判下架。确认登录仅标saved、verified=false，首次真实采集核对来源是否可读；尚未实现独立的登录后只读验收结论，不能把人工点击当ready。

Web新增下载/配对/撤销/profile管理、账号绑定和本机Chrome登录向导；来源页默认隐藏归档公司的历史来源，仍可选择查看历史。下载校验manifest路径、平台及SHA256，不匹配拒绝发包。

## TLS和网络

已生成真实内部CA和SAN包含`10.255.200.16`的服务器证书。私钥仅在远端私有`deploy/probe-tls/`；公开CA随配对配置传给客户端，客户端正常校验证书，未使用TLS校验绕过。CA SHA256：`187eb3aabd545e628d05911c83293c0aac5230a17a194fe8bad81a58ceb034e2`。配对信任来自用户在自己的平台获取配置；HTTP获取不能提升为公有CA的身份保证，后续可换公司证书。

`probe-gateway`为可选Compose profile，非root、只读根目录，仅增加HTTPS18443端口，复用`172.16.240.0/24` app网络。原基础+代码维护+搜索三份Compose及其余六网保留，无新生产网段。首次网关遗漏fastcgi/uwsgi/scgi临时目录而重启；补齐`/tmp`路径后healthy。原HTTP18080保留。

## 包与构建证据

构建目录：远端`/home/dabing/hr-probe-implementation-20261011`。只使用独立容器工具链，未在宿主安装Wine。Electron官方归档经授权SOCKS5下载并与官方SHASUMS256核对；临时下载代理仅在QA网络，无宿主端口，构建后停止。

| 包 | 字节 | SHA256 |
|---|---:|---|
| `guanshi-probe-0.1.0-windows-x64.exe` | 113541320 | `fa5ed4d13943088db7719accc504e4b5f0e1009775fa846fd7171b0da356345a` |
| `guanshi-probe-0.1.0-macos-x64.zip` | 134934397 | `1eb4dc73f514420515f383e70c38a796efa4abdc17a9f274f2c5e3396aa41b82` |
| `guanshi-probe-0.1.0-macos-arm64.zip` | 130987336 | `47072f0838b4cc63d94c440ed25defac10cc57d68e8c528f9e2224d4e5a24bf1` |

Windows实际NSIS、PE结构及无Authenticode状态核对通过，日志`windows-disk-package.log`。早期Wine缺失、prefix权限、tmpfs空间失败保留，不算通过。

Mac为Linux交叉打包的真实`.app`。初次electron-builder ZIP跟随framework链接导致膨胀，改由`deploy/package_probe_macos.py`保留symlink和执行权限后重打，CRC及链接/模式检查通过，见`mac-archive-check.log`。无原生macOS启动、公证或Developer ID签名。用户接受未签名内部分发，签名不是本轮阻塞；不保证所有版本Gatekeeper/Keychain体验。

三个打包目录的app.asar均逐文件匹配最终dist的9个编译文件，集合SHA`92016e220a0c5de8f651609c2946fbc8645e68405ddb2c7d4e7d921458b23d33`，见`packaged-source-check.json`。正式HTTPS下载三个包与上述SHA全部一致。包不含设备凭据、配对码或浏览器资料。

## 验证范围

| 层次 | 结果与证据 | 不能替代 |
|---|---|---|
| 后端全量 | `frozen-full-tests.log`：801 passed，433.01s；独立`hr_probe_test`，Mock。首次796/5失败因浏览器缓存路径/旧Celery localhost约束，修正环境后全量重新通过 | 真实模型/网站、原生探针 |
| 协议/权限 | 新`test_probes.py`初轮18项：PG/HTTP/WS、所有权、撤销、幂等/fencing；纳入801 | 实际终端 |
| 相关回归 | `regression-round2.log`：121 passed，50.28s。首轮110/11因Mock预算0，fixture显式12后重跑；APP_ENV=test拒绝真实模型 | 付费模型质量 |
| 两条采集接线 | `probe-collectors.log`：3 passed；真实解析器使用明确浏览器夹具，验证断线partial保留/舆情桥 | 登录后真实采集 |
| Node | 编译及5个协议/日志测试通过，OS秘密存储codec模拟 | DPAPI/Keychain原生行为 |
| 实际通道 | `channel-smoke.log`：实际TLS+WSS及HTTPS轮询注册/命令/回执/确认登录通过，浏览器执行器和OS存储模拟，仅独立QA库 | Win/Mac Chrome、扫码、300012 |
| Web构建 | TypeScript/Vite通过，`web-final-check.log`；已有公共大chunk提示保留 | 原生安装 |
| 正式下载/TLS | `release-verification.json`：正常TLS、三包完整下载SHA一致；两层nginx后无身份WSS正确403，无新设备/profile/任务 | 正式有身份设备连接；协议在QA另验 |
| 正式UI | `ui-verification/summary.json`：设备/账号页、三下载、profile草稿开关及既有BOSS；JS/API错误0、业务写0，1440/1440无溢出。初期脚本中文空格/导航选择器失败已修正，仅改核验脚本 | 配对提交、原生安装/登录 |

真实模型请求0；无批量真实网站请求或外发通知。QA网固定`172.16.247.0/24`，与现场路由/已有网段不重叠。QA API不与会清空QA库的pytest并发运行。

## 正式部署和保全

生产目录：`/home/dabing/hr-research-verification-20261005`，项目`hr-intel-verify-20261005`。Backend镜像`sha256:e280d5f3170b64717acad72b2494b969f857e96d8599a21aa6fcb417f183faae`，Web镜像`sha256:0cf30c5ac5733dc5a5aef021ab8a77fbe755b26659d5a66eb38015c7db8ea159`；核心锁仍为`185e5523b000d271df89bfe2bd38a073fb3c47b2fbc499e2d541c723082f3a6b`。

139个产品输入逐项匹配本机与远端（服务端/Web在生产、探针在构建隔离目录），源清单SHA`8e612f793a9429e9afdd63c0890c87ebffc18e785f0edc6298af131023cbc048`。其中105个后端/迁移/适配/锁输入另与实际API镜像逐文件匹配；生产迁移head为d39a，Alembic check无差异。范围不包含测试/后加核验脚本，不冒充全量测试前后清单。安装包编译集合与Web产物另行留证。

部署前备份配置/Compose/旧页面/数据库，证据在私有`.runtime/probe-deployment-20261011-01/`。旧业务摘要算法未随记录保存，故将备份恢复到独立`hr_probe_predeploy_test`，用同一只读JSONB摘要比较13表，全量一致：公司/主体/来源/账号、岗位/版本/观察、原文/舆情、计划/任务及ModelCall。宇树26岗、智元50岗和4条原模型记录保留，无新QA主体。恢复用测试库检查后删除，备份私有保留。

21个既有容器的ID、镜像、启动时间、状态及网络成员一致，包括正式PG/Redis、Runner/搜索及其他项目；仅替换本项目api/worker/beat/web/migrate、新增网关，见`container-preservation.json`。无prune/down -v、全局代理变更或新192/10容器子网。

## 未完成项

1. Windows/Mac实际安装启动、Chrome发现/profile、DPAPI/Keychain、重启/睡眠、开机启动、撤销/卸载/升级保留身份与资料均未原生验收；无自动更新安装流程。
2. BOSS/猎聘及小红书/抖音/微博真实账号未在探针登录，目前0台正式设备。需真实验证两条业务路径的指定浏览器/出口、验证码、来源可读性/部分覆盖；300012未实测，不保证探针消除网站限制。
3. 登录后的独立有界只读验证、普通用户升级提示、设备离线通知与已有通知冷却整合仍有缺口，UI状态/通道重连不代替这些要求。
4. 未实现MCP服务或透明无限CDP桥；首版采用结构化浏览器命令。模型维护仍走既有服务端Runner/预算，不执行终端候选脚本。

这些缺口保留在U37，不削减范围宣称全部完成。无需用户组织小范围试点或提供安装签名证书才完成本轮工程。
