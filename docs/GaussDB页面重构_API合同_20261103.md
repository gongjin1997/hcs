# GaussDB 页面重构 API 合同（2026-11-03）

依据本地 `gaussdb-api.pdf`（GaussDB 25.1.30，文档版本 01 / 2025-03-30）和当前后端实现。下表页码为文档正文页；PDF 阅读器物理页 = 正文页 + 10。未访问正式 HCS、AOM、CES 或数据库。

## UI 可直接使用的能力矩阵

全部入口前缀 `/api/v1/gaussdb`；`resource_ref` 是本地台账资源主键，不是云实例 ID。JSON 使用 `ApiResponse` 信封。

| 页签 / 能力 | 已实现 GET（`/instances/{resource_ref}` 后缀） | 必选参数与分页 | 时效 / 单位 / 限制 | 现有缺口与展示边界 | PDF 正文页 |
| --- | --- | --- | --- | --- | --- |
| 实例选择 | `/instances`（相对前缀） | 无必选；`page=1,page_size=50`，大小 1–100；可筛 `tenant_id/project_ref/keyword` | 本地 ACTIVE 台账，`snapshot_at` 为台账最近发现时间；vCPU、已确认的配置容量 GB | 配置 CPU/内存不是实时使用率；未知配置单位必须保留未知（表 4-56 / 57 的 mem/size 未说明单位） | 72–96（实例列表 §4.3.4）；兼容旧版见 §5.2 |
| 深链接 / 刷新定位 | `/instances/{resource_ref}`（相对前缀） | 仅本地资源主键 `resource_ref` | `data: GaussInstance`，与列表单行完全同类型；仅查询本地 ACTIVE GaussDB 台账 | 只需 `ledger:view` + 当前 scope，不发云、不要求 `task:run`；不可见/非 Gauss/已释放/不存在返回 404。云 ID 格式异常或租户/项目停用仍可读台账 | 本次授权新增的本地只读 API |
| 项目名称筛选 | `/projects`（相对前缀） | 无 | `data: GaussProjectOption[]`；仅可见 ACTIVE Gauss 实例涉及的有效租户项目关联；每项目一项，按租户名、项目名、区域、主键稳定排序 | 无有效项目返回 `[]`；无项目、跨租户错绑项目不列入。停用租户/项目仍可作为已有台账筛选；不发云、不要求 `task:run` | 本次授权新增的本地只读 API |
| 磁盘与组件 | `/overview` | 仅 `resource_ref` | 原生 `volume-usage`：`used_gb/total_gb` 为 GB，`usage_percent` 为 0–100 口径数值；总量 0 时使用率 null。`collected_at` 是本地响应接收时间，非源采样时间。组件按节点分页取回、扁平化为 node/component 配对 | 有实例级存储空间，没有每节点/组件磁盘用量。组件含 CN/DN/CM/GTM/ETCD；不可将全部组件用于慢 SQL/会话。`cpu_usage/memory_usage` 固定 null | 25–27、109–116 |
| 磁盘历史 | `/disk-history` | `start_at/end_at` 带时区；`period_sec=300`，60–86400；最多 5000 桶 | 本地定时 collector 累积；半开区间 `[start_at,end_at)`，>0 且 ≤31 天；GB / %；`timestamp` 为桶起点、`collected_at` 为该桶最后成功样本获取时间 | 没有云历史磁盘 API；不能补上线前历史。桶最后一次失败则值 null；折线 `connectNulls:false`。桶间隔不改变采集频率；现实现历史保留 90 天 | 原始采集依据 25–27；分桶/保留为本平台策略 |
| CPU / 内存巡检报告列表 | `/inspections` | 无必选；时间成对可选，带时区、≤31 天；`limit=50`（1–100），`offset=0`（0–1000） | 只返回当前实例已完成记录；返回原生 `start_time/end_time` 为 UTC 毫秒。后端先遍历最多 20 页×100，再按实例/完成状态/时间过滤并本地分页 | 原生记录默认保留 60 天；没有创建/执行巡检操作。`total=null,truncated=true` 表示扫描不完整，应缩小时间范围 | 372–376 |
| CPU / 内存巡检报告详情 | `/inspections/{record_id}` | 先从列表选择 `record_id` | 仅已完成且归属当前实例的记录。`start_at/end_at` 为报告执行时间；`metric_series[].points[].observed_at` 来自指标 `data.time` UTC 毫秒，与执行时间可不同。单位沿用原生，指标缺失/异常保持 null | 没有实时 CPU/内存 API；没有保证每份报告都含内存。按 `metric+name+unit` 分组、真实节点分线；min/max/average/p95 沿用报告，不造采样点 | 380–384；表 4-318～321，CPU 示例 383–384 |
| 慢 SQL 节点 | `/slow-nodes` | 仅 `resource_ref` | 原生查询 `action=slow`；仅 CN 或 DN 主备节点 | 不用 overview 全部组件代替该节点目录；不支持冷备/日志/仲裁 DN | 384–387 |
| 慢 SQL 列表 | `/slow-sql` | `start_at/end_at`（带时区、≤31 天），`node_ids` 重复查询参数（1–20 个）；`threshold=1`（1–99 秒）；`sql_id` 可选 | 请求时间转为严格递增的 13 位 UTC 毫秒；转毫秒后同一时刻的区间拒绝。`avg_exec_time/avg_cpu_time/avg_io_time` 返回微秒，可能是字符串；次数/行数为原值 | PDF 没有列表分页参数，不能造 page/offset。最多保留 5000 行；截断返回 PARTIAL 和 warning，应缩小节点/时间。命中率原值不可无条件标 `%` | 387–391；表 4-329 / 331 |
| 慢 SQL 详情 | `/slow-sql/detail` | 列表条件 + 必选 `sql_id`；旧 `threshold` 参数兼容接收但不发云 | `total_time/cpu_time/plan_time/io_time/lock_time` 为微秒；`start_time/finish_time` 保留原生 UTC 字符串 | 无分页；表 4-336 类型列写 Integer，但格式描述和示例为 UTC 字符串，不能当请求毫秒解析。只展示查询/执行计划，不执行 SQL | 391–395 |
| 实时会话 | `/sessions` | 同实例 `node_id/component_id` 配对；CN 或 DN 主备；`limit=100`（1–200）。可选 `database_name/user_name/client_ip/state/wait`，wait=t/f | 查询获取时间看 `queried_at`；`exec_time` 秒；`query_runtime` 是原始时长文本，示例 `00:00:22.641774`；会话/事务/语句开始时间原值保留 | 无分页/总数；满 limit 保守标 PARTIAL，`total=null`。统计明确关闭，因此不承诺事务/回滚/SQL 数。没有结束会话、结束空闲会话操作 | 422–427 |
| 同条件 XLSX | `/export` | `panel` 必选：`overview/disk-history/slow-sql/slow-sql-detail/sessions/inspections/inspection-detail`；各页原必选条件原样带上 | 与 GET 共用查询函数和缓存；导出保留原始单位、null、查询范围和状态 | 没有 slow-nodes 专属导出；不是后台全量导出。磁盘历史无云请求，其他面板均为只读查询 | 本地导出能力 |

