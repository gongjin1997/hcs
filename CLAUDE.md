# CLAUDE.md · HCS-Inspector（HCS 云服务巡检平台）

> 写给 Claude Code 的项目说明，每次会话自动加载，只放「必须一直记住」的内容；细节按需读下面列出的文档。
> 子目录说明：`backend/CLAUDE.md`、`frontend/CLAUDE.md`。**接续开发的任务卡与验收标准：`docs/接续开发任务书.md`**。

## 项目

运维组内部使用的华为云 Stack（HCS 8.5.1）**只读**运维平台，v0.4.0，多租户：

- **巡检告警**：定时采集 DataArts / MRS（集群 + 经 DataArts 实例节点的作业）/ GaussDB / DCS / ECS / CCE 容器事件 → 37 条内置规则 → 告警去重 / 升级 / 恢复 → 邮件通知（认领 / 升级）、维护窗口与静默
- **资源台账**：每小时按「租户 × 项目」同步 11 类资源，变更留痕，字典约束 + Excel 导入导出
- **报表中心**：日 / 周 / 月 / 台账报表，定时发邮件

进程（同一后端镜像）：`api`（uvicorn）· `scheduler`（巡检 / 台账 / 报表 / 清理，APScheduler + Redis 锁）· `notifier`（Outbox → SMTP）· `migrate`（alembic + bootstrap）。
`backend/mock_hcs/` 模拟 HCS 8.5.1 的两个租户（账号密码 + CES / AK/SK + Prometheus）。生产形态：**单机 docker compose**（`deploy/`），PostgreSQL 16 + Redis 7。

## 技术栈

- 后端 `backend/`：Python 3.12（镜像与 CI）· FastAPI · SQLAlchemy 2（同步 ORM）· Alembic · Pydantic v2 · APScheduler 3 · redis-py · httpx · openpyxl · Jinja2；测试 pytest，开发 / 测试库 SQLite
- 前端 `frontend/`：Vue 3.5 + TypeScript 5.6 + Vite 6 + Element Plus 2.9（组件按需引入）+ Pinia + ECharts 5（按需）+ axios + dayjs

## 文档索引（只读与当前任务相关的）

| 想了解 | 文档 |
| --- | --- |
| 现在做什么、优化清单 C01–C22、任务卡 T01–T21、完成定义 | `docs/接续开发任务书.md` |
| 编码约定；新增采集器 / 台账类型 / 规则 / 接口 / 页面 / 设置项 / 权限点 / 迁移；测试；发版 | `docs/开发文档.md` |
| 模块划分、数据模型、关键流程（采集 → 判定 → 告警 → 通知） | `docs/架构文档.md` |
| 部署、`.env`、升级回滚、备份恢复 | `docs/部署运维手册.md`、`deploy/README.md` |
| 对接真实 HCS、`make probe`、字段映射 | `docs/联调手册.md` |
| 路线图与待办 B01–B28 | `docs/后期开发方案.md` |
| 接口与权限点清单（**生成物**，用 `make apidoc` 更新，不要手改） | `docs/接口清单.md` |
| 资源 / 指标 / 事实说明（**生成物**，用 `make guides` 更新，不要手改） | `docs/资源与指标指南.md` |

## 常用命令（仓库根目录）

```bash
make init                     # 首次：venv + 依赖（backend/constraints.txt 锁版本）+ npm ci
make dev-api / make dev-web   # 后端 :8000（SQLite + 演示数据，/api/docs）/ 前端 :5173
make dev-mock                 # Mock HCS :9000；backend/.env 设 INSPECTOR_HCS_MOCK_URL=http://127.0.0.1:9000 走真实采集链路
make check                    # 提交前必跑（= CI）：ruff + mypy + vue-tsc + pytest + 前端构建
make lint / make test         # 只做静态检查 / 只跑后端测试
cd backend && .venv/bin/pytest tests/test_rules.py -k expr -x   # 跑单个文件 / 用例
make revision m="..."         # 生成迁移（建议加 --rev-id，见 backend/CLAUDE.md）；make migrate 升级
make apidoc                   # 改了接口后重新生成 docs/接口清单.md
make run-once task=ecs_status # 手动跑一次巡检任务；make report kind=DAILY；make status
make probe args="--mock-url http://127.0.0.1:9000 --only dataarts"   # HCS 只读接口探测
```

项目自定义命令：`/task T04`（按任务卡开发）、`/check`、`/new-api`、`/probe-review`、`/release 0.5.0`，定义在 `.claude/commands/`。

## 硬性约束（违反即缺陷）

1. **对 HCS 只读**：只调用查询类接口（及 IAM 取 Token）；新增 HCS 接口要登记到 `backend/app/tools/probe.py` 的 `PROBES`，联调时可核对字段。
2. **机密**：不读取、不输出、不提交 `deploy/.env`、`deploy/data/`、`deploy/backups/`、证书；密码 / SK / Token 不进日志、审计和接口返回；凭据经 `core/crypto` 加密存储，展示时脱敏。
3. **权限**：每个写接口 `require_perm(...)`；查询按 `get_scope` 过滤租户；新权限点登记在 `core/permissions.py`（`PERMISSIONS` / `BUILTIN_ROLES` / `ADDED_IN`）。`tests/test_apidoc.py` 会检查写接口是否声明权限。
4. **审计**：所有写操作调用 `audit.log("对象.动作", ...)`。
5. **多租户隔离**：资源 / 告警 / 台账数据都带 `tenant_id`（+ `project_ref`），查询与写入不得跨租户混用（已知缺陷见任务书 C01）。
6. **迁移**：模型变更必须配 Alembic 迁移，可 downgrade，同时兼容 SQLite（batch 模式）与 PostgreSQL；**不要修改已发布的 0001–0003**。
7. **时间**：库内存 UTC（`utcnow()` / `UtcDateTime`），接口传 ISO 8601，前端按本地时区显示。
8. **接口形态**：返回 `{"code": 0, "message": "ok", "data": ...}`，错误抛 `BizError`；分页参数有上限。
9. **不引入新的基础设施**（ES / K8s / MQ 等）；新增 Python / npm 依赖要说明理由并更新锁文件（`make lock` / `package-lock.json`）。
10. **不要对整个仓库跑 `ruff format` / prettier**（现有代码未统一格式化，会产生上百个文件的无关 diff）；只整理自己改动的代码。

## 完成定义（每个任务）

- `make check` 通过；新逻辑有测试，修 bug 先写能复现的失败用例
- 改模型 → 新迁移，`tests/test_migrations.py` 仍通过（upgrade → 与模型一致 → downgrade → upgrade）
- 改接口 → `make apidoc`；改行为 / 配置 / 部署 → 同步 `docs/` 对应章节，并在 `CHANGELOG.md` 的「Unreleased」记一条
- 前端改动在 `make dev-api` + `make dev-web` 下实际点过（或有 E2E 覆盖）
- 一个任务一个分支（`task/T04-resource-identity`），提交信息 `<type>(<scope>): <中文摘要>`，type ∈ feat / fix / perf / refactor / test / docs / chore / ci，正文写任务编号、改动点、验证方式

## 工作方式

- 先读任务卡 → 给出计划（改哪些文件、是否要迁移、怎么测）→ 再动手；小步提交
- 业务口径（阈值、HCS 字段含义、报表统计口径）不确定时停下来问，不要猜；字段映射以 `make probe` 实测为准
- 不要 `git push`、不要打 tag、不要改 `deploy/.env`；发版流程见 `/release` 与 `docs/开发文档.md` §7
- 演示数据（`app/demo/`）只服务开发 / 演示环境，业务逻辑不得依赖它