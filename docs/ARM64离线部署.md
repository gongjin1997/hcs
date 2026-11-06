# ARM64 离线部署

正式环境包，包含后端、前端、PostgreSQL 16、Redis 7 四个 `linux/arm64` 镜像。不带演示租户、Mock 服务、演示数据或租户凭据。

目标机要求：ARM64 Linux、Docker 20.10+、独立 `docker-compose` 命令（1.29+）、Bash。建议 4 核 / 8 GB 内存 / 100 GB 数据盘。目标机不需要 Node、Python、make、jq 或 `docker compose` 插件，也不需要访问互联网。

## 首次安装

交付文件集中在构建机 `/data/hcs/deploy/`，包含已展开的脚本、四个镜像、业务代码包和校验清单。把整个目录复制到内网机器任意传输目录，进入该目录执行：

```bash
bash install.sh --check   # 只校验文件，不启动服务
bash install.sh           # 默认安装到 /data/hcs-inspector 并启动
```

安装入口先校验全部文件和目标架构，再把安装文件放到 `/data/hcs-inspector`，在该目录生成 `.env`、数据目录和日志。目标目录已有文件时拒绝覆盖，已有部署改用原目录的 `bash deploy.sh`。不需要先创建 `/data/hcs-inspector`；当前用户须有目标目录写权限和 Docker 访问权限。

也可以只传输目录中的完整压缩包及其 `.sha256` 文件：

```bash
sha256sum -c hcs-inspector-0.4.0-arm64-offline.tar.gz.sha256
tar xzf hcs-inspector-0.4.0-arm64-offline.tar.gz
cd hcs-inspector-0.4.0-arm64-offline
bash install.sh
```

脚本校验并导入镜像，执行数据库迁移和基础初始化，启动全部服务并等待健康检查。默认端口 80；初始账号 `admin`，随机口令见 `.env` 中的 `INSPECTOR_INIT_ADMIN_PASSWORD`。首次登录后修改口令。

**新生产环境的采集开关、巡检任务均默认关闭**，包括台账同步和数据清理。新租户同样默认关闭采集。登录后确认租户配置，再在系统管理中按需启用；更新已有环境不重置原开关。

新装需要换端口时：`WEB_PORT=8080 bash install.sh`；指定访问地址时：`INSPECTOR_BASE_URL=http://内网IP:8080 WEB_PORT=8080 bash install.sh`。默认部署目录为 `/data/hcs-inspector`；确需换目录可用 `bash install.sh --dir /绝对路径`。若自动识别的访问地址不合适，在部署目录 `.env` 修改 `INSPECTOR_BASE_URL` 后执行 `cd /data/hcs-inspector && bash deploy.sh`。邮件和 HCS 租户在系统管理中配置，内部 CA 放部署目录 `certs/`。

## 已有正式配置

先在原部署目录执行 `bash scripts/backup.sh`，再将新包解压到原目录并执行一键部署：

```bash
tar xzf /path/hcs-inspector-0.4.0-arm64-offline.tar.gz -C /data/hcs-inspector --strip-components=1
cd /data/hcs-inspector
bash deploy.sh
```

把示例路径替换为实际文件和原部署目录。包内默认 Nginx 配置位于 `defaults/`，仅在目标文件不存在时复制，不覆盖已有配置。

- 脚本不会重建或覆盖原 `.env`、数据库口令、加密主密钥、租户、任务和邮件配置。
- `bundle.env` 只提供本包镜像名和版本，不含业务配置或凭据。离线覆盖文件固定目标架构，禁止拉取镜像。
- 首包还包含独立业务代码包，启动时挂载到 `releases/`。`.code.env` 仅记录活动代码目录，不替换原 `.env`；镜像作为固定运行时复用。
- **不要把原数据迁到新目录后丢弃原 `.env`**；主密钥不同将无法解密既有租户凭据。
- 从另一台主机迁移，需要同时迁移数据库和原 `.env`；仅拷贝镜像不会携带数据库里的正式配置。
- 显式配置的 `INSPECTOR_DATABASE_URL` / `INSPECTOR_REDIS_URL` 会沿用。使用外置数据库时按原数据库的备份、恢复方式操作，包内备份脚本只操作内置 PostgreSQL。
- 如仍需补录 HCS/邮件配置，在页面维护；不要启用演示种子或 Mock 地址。

## 后续只更新代码

**已经安装过旧首包且还没有 `update` 入口时**，只上传 `hcs-inspector-code-deploy-upgrade.tar.gz` 小补丁，解压到原部署目录（去掉最外层目录），运行 `bash apply-code.sh`。它不含镜像或真实配置，不需要重传完整首包；此步骤只做一次。

维护人员在构建机生成代码包（目标机不用执行）：

```bash
sh deploy/scripts/code-bundle.sh
```

只上传 `deploy/dist/hcs-inspector-code-<版本>.tar.gz`，在原部署目录执行：

```bash
cd /data/hcs-inspector
bash deploy.sh update /path/hcs-inspector-code-<版本>.tar.gz
```

脚本校验文件和依赖、备份内置数据库、执行迁移、切换代码并检查服务健康。原镜像、`.env`、主密钥、租户和开关保留；不会 `docker pull/build/load`，也不会在宿主机执行 pip/npm。前端构建在构建机完成，代码包中携带构建后的静态文件。

Python 依赖变化时，在构建机用 `sh deploy/scripts/code-bundle.sh --with-wheels`，把 ARM64 wheels 随代码包上传；目标机在原容器内离线安装。不带所需 wheels 的包会在停止服务或迁移之前拒绝，旧版本不变。升级 Python 主版本或系统级库不属于普通代码迭代，需要单独更新基础运行环境。

## 常用命令

```bash
cd /data/hcs-inspector
bash deploy.sh status           # 查看服务状态
bash deploy.sh logs api         # 查看日志
bash deploy.sh stop             # 停止服务，保留数据
bash scripts/backup.sh          # 备份数据与配置
```

重新启动或更新配置后再次执行 `bash deploy.sh`。

若 Redis 退出且日志包含 `ARM64-COW-BUG`，应核对并修复目标机内核；正式配置保留 Redis 的内核保护检查，不默认忽略告警。Redis 官方说明见 [ARM64 内核检查](https://github.com/redis/redis/blob/7.4.11/src/syscheck.c) 和 [官方问题记录](https://github.com/redis/redis/issues/8351)。

## 重新构建（仅构建机联网）

在源码仓库执行：

```bash
bash deploy/scripts/offline-bundle.sh
```

产物位于 `deploy/dist/`。构建机需要 Docker Buildx、Python 3、Node 22.12+ / npm；x86 构建机需先具备 ARM64 binfmt/QEMU 支持。镜像按实际架构、manifest ID 与稳定 config digest 校验后才入包。已有首次运行时镜像时可加 `--reuse-images` 仅重新组合首包。目标机无需这些构建工具；后续迭代应使用小代码包，不再上传完整镜像包。
