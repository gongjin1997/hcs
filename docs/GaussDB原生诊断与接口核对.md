# GaussDB 原生诊断与接口核对

依据上传的 `gaussdb-api.pdf`：云数据库 GaussDB 25.1.30，文档版本 01，2025-03-30，共 1101 个物理页。SHA-256：`3d7d543e9975acba2a665b5ee1c4d123952850d8e31b5158a9317f4022cfd760`。

## 接口核对

| 能力 | 正文页（物理页） | 原生契约与口径 |
| --- | --- | --- |
| 实例列表 | 72–97（82–107）等已核对版本 | 列表版本 v3.3/v3.2/v3.1/opengauss_v3 明确配置，不逐请求猜版本 |
| 磁盘已用/总量 | 25–27（35–37），§4.2.1 | GET /gaussdb/v3/{project_id}/instances/{instance_id}/volume-usage；used、total 均为 GB。使用率由同次响应计算，total=0 时为空。源端不返回采样时间，前台 collected_at/兼容 observed_at 为本地响应接收时间，time_basis=RESPONSE_RECEIVED |
| 本地磁盘历史 | 本平台定时采样，不是原生历史接口 | GET /api/v1/gaussdb/instances/{resource_ref}/disk-history；仅保存 collector 当时的原生存储响应，缺失和失败为 NULL，不从配置或当前快照补历史 |
| 节点与组件 | 109–115（119–125），§4.3.10 | GET /gaussdb/v3/{project_id}/instances/{instance_id}/components；offset 为记录偏移。父 nodes[].id 为节点 ID，components[].id 为组件 ID，type 区分 CN/DN 等 |
| 慢 SQL 节点 | 384–387（394–397），§4.9.1 | POST .../slow-sql-execute-node，action=slow；返回实例所属 CN/DN 节点 |
| 慢 SQL 列表 | 387–391（397–401），§4.9.2 | POST .../slow-sql-list；start_time/end_time 为 UTC 毫秒，node_ids 非空，threshold 为 1–99 秒。avg_exec_time/avg_cpu_time/avg_io_time 均为微秒 |
| 慢 SQL 详情 | 391–395（401–405），§4.9.3 | POST .../slow-sql-detail；sql_id、node_ids、起止时间必需。total_time、cpu_time、plan_time、io_time、lock_time 为微秒，返回时间字符串保留原生时区 |
| 实时会话 | 422–427（432–437），§4.11.1 | POST .../real-time-session；必须匹配同实例 node_id、component_id；limit 1–200，is_statistic_open 使用字符串 false。exec_time 为会话建立时长（秒），query_runtime 为原生语句运行时长，不能套毫秒单位 |
| 巡检记录 | 372–376（382–386），§4.8.1 | POST /gaussdb/v3/{project_id}/inspections/records；按实例名筛选后仍逐条校验 instance_id，避免重名或上游忽略过滤时串实例 |
| CPU/内存等巡检指标 | 380–384（390–394），§4.8.3 | GET /gaussdb/v3/{project_id}/inspections/records/{record_id}；再校验 response.instance_id。展示检查项、节点、原生单位、原始点时间、min/max/average，不作为当前实例使用率 |

已视觉核对正文第 26、383 页。第 383 页表格将 data 描述为数组，但 JSON 示例为 `{time:[], metrics:[]}` 对象，面板与导出兼容两种结构；仅按原始下标配对，缺失值为空，不补零、不改时间。报告执行时间与其中指标采样时间可能不同，分别显示。

规范化 `metric_series` 优先使用节点指标自身 `metric.unit`，缺失时才用 `indicator.unit`，不换算数值；例如父指标 MB、节点指标 GB，节点曲线与 XLSX 均保留 GB。点按原始 `data.time` 稳定升序排列，null 缺口保留；无效时间、无时间配对的额外数值会从曲线丢弃并在 warning 计数说明，原 `items` 仍保留。不得用报告日期或本地获取时间补采样时间。

本平台新增 `GET .../disk-history` 及 `/export?panel=disk-history`（相同 start_at/end_at/period_sec），仅查询本地定时历史；另支持 `/export?panel=slow-sql-detail`，与慢 SQL 详情使用同一节点、时间和 SQL ID 筛选。存储历史 `time_basis=RESPONSE_RECEIVED`，点的 collected_at/兼容 observed_at 是当次本地响应接收时间；桶起点 timestamp 只用于图表分桶。当前磁盘和存储历史的界面、导出时间应标为“获取时间”，报告 data.time 才是源端采样时间。

