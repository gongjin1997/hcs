# HCS-Inspector · HCS 云服务巡检平台（v0.4.0 · 适配 HCS 8.5.1）

运维组内部使用的华为云 Stack（HCS 8.5.1）**只读**运维平台，同时接入多个租户，主要功能：

| 能力 | 说明 |
| --- | --- |
| **巡检告警** | 定时采集 DataArts Studio（实时 / 批作业）、MRS（集群；作业取自 DataArts 作业实例的 MRS 节点）、GaussDB、DCS、ECS（状态；性能需单独配置来源）、CCE 容器事件（重启 / OOM / CrashLoop / 背压 …）→ 40 条内置规则 → 告警去重 / 升级 / 恢复 → 邮件通知；支持维护窗口、静默和平台自身告警。缺失指标不判正常，不参与恢复 |
| **资源台账** | 每小时按租户同步 11 类资源（ECS、CCE、EVS、VPC、子网、EIP、ELB、GaussDB、DCS、MRS、DWS）；负责人 / 业务系统 / 环境 / 核心标记 / 备注由运维维护（**字典约束 + Excel 批量导入**）；新增、变更、释放自动留痕；疑似闲置识别；Excel 导出 |
| **GaussDB 管理** | 原生磁盘与组件、慢 SQL、只读会话、CPU/内存等原生巡检报告及 XLSX；按租户与权限隔离 |
| **DCS 内存** | 按实例查看 5/10/15/30/60 分钟趋势，最近 1/2/7 天或自定义范围；DCS 原生接口实际采样历史、缺失时段、完整 Excel 指标导出 |
| **报表中心** | 日报 / 周报 / 月报 / 台账报表：按租户或「合并」生成，按计划自动发邮件（正文摘要 + Excel 附件），发送时间与收件人在页面配置 |

平台能力：多租户接入（账号密码 IAM Token 或 AK/SK 签名，凭据 AES-256-GCM 加密）、连通性测试、**采集开关**（按资源类型 × 租户启停）与**采集范围**（再细分到 CCE 命名空间 / 某个实例 / 某个工作空间下的作业）、RBAC（28 个权限点 + 租户数据范围）、操作审计、数据保留与自动清理、Prometheus 指标、`make status` 自检。

> 当前版本所有资源（含 ECS）暂时停用 AOM，旧地址、映射和可用记录不参与采集；DCS、GaussDB 使用原生接口，ECS 可选已配置的租户 Prometheus，未接入时保留原生状态。当前局点不使用 CES。不依赖 Elasticsearch / K8s；**生产部署使用单机 Docker 和独立 `docker-compose`**。

## 文档

完整分类索引见 [docs/README.md](docs/README.md)（区分单一来源、当前有效文档与历史记录）。

