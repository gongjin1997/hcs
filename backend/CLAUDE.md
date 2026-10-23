# backend/CLAUDE.md · 后端速查

> 详细约定见 `docs/开发文档.md` §3–§5、§8；这里只列写代码时最常用的入口和容易踩的坑。命令在 `backend/` 下用 `.venv/bin/...`，或在仓库根目录用 `make`。

## 分层与入口

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 路由 | `app/api/v1/*.py`，注册在 `app/api/router.py` | 参数校验、`require_perm`、`get_scope`、`audit.log`、组装 `ok(...)`；端点用同步 `def`（SSE 例外，`stream.py`） |
| 请求 / 响应模型 | `app/schemas/` | Pydantic v2，校验提示写中文；新接口尽量声明 `response_model=ApiResponse[...]` |
| 业务 | `app/services/`、`app/ledger/`、`app/notify/`、`app/reports/` | 接收 `Session`，可被 worker / 工具 / 测试直接调用 |
| 采集 | `app/collectors/*.py`（注册 `registry.TASKS`）、`app/k8s/`、`app/metrics/`（CES / Prometheus） | 只调 HCS + 产出 `Observation`；`RunContext` 提供 `client` / `db` / `tenant_id` / `project_ref` |
| 判定与告警 | `app/rules/`（`default_rules.yaml`、`expr.py` 安全表达式、`evaluator.py`）→ `app/services/pipeline.py` → `app/services/alarm_service.py` | 指纹 `app/alarms/fingerprint.py`（账号 / 项目 / 资源 / 规则 / 子对象） |
| HCS 客户端 | `app/hcs/`（`client.py` 认证 / 限流 / 重试，`factory.py` 按租户构造，`pager.py` 翻页，`signer.py` AK/SK） | 只读；`DEFAULT_QPS` 每服务 5 QPS |
| 进程 | `app/workers/`（`scheduler` / `notifier` / `bootstrap` / `run_once`） | 定时任务有 Redis 锁，`max_instances=1` |
| 工具 | `app/tools/`（`probe` 接口探测、`status` 自检、`apidoc` 接口清单） | |
| 基础设施 | `app/core/`（`config` 设置、`db`、`deps` 鉴权与范围、`permissions`、`audit`、`crypto`、`security`、`events` SSE 事件、`metrics`、`response`、`xlsx`） | |
| 模型 / 迁移 | `app/models/`、`migrations/versions/`（0001–0003 已发布） | |

## 写代码时照着用

```python
from app.core.response import ok, not_found, BizError, ErrorCode

@router.put("/xxx/{id}", summary="修改 xxx（进接口清单）")
def update_xxx(id: int, body: XxxPatch, db: Session = Depends(get_db),
               audit: Auditor = Depends(get_auditor), scope: Scope = Depends(get_scope),
               _: CurrentUser = Depends(require_perm("xxx:manage"))):
    obj = db.scalar(scope.apply(select(Xxx).where(Xxx.id == id), Xxx.tenant_id))
    if obj is None:
        raise not_found("xxx")
    ...
    audit.log("xxx.update", target_type="xxx", target_id=id, target_name=obj.name, detail={"changes": diff})
    return ok(XxxOut.model_validate(obj).model_dump())
```

- 错误码：`40001` 参数 · `40101` 未登录 · `40301` 无权限 · `40401` 不存在 · `40901` 冲突 · `50001` 内部 · `50201` HCS 上游
- 配置：`get_settings()`（环境变量前缀 `INSPECTOR_`，`backend/.env.example`）；运行中可改的配置用 `settings_service`（新增字段不用迁移），不要再加“改了要重启”的环境变量
- 时间：`from app.models.base import utcnow`；列类型 `UtcDateTime`；不要用 `datetime.now()` / 无时区时间
- 日志：`logging.getLogger(__name__)`，JSON 输出，用 `extra={...}`；不打印凭据和完整响应体
- HCS：`ctx.client.get("dcs", "/v2/{project_id}/instances", params=...)`；可选能力（CES）用 `guarded_ces(...)`；新接口登记 `tools/probe.py` 的 `PROBES`；路径里的名称参数要 `urllib.parse.quote(name, safe="")`
- 活动告警状态：`models/alarm.py` 的 `ACTIVE_STATUSES = ("FIRING", "ACKED", "SUPPRESSED")`；同一指纹只允许一条活动告警（部分唯一索引 `uk_alarm_active`）
- 资源类型目录：`services/catalog.py` 的 `RESOURCE_TYPES`（前端字典 `frontend/src/utils/dict.ts` 要同步）；内置任务 `services/bootstrap.py` 的 `BUILTIN_TASKS`

## 迁移

```bash
cd backend && .venv/bin/alembic revision --autogenerate --rev-id 0004 -m "resource identity per project"
# 生成后人工检查；SQLite 改表用 op.batch_alter_table；JSON 列 sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head
```

- 修订号顺序递增（`0004`、`0005` …），按合入顺序取下一个，不提前占号；文件名模板 `YYYYMMDD_<rev>_<slug>.py`
- 只做向前兼容的改动（加表 / 加可空列 / 加索引）；删列、改类型分两个版本
- 数据补齐写在迁移里且幂等；新权限点补授参考 `0003` 的 `_grant_new_permissions` + `ADDED_IN`
- 部分唯一索引写法参考 `uk_alarm_active`（同时给 `sqlite_where` 和 `postgresql_where`）

## 测试

- `tests/conftest.py` 在导入 app 前把库指向临时 SQLite，并开启演示数据。夹具：`client`（会话级 TestClient）、`admin`（admin 请求头）、`login(client, u, p)`、`demo_login(client, 用户名, tenant_id)`、`tenant_ids`、`mem_db`（空内存库）、`world`（空库 + 两个租户）、`mock_http`（进程内 Mock HCS）
- 规则用 `tests/test_rules.py`（纯函数）；流水线 / 多租户用 `world` + `mock_http` 跑 `run_task`；接口用 `client` + `demo_login`
- `client` 是会话级共享库：用例之间会互相看到数据，断言用“包含 / 增量”，不要断言全表条数；需要干净库用 `mem_db` / `world`
- 全量约 23 秒；最慢的是首次启动写演示数据（约 5 秒）和 `test_bootstrap`（约 5 秒）
- 当前只在 SQLite 上测试，PostgreSQL 差异（部分索引、`SKIP LOCKED`、VARCHAR 长度、JSONB）见任务书 T02

## 易踩的坑

- `Session` 不是线程安全的：采集器并发调用 HCS 时，只把 HTTP 请求放进线程，写库回到主线程
- 资源唯一键目前是全局 `(resource_type, resource_id)`，跨租户同 ID 会串数据（任务书 C01 / T04 修复）；在那之前新增查询务必带上 `tenant_id` / `project_ref`
- `default_rules.yaml` 只在规则不存在时写入，改了 YAML 已部署环境不会变（要么页面改，要么写迁移）
- 采集开关（`services/collect_switch.py`）和静默（`services/silence_service.py`）会影响“有没有告警”，排查时先看这两处
- `services/seed*.py` 只服务演示环境，复杂度高，不在重构范围
- 现有代码未统一 `ruff format`，不要全量格式化；ruff 规则 `E,F,I,B,UP`，行宽 128