## 共用状态、权限与只读边界

- `ledger:view`：全部入口；原生在线查询另需现有 `task:run`，本地实例/磁盘历史不需要；XLSX 另需 `ledger:export`。本次不改变权限点。
- 成功查询缓存 30 秒，失败短缓存 2 秒；命中缓存的 `queried_at` 不更新。报告采样时间、磁盘获取时间、台账快照时间须分别显示。
- `AVAILABLE/NO_DATA/PARTIAL/UNAVAILABLE/ERROR/DISABLED/UNSUPPORTED` 是业务状态，HTTP 200 不等于有有效数据；磁盘和组件各有独立状态。`warning/truncated` 必须呈现。禁止把 null 变成 0 或拿配置量填当前/历史曲线。
- 已实现原生 POST 仅为文档查询：`slow-sql-execute-node/slow-sql-list/slow-sql-detail/real-time-session/inspections/records`。其他原生能力用 GET；无云修改、SQL执行、限流设置、扩容、查杀会话，不启用 AOM/CES。
- 原生诊断 URI 固定 `/gaussdb/v3/{project_id}/...`，不继承实例目录可配置的 v3.3/v3.2/v3.1/opengauss_v3 版本。磁盘正文 URI 带 `/gaussdb`，示例省略产品前缀，不跟随示例改正式路径。
- 组件 offset 表（正文 110）同时写“最小 1 / 最大 100”和“默认 0”，存在文档自相矛盾。当前沿用节点记录偏移、每页 limit=100、最多 20 页；不据此臆造另一条 API。提前空页但总数未到、重复节点/组件、总数小于实得条数均返回组件 ERROR，不把部分目录视作完整目录。
- 实时会话 PDF 还有 `session_id/wait_event/exec_time/query_runtime/unique_sql_id/order_by/order` 筛选或排序；当前公开 HTTP 只暴露矩阵所列筛选，不宣称全部已支持。本次不扩展这些参数。
- 原有 API 不添加/删除/重命名字段，JSON 形态和旧导出入口保持兼容。按补充授权新增上述两个本地 GET；`projects` 静态路由单独声明，不与实例动态路由混用。

