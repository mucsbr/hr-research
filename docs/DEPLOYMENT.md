# Docker 部署与隔离验证

Docker 是服务器交付方式。本机开发和 Python 测试仍使用 `hr-research-dev` Conda 环境，不在没有 Docker 的本机调用、安装或启动 Docker。本文件中的 `docker` 命令仅在具备 Docker 的部署服务器执行。

## 交付组成

`compose.yaml` 启动 PostgreSQL 16、Redis 8、数据库迁移、FastAPI、Celery Worker、单实例 Beat 和 Nginx/React。Python 依赖使用 `backend/requirements.lock`，前端使用 `frontend/package-lock.json`。后端镜像安装 Playwright 对应的完整 Chromium，运行非 root 用户，API 与 Worker 共享加密浏览器会话及报告文件卷。

只有 Web 服务发布端口，默认 `127.0.0.1:18080`。PostgreSQL、Redis、API、浏览器内部调用没有宿主机端口。Nginx 仅反代 `/api/v1/`，显式拒绝 `/api/internal/`；内部浏览器调用仍要求 Worker Token。用户登录视图经受认证保护的 API 返回画面与接收操作，不发布 CDP/VNC 端口。

API 暂时固定一个 Uvicorn 进程，因为活动浏览器登录会话保存在该进程的 LoginManager 中。不要增加 `--workers`、横向扩容 API 或启动多个 Beat，直到相应会话归属和调度去重机制完成适配。Worker 默认 3 个线程。

Worker 监听 `recruitment,analysis,sentiment,news,notifications,maintenance,celery`。仅启动招聘队列会导致舆情、资讯、通知或维护任务持续排队；验收时应检查完整队列列表。

容器根文件系统只读，但 Chromium 的 `XDG_CONFIG_HOME` 与 `XDG_CACHE_HOME` 位于 `/tmp` 的 tmpfs 中；不能将它们改回只读 home 目录，否则 Chromium crashpad 会启动失败。长期登录信息由应用加密后保存在 `/data/browser-profiles`，不依赖这些临时目录。

## 首次部署

将代码复制至新目录，排除本地 `.env`、`.runtime`、`.git`、浏览器 Profile、数据库目录和 `node_modules`。可以在本机使用下列只上传允许目录的脚本；该脚本不执行 Docker，也不删除远端文件：

```sh
ssh -o BatchMode=yes user@server 'mkdir -p ~/hr-intelligence && chmod 700 ~/hr-intelligence'
sh scripts/remote_sync.sh user@server hr-intelligence
```

随后在服务器的项目目录执行：

```sh
python3 deploy/init_env.py
docker compose --env-file deploy/server.env config --quiet
docker compose --env-file deploy/server.env build api web
docker compose --env-file deploy/server.env up -d
docker compose --env-file deploy/server.env ps -a
```

`deploy/init_env.py` 只使用 Python 标准库，生成一份独立、权限 `0600` 的 `deploy/server.env`；已有文件时拒绝覆盖。它不会读取本机开发配置，也不会输出密钥。管理员账号默认为 `admin`，初始密码保存在该私有环境文件里。首次启动会在数据库中创建管理员；后续改环境变量不会自动修改已创建账户的密码。

默认 `LLM_API_KEY` 为空且 `LLM_DAILY_REQUEST_LIMIT=0`。部署验证不需要模型。需要使用模型时，再由管理员填写真实网关、密钥与明确预算，重新创建 API/Worker/Beat。不得把模型密钥放入镜像构建参数、前端、测试脚本或版本库。

邮件配置通过 `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM/SECURITY` 传入，默认不配置；仅支持 STARTTLS 或 TLS 直连。`NOTIFICATION_ALLOWED_HOSTS` 仅供管理员按精确主机授权内部业务接收器，默认空，不开放本机管理或元数据地址。