| 文档 | 内容 |
| --- | --- |
| [docs/架构文档.md](docs/架构文档.md) | 实现版架构：部署视图、进程、模块、数据模型、关键流程、安全、可观测性、扩展点 |
| [docs/开发文档.md](docs/开发文档.md) | 本地环境、目录结构、编码约定、新增资源类型 / 采集器 / 规则 / 接口 / 页面、迁移、测试、发版 |
| [docs/部署运维手册.md](docs/部署运维手册.md) | 环境要求、`.env` 说明、安装 / HTTPS / 白名单 / 证书、监控对接、升级回滚、备份恢复、日常巡检与排障 |
| [docs/联调手册.md](docs/联调手册.md) | 局域网对接真实 HCS：账号权限、租户录入、`make probe` 接口探测、字段映射修正、分批放开采集、验收 |
| [docs/后期开发方案.md](docs/后期开发方案.md) | v0.5 → v1.0 路线图、待办清单（优先级 / 工作量）、风险 |
| [docs/接续开发任务书.md](docs/接续开发任务书.md) | 代码体检结论、优化清单 C01–C22、按顺序执行的任务卡 T01–T21（步骤 + 验收标准），供 Claude Code / 新成员接续开发 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 项目说明（约束、命令、完成定义；`backend/`、`frontend/` 下另有速查）；`.claude/` 为权限配置与自定义命令 |
| [docs/接口清单.md](docs/接口清单.md) | 接口方法 / 路径 / 所需权限点（`make apidoc` 自动生成） |
| [docs/全系统复核_20261102.md](docs/全系统复核_20261102.md) | PDF 契约、监控语义、范围隔离与本轮验收 |
| [docs/全系统检查.md](docs/全系统检查.md) | 本轮检查范围、修复项和测试结果 |
| [docs/DCS内存监控.md](docs/DCS内存监控.md) | 历史来源、聚合口径、Excel 与升级 |
| [docs/AOM停用说明_20261102.md](docs/AOM停用说明_20261102.md) | 所有资源停用 AOM、旧配置与历史处理、验证和部署边界 |
| [docs/资源与指标指南.md](docs/资源与指标指南.md) | 15 类资源（11 类台账 + 作业 / 容器巡检对象）的字段、单位、来源、排障和最佳实践 |
| [docs/平台指标口径.md](docs/平台指标口径.md) | 健康分、告警、MTTA/MTTR、报表、作业及 Prometheus 指标口径 |
| [docs/采集与QPS最佳实践.md](docs/采集与QPS最佳实践.md) | QPS 定义、并发、限流边界、配额与调优 |
| [docs/邮件服务配置.md](docs/邮件服务配置.md) | 页面配置 SMTP / IMAP、端口、加密及测试 |
| [deploy/README.md](deploy/README.md) | 部署目录速查 |
| [docs/ARM64离线部署.md](docs/ARM64离线部署.md) | ARM64 正式环境离线包、一键安装与原配置保留 |
| [docs/首次离线部署验收.md](docs/首次离线部署验收.md) | /data/hcs/deploy 完整交付、默认部署目录与独立 docker-compose 实测 |
| [docs/ARM64离线验收.md](docs/ARM64离线验收.md) | 实际镜像、校验值、模拟联调结果与验证边界 |
| [docs/代码更新与默认开关验收.md](docs/代码更新与默认开关验收.md) | 新环境全部默认关闭，独立 docker-compose 与无需传镜像的代码发布 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

## 快速开始

**独立局域网演示（本轮修复版）**

```bash
make lan          # 构建前端并启动；终端输出局域网地址，默认端口 5173
make lan-status   # 查看访问地址、进程和日志位置
make lan-stop     # 停止本项目进程与容器，保留演示数据
make test-pg      # PostgreSQL 16 全量回归，测试后自动移除临时容器
```

需要 Docker、Python 3.12、Node 22.12+ 及已安装的项目依赖；可用 `python3 scripts/lan.py start --host <本机局域网IP> --port 5173` 指定绑定地址。
局域网模式使用 Mock HCS 与独立演示数据库，演示管理员为 `admin / admin123`。数据库 / Redis / API / Mock / Mailpit 只监听本机，前端监听指定局域网地址。
该模式使用随机密钥和 `.runtime/lan/config.json`（600 权限），通过 `INSPECTOR_ENV_FILE=` 禁用现有 `.env`，避免误连真实云环境。
详细修复范围、迁移和验收记录见 [docs/缺陷修复与局域网验收.md](docs/缺陷修复与局域网验收.md)。

**本地开发（无需 Docker，SQLite + 演示数据）**

```bash
make init          # 创建 venv、安装依赖（按 backend/constraints.txt 锁定版本）、复制 .env
make dev-api       # 终端 1：后端 http://localhost:8000/api/docs（首次启动自动建表 + 写入演示数据）
make dev-web       # 终端 2：前端 http://localhost:5173（/api 代理到 :8000）
make dev-mock      # 可选，终端 3：Mock HCS（:9000，两个租户）；backend/.env 设置 INSPECTOR_HCS_MOCK_URL=http://127.0.0.1:9000 后走真实采集链路
```

演示账号只在非生产 + 演示数据开启时由登录页展示（`INSPECTOR_ENV=prod` 时不会写入，也不会显示）。

**演示环境（Docker）**：`make up` → 控制台 http://localhost:8080 ，邮件收件箱 http://localhost:8025（Mailpit）。

**ARM64 正式环境首次离线部署**：完整交付目录为 `/data/hcs/deploy/`，复制到内网机器后执行 `bash install.sh`，默认安装到 `/data/hcs-inspector`。也可只传完整压缩包及校验文件，解压后执行同一入口。已有部署在原目录执行 `bash deploy.sh`。详见 [ARM64 离线部署](docs/ARM64离线部署.md)。其它单机部署见 [部署运维手册](docs/部署运维手册.md)。