项目选项类型：

```ts
interface GaussProjectOption {
  project_ref: number
  project_name: string // 未设置名称时回退云 project_id
  tenant_id: number
  tenant_name: string
  region: string | null // HcsProject.region_id
}
// GET /api/v1/gaussdb/projects -> ApiResponse<GaussProjectOption[]>
// GET /api/v1/gaussdb/instances/{resource_ref} -> ApiResponse<GaussInstance>
```

会话组件前后端精确谓词（PDF 正文 111 / 113 / 114 / 423）：

```ts
const canQuerySession = (c: GaussComponent) => c.component_type === 'CN' || (
  c.component_type === 'DN' && (
    ['master', 'slave'].includes(c.role ?? '') ||
    (!c.role && ['Primary', 'Standby'].includes(c.status ?? ''))
  )
)
```

CN 无主备关系，role 可缺。DN role 缺失时只接受文档明确为主/备的 `Primary/Standby` 状态；`Normal` 仅表示正常，不能证明为主备。显式 `offline/secondary/logger_dorado/arbiter` 或未知 role 不被 status 覆盖。不从组件 ID、实例节点角色或“以前查成功”推断角色。慢 SQL 节点的文档响应没有 status，仍仅允许 CN 或 role=master/slave 的 DN。

绕过前端直接查不合规组件：后端 HTTP **400**，`code=40001`，`message="节点和 CN 或 DN 主备组件不匹配"`；这类参数/支持范围错误走 `ApiError`，不是 `GaussNativeRows.status`，没有伪造 HTTP 200 / UNSUPPORTED 或返回 500。原生组件目录结构异常则按既有合同返回 HTTP 200 + `status=ERROR`。已只读审阅当前前端：`api/request.ts` 构造 ApiError → `useGaussPanel.runNative` 捕获 message → `GaussWorkspace` 错误 alert；查询前清空上次结果，不会把拒绝变成旧数据成功态。前端由主代理同步谓词，本侧未写前端。

## 本轮后端核对与验证

已完成 PDF 文本核对，并渲染核看物理页 399（正文 389，慢 SQL 参数表）和物理页 433（正文 423，会话节点/组件限制）。

实际修复：