官网检查点通过 `CHECKPOINT_MAX_AGE_SECONDS`、`CHECKPOINT_MAX_RAW_BYTES`、`CHECKPOINT_MAX_STEPS`、`CHECKPOINT_MAX_RECORDS` 配置，默认分别为3600秒、30000000字节、600步、500岗位。缩小上限后不满足条件的旧检查点将拒绝接续，可从头采集；不要通过扩大时效把陈旧结果当成新鲜完整基线。

BettaFish Bridge 默认不激活。Compose 映射 `BETTAFISH_REPO_DIR`、`BETTAFISH_PYTHON`、`BETTAFISH_REVISION`、`BETTAFISH_LICENSE_CONFIRMED`、`BETTAFISH_TIMEOUT_SECONDS`。普通core镜像没有上游依赖，状态为 `not_configured`；仓库和独立Python必须是容器内实际路径，不能填写宿主机Conda路径。仅填写旧 `BETTAFISH_URL` 不能激活当前ReportEngine Bridge。

### 可选 BettaFish 镜像

`backend/Dockerfile.bettafish` 在core后端镜像上增加固定提交源码、git与独立venv，依赖由 `backend/bettafish-requirements.lock` 锁定。先构建core，再按同一私有配置构建和启动覆盖文件：

```sh
docker compose --env-file deploy/server.env build api web
docker compose --env-file deploy/server.env -f compose.yaml -f compose.bettafish.yaml build api
docker compose --env-file deploy/server.env -f compose.yaml -f compose.bettafish.yaml up -d
```

四个后端服务共用可选镜像，原有环境、只读根目录、非root、卷和队列继承core配置。后续更新、迁移和重建也须带相同两个 `-f` 参数；仅回退core覆盖文件不应删除数据卷。

覆盖文件不会提高模型预算，也不会确认许可；默认应显示 `license_pending`。管理员需核实实际适用许可并配置模型后再启用业务报告。默认报告为有证据引用的预算简报，预检在确定预算不足时先拒绝；公开下载使用静态化HTML，原始上游HTML仅私有留存。已执行的是独立可选镜像的离线合成验证，不能将其等同于真实模型内容质量或主栈已切换该镜像；详见 [BettaFish容器验证](BETTAFISH_DOCKER_VALIDATION.md)。

首次可通过 SSH 隧道访问，无需开放服务器入站端口：

```sh
ssh -N -L 18080:127.0.0.1:18080 user@server
```

浏览器访问 `http://127.0.0.1:18080`。正式内部域名部署时，将 `APP_URL` 设置为实际访问地址；在受管理的 TLS 反向代理后启用 `COOKIE_SECURE=true`。如果改变端口或域名，`APP_URL` 必须同步修改，否则浏览器写请求会被 Origin 校验拒绝。只在确实需要并已配置网络访问控制时修改 `BIND_ADDRESS`。

## 网络与大陆环境

镜像地址、PyPI、npm registry 和 Playwright 下载主机均可通过 Compose 构建变量选择，例如 `PYTHON_IMAGE`、`POSTGRES_IMAGE`、`REDIS_IMAGE`、`NODE_IMAGE`、`NGINX_IMAGE`、`PIP_INDEX_URL`、`NPM_REGISTRY`、`PLAYWRIGHT_DOWNLOAD_HOST`。选择镜像源后仍需保持版本一致，避免为解决下载问题取消依赖锁。

用户提供的 `socks5://10.255.200.17:50001` 是 SOCKS5 服务，不能直接当作 HTTP 代理传给不支持 SOCKS 的 Docker daemon、apt 或 Playwright。必要时使用支持 SOCKS 的下载工具（例如 `curl --proxy socks5h://10.255.200.17:50001`），或只在该项目的构建环境中临时转换代理。不得修改共享服务器的全局 Docker 代理或重启 daemon 影响现有容器。应用默认不继承构建时代理配置。

## 更新与持久化

持久卷为 PostgreSQL 数据、Redis 队列、应用文件（报告、加密浏览器会话、Beat 调度状态）。升级前备份数据库和应用文件卷，并保留对应的 `ENCRYPTION_KEY`；换密钥会使旧浏览器会话不可解密。不要使用 `down -v` 作为常规停止或升级命令。