新生产环境采集、巡检默认关闭。旧部署无需重传镜像：上传 `hcs-inspector-code-deploy-upgrade.tar.gz` 小补丁，解压到原目录后运行 `bash apply-code.sh`；以后只上传代码包，执行 `bash deploy.sh update 代码包.tar.gz`。目标机使用 `docker` 和独立 `docker-compose` 即可，不要求宿主 Python、Node 或 make。

## 常用命令（仓库根目录）

| 命令 | 说明 |
| --- | --- |
| `make check` | 提交前检查（CI 同款）：ruff + mypy + vue-tsc + pytest + 前端构建 |
| `make run-once task=<code>` | 手动执行一次任务：`dgc_realtime_status` `dgc_batch_instance` `mrs_cluster_status` `mrs_job_status` `gaussdb_status` `dcs_status` `ecs_status` `cce_pod_events` `ledger_sync` `data_cleanup` |
| `make report kind=<KIND>` | 手动生成上一周期报表：`DAILY` `WEEKLY` `MONTHLY` `LEDGER` |
| `make status [args=--json]` | 平台自检：进程心跳、通知积压、任务健康、平台告警、迁移版本 |
| `make probe args="--tenant tenant-a"` | HCS 接口探测（只读），核对字段映射；对 Mock 加 `--mock-url http://127.0.0.1:9000` |
| `make migrate` / `make revision m="..."` | 数据库迁移 / 生成迁移脚本 |
| `make bootstrap` | 幂等初始化：内置角色、规则、巡检任务；设置 `INSPECTOR_INIT_ADMIN_PASSWORD` 时创建 admin |
| `make apidoc` / `make lock` | 重新生成接口清单 / 依赖锁定文件 |
| `make build` / `make up` / `make down` | 构建镜像 / 启动、停止演示环境 |

## 目录

```
hcs-inspector/
├── backend/            # FastAPI + SQLAlchemy 2 + Alembic；api / scheduler / notifier / migrate 共用一个镜像
│   ├── app/            # api/v1 路由 · core 基础设施 · hcs 客户端 · collectors 采集器 · k8s 容器事件 · metrics 指标源
│   │                   # ledger 台账 · rules 规则引擎 · services 业务服务 · notify 通知 · reports 报表 · workers 进程 · tools 运维工具
│   ├── mock_hcs/       # Mock HCS 8.5.1（两个租户：IAM Token 缓存 / AK/SK；本地合成 Prometheus；AOM 历史协议 fixture 不作为当前来源）
│   ├── migrations/     # Alembic：0001 初始化 · 0002 多租户 / RBAC / 审计 / 容器事件 · 0003 静默 / 采集开关 / 系统设置 / 平台看护
│   └── tests/          # pytest
├── frontend/           # Vue 3 + TypeScript + Vite + Element Plus（按需引入）+ Pinia + ECharts
├── deploy/             # 生产部署目录：compose（生产 / https / demo / build）、nginx、scripts、Makefile、.env.example
├── docs/               # 架构 / 开发 / 部署运维 / 联调 / 后期开发方案 / 接口清单
├── .claude/            # Claude Code：settings.json（权限 / 机密保护）+ commands/（/task、/check、/new-api、/probe-review、/release）
├── CLAUDE.md           # Claude Code 项目说明（backend/、frontend/ 下另有 CLAUDE.md 速查）
├── .gitlab-ci.yml      # CI：check → build（镜像）→ package（离线包）
└── Makefile
```

## 约定（摘要，详见开发文档）

- 接口统一返回 `{"code": 0, "message": "ok", "data": ...}`；错误码 `40001` 参数 · `40101` 未登录 · `40301` 无权限 · `40401` 不存在 · `40901` 冲突 · `50001` 内部 · `50201` HCS 上游异常。
- 权限：后端 `require_perm("alarm:handle")` + `get_scope` 数据范围；前端路由 `meta.perm` + `v-perm` 指令；`tests/test_apidoc.py` 保证所有写接口都声明了权限点。
- 平台对 HCS **只读**；写操作全部写 `audit_log`（密码、SK 不入审计）；时间 UTC 存储、ISO 8601 传输；凭据加密存储、页面与日志脱敏。