1. 慢 SQL / 会话之前只检 CN/DN 类型，误放行 DN 冷备、日志、仲裁及未知/缺失角色；现在 CN 无主备要求，DN 必须 master/slave。会话兼容 role 缺失但 status 明确为 Primary/Standby 的组件，不拿 Normal 猜角色；详见上述谓词。overview 仍完整保留全部组件，未删台账内容。依据正文 111、113–114、384、387、391、423。
2. 组件分页提前空页但 total_count 未到时，以前把已取得的部分目录标 AVAILABLE；现在标 ERROR，禁止拿不完整目录继续查询会话。新增重复节点和条数矛盾保护；节点有空组件列表时仍正确按节点推进 offset。依据正文 110、112。
3. 巡检记录分页提前空页时，以前误报完整；现在保留已有本实例记录并标 PARTIAL、truncated=true、total=null。重复外实例记录此前被过滤后绕过重复检查，可能消耗全部 20 次请求；现在在项目原生页层统一检查，第二次重复即安全 ERROR。总数小于实得条数也不再作为成功。列表和详情完成状态验证共用分页逻辑。依据正文 374–375。
4. 巡检详情忽略源端 total_count，之前检查项缺失仍报 AVAILABLE；现在 total 保留源值，已知遗漏或超过 5000 条均标 PARTIAL / truncated，并给 warning。原有 items / metric_series、CPU/内存单位和时间保持兼容。依据正文 381–383。
5. 慢 SQL 之前接受非 13 位毫秒时间，或起止转换成同一毫秒仍发云；现在先本地拒绝，GET/详情/导出共用校验。依据正文 389、393；31 天上限与 20 节点上限是既有平台策略，不冒称 PDF 规定。

本次修改文件仅限：

- `backend/app/api/v1/gaussdb.py`：两个新增本地 GET，原生诊断权限沿用。
- `backend/app/schemas/gaussdb.py`：新增 `GaussProjectOption`；现有模型字段不变。
- `backend/app/services/gaussdb_monitoring.py`：本地查询、项目选项、上述五组边界修复。
- `backend/tests/test_gaussdb_ui_contract.py`：新增 49 个行为用例（参数化计数），覆盖本地 API、权限/租户隔离、无云调用、角色、时间、分页与截断；含缺 role 的 Primary/Standby 兼容和显式非主备角色优先拒绝。
- `backend/tests/test_gaussdb_monitoring.py`：合成 DN 节点响应补上 PDF 定义的 role=master，不放宽断言。
- 本合同文档。

`gaussdb_api.py / gaussdb_native.py / gaussdb_history.py / gaussdb_reports.py` 已核对，无需修改。未改前端、全局导航、默认规则、权限点、迁移、非 Gauss 模块或全局生成文档。`make apidoc` 的共享生成物交主代理统一更新。

隔离验证（全部命令均使用 `INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL=''`）：

- 原基线专项：359 passed；不含迁移。
- 先补失败复现：首批 21 failed / 7 passed；追加时间和矛盾总数 7 failed；缺 role 的明确主备 status 兼容 4 failed，修复后全绿。
- 最终 GaussDB 专项：**409 passed in 28.67s**，包含新合同、监控、报告、历史、API profile、GaussDB 迁移、Mock、PDF 旧契约。
- 另跑 API 文档与只读探测策略回归：**19 passed in 0.50s**（`test_apidoc.py / test_probe_policy.py`），日志 `api-policy.log`。未改共享生成物。
- Ruff：所有本次涉及 Python 文件及核对的 7 个专属源文件均通过；mypy：**7 source files 无错误**。
- 日志位于 `/tmp/hcs-gauss-contract-20261103/`；`gaussdb-final.log` 为最终专项结果。测试仅使用临时/内存 SQLite 和合成 MockTransport，未调用正式外部服务，不代表生产环境已联调。

复现命令（仓库 backend 目录）：

```bash
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' .venv/bin/pytest -q -o addopts='' \
  tests/test_gaussdb_ui_contract.py tests/test_gaussdb_monitoring.py \
  tests/test_gaussdb_reports.py tests/test_gaussdb_history.py tests/test_gaussdb_api_profile.py \
  tests/test_gaussdb_migration.py tests/test_mock_gaussdb_native.py tests/test_pdf_gaussdb_dcs_contract.py
```
