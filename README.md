# HCS-Inspector · HCS 云服务巡检平台（v0.3.0 · 适配 HCS 8.5.1）

运维组内部使用的华为云 Stack（HCS 8.5.1）**只读**运维平台，同时接入多个租户（当前两个），三块核心能力：

| 能力 | 说明 |
| --- | --- |
| **巡检告警** | 定时采集 DataArts Studio（实时 / 批作业）、MRS（集群 / 作业）、GaussDB、DCS、**ECS（运行状态 + CPU / 内存 / 磁盘）**、**CCE 容器事件（重启 / OOM / CrashLoop / 背压 …）** → 规则判定（35 条内置规则）→ 告警去重 / 升级 / 恢复 → 邮件通知（含手机端一键认领、未认领按级别升级） |
| **资源台账** | 每小时按租户从 HCS 同步 14 类资源（ECS、CCE、EVS、VPC、子网、EIP、ELB、RDS、GaussDB、DCS、Kafka、MRS、DWS、CSS）；负责人 / 业务系统 / 环境 / 核心标记 / 备注由运维维护；新增、变更、释放、ECS 状态变化自动留痕；疑似闲置识别；Excel 导出 |
| **报表中心** | 日报 / 周报 / 月报 / 台账报表：按租户或「合并」生成，按计划自动发邮件（正文摘要 + Excel 附件，支持租户收件人），也可手动生成、导出、补发 |

平台能力：**多租户接入**（每个租户独立选择「账号密码（IAM Token）」或「AK/SK（SDK-HMAC-SHA256 签名）」，凭据 AES-256-GCM 加密入库）、**连通性测试与能力探测**、**RBAC 权限 + 租户数据范围**、**操作审计**。

> 指标来源按租户自动探测：**CES 优先** → 不可用时用 **Prometheus（可选）** → 都没有则降级为仅状态巡检（ECS 状态、容器事件不受影响）。
> 不需要 Elasticsearch；ManageOne 北向告警、CTS 审计事件为后续可选接入。平台自身健康看 `/healthz` 与结构化日志。
> 本仓库是**可运行的脚手架**：前后端分层、HCS 客户端（Token / AK/SK）、规则引擎、台账同步、容器事件采集、报表、通知 Outbox、Mock HCS（两个租户）、演示数据与测试均已就绪。

## 账号与权限

账号由管理员在「系统管理 → 用户管理」开通（用户名 + 密码登录，不对接 SSO）。4 个内置角色 + 自定义角色，权限点 22 个（8 个模块），另按租户限定数据范围：

| 内置角色 | 能做什么 |
| --- | --- |
| 平台管理员 `admin` | 全部功能，包括租户接入（AK/SK）、用户与角色管理 |
| 运维负责人 `ops_lead` | 全部业务功能（含规则修改、背压探针配置、报表删除、操作审计），不含租户管理、用户与角色管理 |
| 值班运维 `oncall` | 查看全部 + 认领 / 关闭告警、维护台账（含同步 / 导出）、生成 / 发送报表、立即执行巡检任务 |
| 只读访客 `viewer` | 只能查看（总览 / 大屏、告警、容器事件、台账、报表、规则、通知、任务），不能做任何修改 |

- **数据范围**：用户可限定为部分租户；顶部「租户切换」通过请求头 `X-Tenant-Id` 传给后端，后端与用户范围取交集，越权返回 403。
- 登录：用户名 + 密码，可勾选「7 天内免登录」（默认 12 小时）；连续输错 5 次锁定 10 分钟；忘记密码由管理员重置。
- 演示账号（仅本地演示数据，`INSPECTOR_ENV=prod` 时不会写入）：`admin`、`zhaolei`（运维负责人）、`oncall`（值班 · 全部租户）、`lina` / `wangfang`（值班 · 仅租户A / 仅租户B）、`viewer`（只读）；初始口令见 `backend/app/services/seed.py`。

## 目录

