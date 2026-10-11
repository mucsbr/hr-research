# 当前使用实例

- 地址：<http://10.255.200.16:18080>
- 管理员用户名：`admin`
- 初始密码已在本次私人对话中提供，也保存在服务器本项目的私有`deploy/server.env`。不在仓库文档中记录密码。
- SSH：`dabing@10.255.200.16`
- 项目目录：`/home/dabing/hr-research-verification-20261005`
- Compose项目：`hr-intel-verify-20261005`，使用基础、代码维护、搜索三份Compose文件。

2026-10-11 已恢复远端服务，容器网络固定为 `172.16.240.0/24` 至 `172.16.246.0/24` 七个小子网；服务器访问地址不变。不得重新使用自动分配的旧 `192.168.*` 控制网络。见 [网络约定](NETWORKING.md) 与 [恢复验证](../docs/NETWORK_RECOVERY_20261011.md)。

用户已改为仅使用远端；本机只保留源码和文档。此前本机运行记录、数据库和审计材料迁移到远端项目的`retired-local-20261005/`私有目录，模型调用记录不清零。

当前后端镜像复用已验证的BettaFish独立运行环境，覆盖最新应用代码；迁移前已核对核心依赖锁一致。`Dockerfile.remote-runtime`用于此次迁移，后续依赖变化时应重新构建并验证标准核心及BettaFish镜像，不能绕过其中的锁一致检查。网页版来自已验证的前端构建产物。

## 桌面探针（2026-10-11）

- 下载与设备管理：<http://10.255.200.16:18080/devices>。
- 探针连接入口：<https://10.255.200.16:18443>，内部CA；配对配置携带公开CA，探针正常验证TLS。
- 三个内部0.1.0未签名包已经发布。用户接受未签名分发；原生安装和真实账号采集尚未验收，见 [记录](../docs/PROBE_IMPLEMENTATION.md) 与 [安装说明](../probe/README.md)。
- 正式维护继续使用三份Compose，并启用可选profile：

```sh
cd /home/dabing/hr-research-verification-20261005
docker compose --env-file deploy/server.env \
  -f compose.yaml -f compose.code-maintenance.yaml -f compose.search.yaml \
  --profile probe up -d --no-build --pull never
```

运行前核对已验证镜像、私有env、证书和包已存在，不能用未构建镜像执行此命令。私有env设置PROBE_PUBLIC_URL、PROBE_CA_FILE、PROBE_TLS_PORT；证书私钥只在服务器`deploy/probe-tls/`，禁止提交。包和manifest保存在原app-data卷的`/data/probe-downloads`，公开CA在`/data/probe-trust/ca.pem`。网关复用现有app网络，没有新增生产子网。

证书生成脚本`deploy/init_probe_tls.py`仅在授权远端本项目环境运行，拒绝覆写已有密钥。当前已有有效证书，不重复生成；替换/续期需保持已配对设备的信任关系或明确重新配对。
