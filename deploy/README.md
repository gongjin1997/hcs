# HCS-Inspector 部署目录（单机 docker compose）

本目录就是生产部署所需的全部文件：拷到目标机（例如 `/opt/hcs-inspector`），在本目录执行 `make` 命令完成安装、升级、备份、排障。
完整步骤见 `docs/部署运维手册.md`，局域网对接真实 HCS 见 `docs/联调手册.md`。

## 环境要求

| 项 | 要求 |
| --- | --- |
| 主机 | 1 台 Linux 虚拟机，4 核 8 GB 起，数据盘 100 GB（`DATA_DIR` 所在分区） |
| 软件 | Docker 20.10+、docker compose v2 插件（`docker compose version`）、GNU make、openssl |
| 网络 | 本机 → 各租户 IAM / 服务 Endpoint（HTTPS 443）、CCE 集群 API、Prometheus（如有）、SMTP；运维办公网 → 本机 80 / 443 |
| 时间 | 开启 NTP 同步（AK/SK 签名和 Token 过期判断依赖本机时钟） |

## 三步安装

```bash
make gen-env        # 1. 由 .env.example 生成 .env（随机生成数据库口令、JWT / 邮件链接密钥、凭据主密钥、admin 初始口令）
vi .env             # 2. 修改「必改」项：INSPECTOR_BASE_URL、SMTP_*；HCS 内部 CA 放 certs/ 并配置 INSPECTOR_HCS_CA_FILE
make install        # 3. 导入 / 构建镜像 → 迁移 + 初始化 → 启动 → 健康检查（可重复执行）
```

安装完成后用 `admin` 和 `.env` 中的 `INSPECTOR_INIT_ADMIN_PASSWORD` 登录，**立即修改口令并清空该行**；然后在「系统管理 → 租户接入」录入租户，
点「连通性测试」，再执行 `make probe TENANT=<租户编码>` 做接口字段核对。

> `.env` 含全部密钥（权限 600）。`INSPECTOR_MASTER_KEY` 一旦丢失，已保存的租户凭据无法解密，**务必离线备份**；`make backup` 的备份包里也带有 `.env`。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `make status` | 容器健康 + 平台自检（调度 / 通知进程心跳、通知积压、任务连续失败、平台告警、迁移版本），退出码 0 = 正常 |
| `make logs S=api` | 查看日志（`api` `scheduler` `notifier` `web` `postgres` `redis` `migrate`） |
| `make restart [S=notifier]` | 重启应用进程（修改 `.env` 后用 `make up` 让容器按新配置重建） |
| `make upgrade V=0.4.1` | 升级：备份 → 新版本先迁移数据库 → 替换进程 → 健康检查；迁移失败自动保持旧版本 |
| `make rollback V=0.4.0 F=backups/xxx.tar.gz` | 回退到升级前：改回旧版本号 → 用升级前备份恢复数据库 → 启动旧版本 |
| `make backup` | 备份数据库 + `.env` + nginx / 证书配置到 `backups/`，保留 `BACKUP_KEEP` 份；建议 crontab 每天执行 |
| `make restore F=backups/xxx.tar.gz [WITH_ENV=1]` | 从备份恢复（覆盖当前数据库，需输入 yes 确认） |
| `make probe TENANT=tenant-a [ONLY=ecs,dcs]` | HCS 接口探测（只读），脱敏报告拷到 `exports/` |
| `make reload-web` | 修改 `nginx/` 下配置（访问白名单、证书）后热加载 |
| `make help` | 全部命令 |

## 目录说明

```
deploy/
├── docker-compose.yml          # 生产：postgres / redis / migrate（一次性）/ api / scheduler / notifier / web
├── docker-compose.https.yml    # 叠加：HTTPS（证书放 nginx/certs/server.crt、server.key）
├── docker-compose.demo.yml     # 叠加：演示 / Mock 联调（Mock HCS + Mailpit + 演示数据），不要用于生产
├── docker-compose.build.yml    # 叠加：从源码构建镜像（仓库内使用）
├── .env.example                # 配置模板（COMPOSE_FILE 决定叠加哪些文件）
├── Makefile                    # 运维命令入口
├── nginx/                      # http.conf / https.conf、snippets/（安全头、访问白名单 allowlist.conf、应用路由）
├── certs/                      # 后端容器 /app/certs：HCS 内部 CA、SMTP CA
├── scripts/                    # gen-env / install / upgrade / rollback / backup / restore / status / offline-bundle
├── data/                       # 运行后生成：PostgreSQL、Redis 数据（DATA_DIR）
├── backups/                    # 运行后生成：备份包（BACKUP_DIR）
└── exports/                    # 运行后生成：make probe 导出的探测报告
```

## 离线局点

在能访问外网的构建机（源码仓库 `deploy/` 目录）执行 `make bundle`，生成 `dist/hcs-inspector-<版本>-offline.tar.gz`
（镜像 tar + 本目录 + 文档）。拷到目标机解压，进入目录后同样执行 `make gen-env && vi .env && make install`；
`install` / `upgrade` 会自动校验 `images/SHA256SUMS` 并 `docker load`。