```
hcs-inspector/
├── backend/            # FastAPI + SQLAlchemy 2 + Alembic（API / 调度 / 通知 共用一套代码，按启动命令区分进程）
│   ├── app/
│   │   ├── api/v1/     # 路由：auth / dashboard / alarms / resources(台账) / pod_events / reports / rules / notify / tenants / users / system / stream(SSE)
│   │   ├── core/       # 配置、数据库、统一响应、JWT、权限点（permissions）、登录与数据范围依赖（deps）、审计、日志、加密、事件总线
│   │   ├── hcs/        # HCS 客户端：IAM Token（提前 1 小时刷新）、AK/SK 签名（signer）、Endpoint 解析、限流、重试、分页
│   │   ├── collectors/ # 巡检采集器：DataArts / MRS / GaussDB / DCS / ECS
│   │   ├── k8s/        # CCE 容器事件：K8s API（pods / events）采集、事件分类（10 类）、背压探针
│   │   ├── metrics/    # 指标源：CES / Prometheus（可选），按租户自动探测与降级
│   │   ├── ledger/     # 资源台账：各服务资源拉取（fetchers）、同步与变更比对（sync）、Excel 导出
│   │   ├── reports/    # 报表：统计构建（builder，按租户 / 合并）、Excel、生成 / 发送服务
│   │   ├── rules/      # 规则引擎：安全表达式、阈值 / 状态规则、状态机判定、默认规则 YAML
│   │   ├── alarms/     # 告警指纹
│   │   ├── services/   # 巡检流水线、告警、总览、租户（tenancy / tenant_check）、资源目录、初始化、演示数据
│   │   ├── notify/     # 通知：路由（按租户匹配）、未认领升级（escalation）、渲染、SMTP、Outbox 投递
│   │   ├── models/ schemas/ templates/ workers/
│   ├── mock_hcs/       # 本地联调用 Mock HCS：两个租户（账号密码 + CES / AK/SK + Prometheus），含 IAM catalog、CCE K8s API
│   ├── migrations/     # Alembic 迁移：0001 init、0002 tenants_rbac_audit_pod_events
│   └── tests/          # 61 个用例
├── frontend/           # Vue 3 + TypeScript + Vite + Element Plus + Pinia + ECharts
│   └── src/{api,components,composables,config,directives,layouts,router,stores,styles,types,utils,views}
│       └── views/{dashboard,alarms,ledger,pods,reports,rules,notify,system,bigscreen,login,mobile,misc}
├── deploy/             # docker-compose.yml、nginx.conf、k8s/hcs-inspector.yaml（K8s 部署参考）
├── .gitlab-ci.yml      # CI 参考流水线：check → build → deploy
└── Makefile
```

## 快速开始

**方式一：本地开发（无需 Docker，SQLite + 内置演示数据）**

```bash
make init          # 创建 venv、安装依赖、复制 .env
make dev-api       # 终端 1：后端 http://localhost:8000/api/docs
make dev-web       # 终端 2：前端 http://localhost:5173
make dev-mock      # 可选，终端 3：Mock HCS（:9000）；设置 INSPECTOR_HCS_MOCK_URL=http://127.0.0.1:9000 后，调度 / 手动任务走真实采集链路
                   # （演示数据由进程内 Mock 生成，不依赖该进程）
make run-once task=ecs_status                 # 执行一次巡检（默认全部启用租户；指定租户：task="ecs_status --tenant tenant-a"）
make ledger-sync                              # 同步一次资源台账
make report kind=DAILY                        # 生成上一周期日报（DAILY / WEEKLY / MONTHLY / LEDGER）
cd backend && .venv/bin/python -m app.workers.run_once check --tenant tenant-b   # 租户连通性 / 能力探测
```

**方式二：一键完整环境（Docker Compose）**

```bash
make up            # 控制台 http://localhost:8080 ，邮件查看 http://localhost:8025（Mailpit）
```

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `make lint` | ruff + mypy + vue-tsc |
| `make test` | pytest（规则、流水线、台账、报表、登录、RBAC、租户、容器事件、升级、迁移、接口） |
| `make migrate` / `make revision m="..."` | 数据库迁移 |
| `make bootstrap` | 生产初始化（幂等）：内置角色、规则、巡检任务、台账同步任务；首次部署设置 `INSPECTOR_INIT_ADMIN_PASSWORD` 创建 admin |
| `make run-once task=<code>` | 手动执行一次任务：`dgc_realtime_status` `dgc_batch_instance` `mrs_cluster_status` `mrs_job_status` `gaussdb_status` `dcs_status` `ecs_status` `cce_pod_events` `ledger_sync` |
| `make report kind=<KIND>` | 手动生成报表：`DAILY` `WEEKLY` `MONTHLY` `LEDGER`（`run_once report <KIND> --tenant <code>` 生成单租户报表） |
| `make scheduler` / `make notifier` | 启动调度进程（巡检 + 台账同步 + 定时报表）/ 通知进程（Outbox 投递 + 每分钟未认领升级扫描） |