在相同项目目录、相同 Compose 项目名及相同私有环境文件下更新：

```sh
docker compose --env-file deploy/server.env stop worker beat
docker compose --env-file deploy/server.env build api web
docker compose --env-file deploy/server.env run --rm migrate
docker compose --env-file deploy/server.env up -d
```

若升级迁移与旧 API 不兼容，先安排维护窗口停止 `api web`。迁移失败时保留日志，不能继续把新版本投入使用。镜像回滚不等同于数据库回滚；需要先确认迁移兼容性。

只停止该项目可执行 `docker compose --env-file deploy/server.env stop`。不要清理共享主机的镜像、容器、网络或 volume，也不要对无关 Compose 项目执行任何命令。

## 有界部署验证

运行以下脚本需要模型保持禁用，脚本只访问本地平台入口。它验证前端与 SPA 深链接、数据库就绪、登录与注销、Origin 保护、内部浏览器接口不可通过前端访问、公司/来源写入，以及 Beat → Worker → 未登录等待状态。它不会创建真实竞品，不访问外部招聘网站，不调用 LLM。

```sh
python3 deploy/verify_stack.py --env-file deploy/server.env
python3 deploy/verify_business.py --env-file deploy/server.env
docker compose --env-file deploy/server.env exec -T api python - < deploy/verify_browser.py
docker compose --env-file deploy/server.env exec -T api python - < deploy/verify_frontend.py
docker compose --env-file deploy/server.env exec -T api python - --authenticated < deploy/verify_frontend.py
docker compose --env-file deploy/server.env exec -T worker python - < deploy/verify_internal.py
docker compose --env-file deploy/server.env exec -T api python -m alembic -c /app/backend/alembic.ini check
docker compose --env-file deploy/server.env exec -T worker python -m celery -A app.queue:celery inspect ping --timeout 5
```

脚本结果写入 `deploy/verification-results.json`（不含密码或登录 token）。浏览器运行、持久化重启、网络端口与其他业务容器未受影响需要另外验证；不能只用健康检查代替完整部署验证。

`verify_browser.py` 仅在当前 Python 进程中启用测试入口，打开容器 loopback fixture，验证完整 Chromium 的画面、点击、输入、会话加密保存。它不更改运行中 API 的配置，不登录真实平台。`verify_internal.py` 从 Worker 调用 API 的内部接口，验证无 token 拒绝、正确 token 可达；不存在的 fixture ID 会在读取任务时返回 404，不触发浏览器或外部网络。

`verify_business.py` 为完整业务模块建立显式 fixture，验证对话查询、未知招聘覆盖、无模型跨公司比较、舆情导入和收藏、未登录等待、BettaFish 配置阻塞、资讯/通知私网拒绝以及无已知上游的维护状态。资讯与通知 fixture 的 127.0.0.1 目的地址必须在连接前被拒绝，不会访问真实站点或发送真实通知。脚本还验证普通账号不能管理用户、维护或创建通知通道，并在测试后停用该临时账号。它不代表真实平台登录或生产 SMTP 已验收。

只在本项目的独立验证环境中，可运行以下持久化检查；它会停掉并重建该 Compose 项目的容器和网络，但保留命名卷：

```sh
docker compose --env-file deploy/server.env exec -T api python - record < deploy/verify_persistence.py
docker compose --env-file deploy/server.env down
docker compose --env-file deploy/server.env up -d --wait --wait-timeout 120
docker compose --env-file deploy/server.env exec -T api python - check < deploy/verify_persistence.py
```

`record_snapshot.py` 记录业务源码、迁移、依赖锁和部署输入的 SHA-256，不读取私有环境文件。每次重新同步业务代码后，在构建前重新记录，避免用旧版本的通过记录描述新代码。

实际执行的节点、命令、镜像与源文件版本、通过项和限制见 [DEPLOYMENT_VALIDATION.md](DEPLOYMENT_VALIDATION.md)。功能仍在并行开发，部署基线通过不代表后续新增模块已经在同一镜像中验证。