## 面板与权限

监控菜单新增“GaussDB 管理”，地址 `/gaussdb`，含磁盘与组件、慢 SQL、会话、CPU/内存及原生巡检报告。列表读取本地台账，手动查询才访问 HCS；不自动轮询。

- 实例列表：`ledger:view`。
- 本地存储历史：`ledger:view`；同条件 `panel=disk-history` 导出额外要求 `ledger:export`，不需要 `task:run`，不访问云。精确 schema 见 [后端联调合同](GaussDB后端联调合同_20261102.md)。
- 原生诊断（含 SQL 文本和会话）：同时需要 `ledger:view`、`task:run`，沿用系统用户及租户范围，不配置独立数据库联系人或数据库登录口令。
- XLSX 导出另需 `ledger:export`，页面与导出复用同一筛选、原始查询和权限；字符串不执行公式。
- 磁盘总量与已用量是原生 GB；列表中的配置 CPU、配置内存与使用率分开，未确认的配置内存单位明确标注。
- CPU/内存没有原生巡检记录或报告点时显示无数据，不拿配置容量、查询时间、其他租户、其他节点的样本填补。
- 会话为只读查询，不提供终止会话、执行 SQL、扩容或改参数；项目既有只读约定不变。

## 调用与隔离

所有查询在缓存前校验本地资源租户、项目、生命周期和当前权限；节点、组件由该实例的原生返回验证，不能由输入的云 ID 直接跨项目查找。停用的租户/项目不创建云客户端。错误记录不把上游响应、SQL 内容或凭据原样写入用户提示。

原生查询缓存 30 秒、失败/部分结果短缓存；按资源、租户、项目、端点、认证配置和筛选参数隔离。同条件导出可复用结果。慢 SQL 未给出分页请求字段，不擅自添加 limit/offset；原生返回总数大于本批或触及本地数量边界时说明未全量。会话原生不提供总数，本批满上限时提示缩小条件，不声称查到全部。

后台 GaussDB 巡检从原生实例列表和 volume-usage 更新状态、磁盘使用率及 GB 用量。旧 AOM/CES CPU/内存值清空，不产生无证据的新观测，也不把未知当恢复。CPU/内存的旧规则说明保留兼容并标注实时未采集。

定时任务在 pipeline 中批量保存 `gaussdb_storage_sample`，按资源、租户、项目及真实采样时刻隔离；失败只记录空值，不回退到旧成功样本。后台固定保留 90 天。前台查询不写采样历史。已有巡检报告详情保留原 `items`，另提供可选 `metric_series`，按实际指标、节点、单位和原始采样时间规范化；CPU-only 报告不补内存，没有实际点的汇总值不造曲线。原始详情未提供状态时还需由已完成记录列表验证，同实例未完成记录不返回指标。

## AOM 的边界

上传的 `aom-api.pdf` 确实包含通用指标查询，不能简单称为“只有日志”。但这不证明本局点 ECS、GaussDB、DCS 指标已接入：通用端点连通、元数据响应成功和日志可用，均不等于产品性能指标可用。

2026-11-02 最新政策：所有资源（含 ECS）均停用 AOM，取代同日此前“ECS 显式映射兼容”的结论。旧地址、映射和历史探测记录保留但不参与当前选源；租户检测与 probe 均不请求 AOM。GaussDB、DCS 的原生查询维持不变，详见 [AOM停用说明](AOM停用说明_20261102.md)。

## 验证与部署

开发验证使用独立数据库、合成 HTTP 响应和独立浏览器会话，不读取正式配置、不连接正式 HCS。开发 Mock 的成功不作为正式环境能力证据。

无新增运行依赖。新增 0013 本地存储样本表（依赖 DCS 的 0012），部署须按既有升级流程迁移至 head；未从存量配置回填任何历史。支持代码包更新，目标机仅用 Docker 与独立 `docker-compose`。本次测试只使用独立 SQLite 与合成云；完整验证和接线位置见 [后端联调合同](GaussDB后端联调合同_20261102.md)。其他服务 API 的审计、已修缺陷及文档缺口见 [系统原生接口审计](系统原生接口审计_20261102.md)。