## 关键配置（环境变量，前缀 `INSPECTOR_`，完整见 `backend/.env.example`）

| 变量 | 说明 |
| --- | --- |
| —— | **HCS 租户接入在页面维护**：「系统管理 → 租户接入」填写 IAM 地址、Domain、项目、认证方式（账号密码 / AK/SK）、指标来源（自动 / CES / Prometheus / 无）；保存前可「连通性测试」 |
| `MASTER_KEY` | 租户凭据（`hcs_account.secret_cipher`，AES-256-GCM）的主密钥，`openssl rand -base64 32` 生成并备份 |
| `HCS_ENDPOINTS` / `HCS_REGION` / `HCS_EXTERNAL_DOMAIN` | 各服务 Endpoint 全局模板（租户可单独覆盖） |
| `HCS_IAM_URL` / `HCS_USERNAME` / `HCS_PASSWORD` / `HCS_DOMAIN` / `HCS_PROJECT_ID` | 兜底单账号（库里一个租户都没有时使用），通过 Secret 注入 |
| `HCS_MOCK_URL` | 仅开发联调：全部租户指向 Mock HCS（prod 忽略） |
| `POD_EVENT_RETENTION_DAYS` / `POD_BACKPRESSURE_PATTERN` / `POD_BACKPRESSURE_RECOVER_MIN` | 容器事件保留天数（默认 30）、背压关键字正则、背压结束判定分钟数 |
| `JWT_SECRET` / `JWT_EXPIRE_MINUTES` / `JWT_REMEMBER_DAYS` | 登录令牌；默认 12 小时，勾选免登录 7 天 |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCK_MINUTES` | 连续输错锁定策略，默认 5 次 / 10 分钟 |
| `REPORT_CRON` / `REPORT_MAIL_TO` | 定时报表（默认日报每天 08:30、周报周一 09:00、月报 1 日 09:00）与默认收件人；租户可另配收件人 |
| `SMTP_*` | 邮件发送 |

## 生产部署要点

- 演示数据只用于开发 / 演示：`INSPECTOR_ENV=prod` 时 `INSPECTOR_DEMO_SEED` 会被忽略。
- 发版顺序：`alembic upgrade head && python -m app.workers.bootstrap`（K8s 中为 `inspector-migrate` Job）→ 滚动更新 api / scheduler / notifier / web。
- 首次部署在 Secret 中设置 `INSPECTOR_INIT_ADMIN_PASSWORD` 创建 admin，完成后删除该变量并修改口令。
- HCS 账号 / AK 只授予**只读**权限（各服务 ReadOnly + CES 读 + CCE 集群只读）；平台不做任何变更操作，闲置资源释放仍在 HCS 控制台完成。
- 确认租户是否有 CES：ManageOne 运营面服务列表是否有「云监控」；或 IAM `GET /v3/auth/catalog` 中是否有 `type=metric / ces`；或直接在「租户接入」点「连通性测试」（自动探测）。

## 约定

- 接口统一返回 `{"code": 0, "message": "ok", "data": ...}`；错误码：`40001` 参数错误、`40101` 未登录、`40301` 无权限 / 越权、`40401` 不存在、`40901` 状态冲突、`50001` 内部错误、`50201` HCS 上游异常。
- 权限：后端 `require_perm("alarm:handle")` 依赖 + `get_scope` 数据范围；前端路由 `meta.perm` + `v-perm` 指令（`v-admin` 为兼容写法）。
- 写操作（登录、告警处置、台账维护 / 同步 / 导出、容器事件采集 / 探针、报表、规则、任务执行、租户 / 用户 / 角色）写入 `audit_log`，敏感字段（密码、SK）不入审计。
- 台账字段分两类：**同步字段**（来自 HCS，只读）与**管理字段**（负责人 / 业务系统 / 环境 / 核心 / 备注，人工维护）；任何变化写入 `resource_change`。
- 所有时间以 UTC 存储、ISO 8601 传输，前端按浏览器时区展示；报表周期按 `INSPECTOR_TIMEZONE`（默认 Asia/Shanghai）切分。
- HCS 凭据加密存储，页面与日志脱敏。
