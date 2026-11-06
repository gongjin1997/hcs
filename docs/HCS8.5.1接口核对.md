# HCS 8.5.1 DCS 与 AOM 接口核对

> 2026-11-02 最新策略（`20261102-aom-disabled-1`）：所有资源暂时停用 AOM。本文此前（含同日早期）关于显式 AOM、映射编辑、探测或查询可用的结论仅保留为历史审计，已由 [AOM停用说明](AOM停用说明_20261102.md) 覆盖；无正式 HCS 调用证据，Mock 成功不代表生产可用。

> 2026-11-02 修订：DCS 已统一为原生接口采集及本地实际采样历史，不再使用本文早期核对中的可选 AOM 路径。当前实现以 [DCS内存监控](DCS内存监控.md) 和 [DCS原生指标接口核对](DCS原生指标接口核对.md) 为准；其他服务的 AOM 协议核对仍保留。

核对日期：2026-10-27。本文只研究公开华为官方文档，没有连接生产云，没有读取配置、凭据或业务数据，没有修改采集器。环境前提由用户确认：私有 Huawei Cloud Stack 8.5.1，没有 CES，有 AOM；DCS 当前数据应调用 DCS 自身接口。

## 1. 结论与证据等级

| 问题 | 已核实结论 | 依据与边界 |
| --- | --- | --- |
| DCS 列表、详情能否读取内存 | 可以。`used_memory`、`max_memory` 都标为 **MB**；`capacity`、小规格的 `capacity_minor` 标为 **GB** | 精确 8.5.1 文档 [D1][D2]。不是 byte，也不能拿配置容量替代实际内存分母 |
| DCS 有无原生统计接口 | 有：`GET /v2/{project_id}/instances/statistic`，只返回 Running 实例 | [D3]。路径是单数 `statistic`，不是 `/stats`；没有时间范围参数 |
| DCS 原生统计中的 CPU | 推荐 v2 的 `used_cpu` 是用户态与内核态的**累计 CPU 时间，秒** | [D3]。不能直接显示为 CPU 使用率；同文档的旧 v1.0 将同名字段写作百分比，见第 3 节 |
| DCS 原生接口有无连接数 | 已核的实例列表、详情、统计响应没有 `connected_clients`；另有指定节点的会话查询，响应有 `count` | [D1][D2][D3][D5]。会话查询不能自动等同实例监控连接数 |
| DCS 原生接口有无历史内存查询 | 已核 API 目录及上述查询没有可指定起止时间的内存时序接口 | [D0][D1][D2][D3]。是“尚未找到官方契约”，不是断言所有补丁都不可能有；本机真实采样历史不应冒称云端原生历史 |
| AOM 8.5.1 有无监控查询 API | 有。v1：`POST /v1/{project_id}/ams/metrics`、`POST /v1/{project_id}/ams/metricdata`；v2：`POST /v2/{project_id}/series`、`POST /v2/{project_id}/samples` | 精确 8.5.1 文档 [A1][A2][A3][A4]。这些 POST 是查询；不能因此放开该服务任意 POST |
| AOM 查询用什么 Token | 这四个接口的请求头表要求 `X-Auth-Token`，明确为 IAM 用户 Token | [A1][A2][A3][A4][A5]。未把 ManageOneToken 列为它们的替代请求头 |
| DCS 能否直接映射到 AOM | **未证实**。DCS 监控目录有 `SYS.DCS`，但 AOM API 的命名空间表未列该名称，也没有给出 DCS 资源映射 | [D6][A1][A2][A3][A4]。不硬改 namespace、不猜 dimension、不用公有云 CES 查询冒充 AOM |

“已核实”指下列指定版本网页正文，不代表现网端点、补丁、授权与指标上报已经联调成功。缺字段、无数据、查询失败须分别保留，不作为数值 0 或成功采集。

## 研究后的实现验收（2026-10-27）

| 原目标 | 当前实现与证据 | 验收结果 |
| --- | --- | --- |
| IAM Token 有效期缓存，不逐接口换发 | `hcs/auth.py`、`hcs/token_cache.py`；按真实 expires_at 提前刷新，本地热缓存与加密 Redis 跨进程合并，401 按代次处理。真实 Redis 专项 51 项覆盖过期、并发、隔离与失败处理 | 通过；局域网新建 5 个客户端执行 5 次业务请求，IAM 换发 0 次 |
| DCS 当前数据调用自身 API | `collectors/dcs.py`；v2 实例列表与运行实例批量 statistic，内存 MB；累计 CPU 秒数不作百分比；本轮缺失值不沿用旧指标 | 通过；连续两轮每轮原生 DCS 请求 2 次；不走 CES |
| 本局点没有 CES，使用显式 AOM | 默认 hcs_ces_enabled=false，AUTO 不探测 CES；DCS 历史的旧导入模块仅保留纯采样类型；客户端在鉴权前阻止未启用的 CES | 通过；本轮连接、巡检、历史与导出观测 CES 请求 0 次 |
| 按 HCS 8.5.1 文档实现 AOM 查询 | 以下专版正文 [A1][A4]；`metrics/aom.py` 实现 v1 元数据和 v2 样本，区分请求字段大小写、毫秒时间戳与秒粒度；显式 Endpoint、namespace、指标、完整维度，拒绝歧义与错误单位 | 通过；独立协议 fixture、真实 HcsClient 合成传输及局域网 AOM 查询链路已验证 |
| 控制重复请求与云端压力 | 共用限流、重试预算和查询缓存；显示聚合与 XLSX 复用原始时序，不补零或伪造历史 | 通过；同范围变更展示粒度及导出均新增 0 次 IAM/AOM/CES 请求 |

最终回归：SQLite 1258 passed / 21 skipped，PG16 1264 passed / 15 skipped，均有 4 个 subtests；覆盖率 90.98%。前端 451 项、typecheck、生产构建通过；LAN 启动脚本 12 项通过。通过测试的核心源码未在验收后变更。

上述运行验证仅使用隔离数据库、本地 Mock HCS 与 Mailpit，**不等同生产 HCS 联调**。生产接入须填写局点真实 AOM 地址和指标映射，不从 SYS.DCS 猜测映射。扩展台账/任务审计中的 CSS 组件版本、MRS JobGateway 作业专版仍作为待核项保留在 [全量核查](HCS8.5.1接口全量核查.md)，不列入已确认的生产兼容结论。

## 2. 版本与来源登记

| 标识 | 官方文档及版本 | 本次看到的页面信息 | 使用范围 |
| --- | --- | --- | --- |
| DCS | **EDOC1100450216**，Distributed Cache Service (DCS) **2.3.0.5 Usage Guide (for Huawei Cloud Stack 8.5.1) 01** | 页面更新日 2025-11-28 | DCS 推荐 v2、过时 v1.0、监控指标目录 [D0] |
| AOM | **EDOC1100451569**，Application Operations Management (AOM) **2.3.0.3 Usage Guide (for Huawei Cloud Stack 8.5.1) 03** | 页面更新日 2026-03-27；本次网页标题为 2.3.0.3，不是仅凭搜索摘要的 2.3.0.2 | AOM v1/v2、鉴权 [A0]。变更记录列出 2.3.0.3 新增告警规则 API [A7]；不能据此证明现网任意旧补丁能力 |
| 公有云 AOM 帮助页 | 即使路由相似，也不作为本次 8.5.1 结论的依据 | 未拿公有云产品页替代上述 HCS 专版文档 | 不继承配额、保留期、namespace 或鉴权差异 |
| 新版 HCS AOM 下载资料 | 主线程已识别的 HCS 8.6.0 / AOM 2.5.0 PDF 属于版本不符候选 | 本文没有用它证明 8.5.1 字段 | 下载地址内容可能更新，引用应保留版本与文档编号 |
| ManageOne / OC 北向 | 与 AOM 租户 API 分开核对 | 本次未取得能确认 8.5.1 性能查询、Token 与请求体的官方 API 正文 | 不列未经证实的路由、请求头或指标映射；社区线索不升格为 8.5.1 API 契约 |

正式引用均指向 `support.huawei.cn` 的具体章节；在该站通过独立浏览器会话读取了正文与目录，不只采用搜索摘要。

## 3. DCS 原生 API 证据表

### 3.1 列表与详情

| 项目 | 实例列表 | 指定实例详情 |
| --- | --- | --- |
| 路由 | `GET /v2/{project_id}/instances` | `GET /v2/{project_id}/instances/{instance_id}` |
| 文档位置 | [D1] 表 4-16 至 4-19 | [D2] 表 4-34、4-35 |
| 查询条件 | `instance_id`、`name`、`status`、`ip`、`capacity`、`tags`，以及 `include_failure`、`include_delete`、`name_equal`、`offset`、`limit` | 路径中的项目与实例 ID，无请求体 |
| 分页 | `offset >= 0`；`limit` 为 1–1000，**默认 10**；响应 `instance_num`、`instances[]` | 无列表分页 |
| 名称搜索 | `name_equal` 默认 `false`，按名称模糊搜索；为 `true` 时精确匹配 | 不适用 |
| CPU 相关属性 | 不据此构造 CPU 使用率 | `cpu_type` 为处理器类型，如 x86_64、aarch64，不是性能值 |

原生内存字段的口径：[D1][D2]

| 字段 | 文档类型 | 文档单位与含义 | 适用限制 |
| --- | --- | --- | --- |
| `used_memory` | Integer | MB，已用内存 | 不自行解释成 byte；未提供时保留未知 |
| `max_memory` | Integer | MB，总内存 | 计算占用比须同一次真实响应中有合法分母，且大于 0 |
| `capacity` | Integer | GB，缓存规格容量 | 是规格，不是已用内存；不能用来伪造实际采样 |
| `capacity_minor` | String | GB，小于 1 GB 实例的容量 | 不因 `capacity` 为 0 就把该实例认作零容量；按响应精确解析 |

`100 × used_memory / max_memory` 可以作为应用自己的派生百分比，但应明确其来源是 DCS 原生字段、采样时间是本机采集时刻。文档未给出这两个字段等同于监控 `memory_usage` 的完整聚合规则，也未说明 MB 的十进制/二进制换算基数；不擅自改标 MiB、bytes。[D1][D2][D6]

### 3.2 Running 实例统计

精确路由：`GET /v2/{project_id}/instances/statistic`。响应顶层是 `statistics[]`，数组元素用 `instance_id` 标识实例。文档只说查询 Running 状态实例，没有起止时间、周期或历史游标；未返回的实例不能补零。[D3]

| 字段 | v2 文档类型 | 单位 / 语义 | 不可误用 |
| --- | --- | --- | --- |
| `used_memory` | Long | MB，已用内存 | 不是 Redis INFO 原始 byte 值 |
| `max_memory` | Long | MB，总内存 | 不是 GB 规格字段 |
| `used_cpu` | String | 秒，缓存用户态与内核态消耗的累计 CPU 时间 | 不是百分比；不能仅凭一次值换算 CPU 使用率 |
| `input_kbps` | String | kbit/s，实例入流量 | 不能改标 KB/s |
| `output_kbps` | String | kbit/s，实例出流量 | 同上 |
| `keys` | Long | 缓存数据记录数 | 不是字节数 |
| `cmd_get_count` | Long | GET 命令执行次数 | 文档未定义计数起点、重置与时间窗口；不是 QPS |
| `cmd_set_count` | Long | SET 命令执行次数 | 同上 |

同一 8.5.1 文档的**过时 v1.0** 另有 `GET /v1.0/{project_id}/instances/statistic`。[D4] 表 4-602 将 `used_cpu` 写作 CPU 使用百分比；推荐 v2 的 [D3] 表 4-83 则写作累计 CPU 秒。应保留这一版本差异，不把旧表的百分比单位套入 v2，也不以文档冲突为理由猜测现网返回。两版的 `used_memory`、`max_memory` 都明确为 MB。[D3][D4]

本次没有找到 `/v2/{project_id}/instances/{instance_id}/stats` 的 8.5.1 契约。统计快照不能回查过去；从上线后每次真实采集持久化的本地样本，应标注“本地采样历史”，并保留缺口，不能复制当前 `Resource.metrics` 填满历史区间。[D0][D1][D2][D3]

### 3.3 连接数的另一条只读路径

`GET /v2/{project_id}/instances/{instance_id}/clients` 查询会话，**`node_id` 为必填查询参数**；响应有 `time`（数据更新时间）、`clients[]`、`count`（总会话数）。`limit` 为 1–1000，默认 10；`offset` 描述写作大于等于 0、默认 0，但同表另列 `Minimum: 1`，存在矛盾，应在联调时核实。[D5] 表 4-544 至 4-547。

这里的 `count` 是该节点查询的会话口径，不是实例列表中的 `connected_clients`。也没有证据说明它与监控系统“排除从节点连接”的 `connected_clients` 完全相同。不能只数当前页 `len(clients)` 当总数，更不能未经节点去重与角色规则确认就跨节点相加。[D5][D6]

只读平台不调用同章的关闭会话操作。

### 3.4 DCS 鉴权与历史能力边界

DCS 的 8.5.1 鉴权章节提供 IAM Token 与 AK/SK 两种方式；Token 方式要求项目级作用域，后续请求通过 `X-Auth-Token` 传递。具体 IAM 登录与项目选择由 IAM 适配核实，不推导成 ManageOneToken。[D7]

已经核实原生列表、详情、Running 统计、会话查询，以及官方 API 总览/目录；其中未找到可按时间范围取内存时序的原生接口。[D0][D1][D2][D3][D5] 这不能证明管理面所有接口都不存在历史能力，也不能把监控系统或 AOM 的历史查询改称“DCS 原生历史”。

## 4. DCS 监控指标目录与 AOM 不能自动互换

8.5.1 DCS **Monitoring > DCS Metrics** 明确列出 namespace **`SYS.DCS`**。这是上报给“监控系统”的指标目录，页面没有把对应 API 路由指向 AOM。查看监控的用户步骤是 ManageOne 中进入 DCS 的 Performance Monitoring，或跳转监控系统控制台。[D6]

| 监控字段 | 文档单位与含义 | 与原生 API 的区别 |
| --- | --- | --- |
| `cpu_usage` | %，监控周期内多个采样值中的最大 CPU 使用率 | 不是 v2 统计的 `used_cpu` 累计秒 |
| `memory_usage` | %，内存占用指标 | 不等于原生 API 的 `used_memory` MB |
| `connected_clients` | 连接客户端数，排除来自从节点的连接 | 不是原生实例详情字段，也不能直接等同会话列表 `count` |
| `used_memory` | Redis 3.0 表为 byte；Redis 4.0/5.0 表写 KB、MB 或 byte，可在控制台配置 | **同名不等于同单位**；原生实例与统计 API 明确是 MB |
| `connections_usage` | %，当前连接数占允许最大连接数比例，列于 Redis 4.0/5.0 表 | 不是连接个数 |

上表仅摘取本次需求字段；原文按 Redis 版本、实例/Redis Server/Proxy 分类，原始监控周期列为 1 分钟，不能据此承诺现网每分钟都有数据。[D6]

监控维度键分别是 `dcs_instance_id`、`dcs_cluster_redis_node`、`dcs_cluster_proxy_node`、`dcs_cluster_proxy2_node`。单节点、主备、读写分离、Proxy Cluster、Redis Cluster 的监控层级与主节点聚合规则不同，应保持原表口径；不能把所有节点数值简单平均或求和。[D6]

目前的证据只确认 DCS 监控目录存在 `SYS.DCS`，**没有确认该私有环境的 AOM 能发现它**。接入 AOM 时应以已授权项目实际返回的 namespace、metricName/metric_name、完整 dimensions、unit 为准；发现不到时报告未发现/不支持，不映射到假定的 `instance_id`。[A1][A2][A3][A4][D6]

## 5. AOM 8.5.1 共通鉴权与命名空间

四个查询接口的 header 表均要求 `X-Auth-Token`（IAM 用户 Token）与 `Content-Type: application/json`，可选 `X-Aom-Prometheus-Id`；未传后者时使用默认 Prometheus 实例。[A1][A2][A3][A4]

鉴权章节说明支持 Token 或 AK/SK；Token 通过 IAM 获取，章节描述有效期 24 小时并建议缓存。构造请求章节给出项目作用域的 `POST /v3/auth/tokens`，从响应 `X-Subject-Token` 取得后续 `X-Auth-Token`。实际客户端仍应依据 Token 响应的有效时间与授权处理；这里不复制任何真实凭据，也不把 ManageOne/OC 的认证方案套入 AOM。[A5][A6]

Endpoint 随服务和区域而异；文档给的是相对资源路径。不能从公有云域名、ManageOne 登录地址或 DCS Endpoint 猜出私有 AOM Endpoint。[A6]

四个查询接口的 namespace 参数表列出的范围：[A1][A2][A3][A4]

| namespace | 官方表中的对象范围 | 本次 DCS 判定 |
| --- | --- | --- |
| `PAAS.CONTAINER` | 组件、实例、进程、容器指标 | “实例”并不自动指 DCS 实例 |
| `PAAS.NODE` | 主机、网络、磁盘、文件系统指标 | 不能直接替代缓存服务实例指标 |
| `PAAS.SLA` | SLA 指标 | 未提供 DCS 映射 |
| `PAAS.AGGR` | 集群指标 | 未定义 DCS 分片聚合映射 |
| `CUSTOM.Prometheus` | Prometheus 指标 | 只有实际上报的序列可查询；不能假定已部署 Redis exporter |
| 自定义 namespace | 自定义指标 | 使用实际发现/上报的名称、维度与单位 |
| `SYS.DCS` | **未列在上述 AOM 查询表中**；出现在 DCS 监控目录 [D6] | 未证实可经 AOM 查询；“未列出”也不是协议明确禁止一切 SYS namespace |

AOM 开发示例另外使用自定义 `NOPAAS.ESC`、`cpu_util`、`instance_id` 演示查询。[A8] 它不是 `SYS.ECS`，也不证明任意原生云服务实例 ID 都可原样作为 AOM 维度。命名空间枚举与示例不等于现网完整指标清单。

## 6. AOM v1：两个查询接口的精确契约

### 6.1 指标目录 `POST /v1/{project_id}/ams/metrics`

来源：[A1]，表 4-2 至 4-11。

- 查询参数：`limit` 为 1–1000、默认 1000；`start` 为非负分页起点；另有 `type`。
- 普通查询体：`metricItems[]`，每项 `namespace` 必填；`metricName` 可选，长度 1–1000；`dimensions[]` 可选。不能写成 v2 的 `metric_name`。
- 每个维度对象用 `name`、`value`。`name` 非空、最多 255 字符，正则 `^[a-zA-Z_][a-zA-Z0-9_]*$`；`value` 非空、最多 1024 字符。
- `type=inventory` 时用 `inventoryId` 替代 `metricItems`；格式 `resType_resId`，resType 列为 host、application、instance、container、process、network、storage、volume。没有列出 dcs，不能拼接假定的 `dcs_<id>`。
- 响应 `metrics[]` 的字段包括 `namespace`、`metricName`、`unit`、`dimensions`、`dimensionvaluehash`；`metaData` 有 `count`、`total`、`start`（下一页起点，null 表示没有更多数据）。分页必须消费实际返回的起点，不因首批为空/不足上限就编造完整清单。

下列是根据字段表整理的**协议示意**。占位 namespace、metric、维度必须换成发现到的真实组合；不表示 DCS 可用，示例值不是现场数据。

```json
{
  "metricItems": [
    {
      "namespace": "<discovered_namespace>",
      "metricName": "<discovered_metric>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ]
}
```

对应成功响应层级示意：业务成功码按本章官方示例为 `SVCSTG_AMS_2000000`；不要只判断 HTTP 200。[A1]

```json
{
  "errorCode": "SVCSTG_AMS_2000000",
  "errorMessage": "success",
  "metaData": {"count": 1, "start": null, "total": 1},
  "metrics": [
    {
      "namespace": "<discovered_namespace>",
      "metricName": "<discovered_metric>",
      "unit": "<returned_unit>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ]
}
```

普通查询应保留发现响应的完整维度。官方示例中的 `appName` + `clusterName`，以及响应示例中的 `instance_id`，仅是示例，不提供 DCS 资源 ID 映射。本文未找到该接口对模糊匹配、通配符和多项 AND/OR 关系的明确约定，不自行扩展。[A1]

### 6.2 数据查询 `POST /v1/{project_id}/ams/metricdata`

来源：[A2]，表 4-12 至 4-23。

| 参数 | 精确契约 |
| --- | --- |
| query `fillValue` | 可选，默认 `-1`；允许 `-1`、`0`、`null`、`average`。保留缺口应显式选择 `null`；不能把填充值 0 当真实采样 |
| body `metrics` | 必填，最多 20 个对象；每项 `namespace`、`metricName`、`dimensions` 必填；维度数组、维度 name/value 均不得为空 |
| body `period` | 必填，**秒**：60、300、900、3600 |
| body `statistics` | 必填字符串数组，枚举 `maximum`、`minimum`、`sum`、`average`、`sampleCount` |
| body `timerange` | 必填字符串，`startTimeInMillis.endTimeInMillis.durationInMinutes`；注意小写拼接的 `timerange` |

时间规则见第 8 节。以下 `-1.-1.5` 表示基于服务端当前时间取最近 5 分钟；不依赖客户端时钟拼接当地时区字符串。[A2]

```json
{
  "metrics": [
    {
      "namespace": "<discovered_namespace>",
      "metricName": "<discovered_metric>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ],
  "period": 60,
  "statistics": ["average", "maximum", "sampleCount"],
  "timerange": "-1.-1.5"
}
```

响应路径是 `metrics[].dataPoints[].statistics[]`。字段表把 `timestamp` 定义为 Long、统计 `value` 为 Double，但官方响应示例把二者写成字符串；解析器需要明确处理真实返回，不能遇到字符串就当无数据。响应 timestamp 表未单独写单位，示例是 13 位毫秒形态；请求时间明确为毫秒，不把这一推断冒充响应表的显式单位说明。[A2]

```json
{
  "errorCode": "SVCSTG_AMS_2000000",
  "errorMessage": "success",
  "metrics": [
    {
      "metric": {
        "namespace": "<discovered_namespace>",
        "metricName": "<discovered_metric>",
        "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
      },
      "dataPoints": [
        {
          "timestamp": 1467892800000,
          "unit": "<returned_unit>",
          "statistics": [{"statistic": "average", "value": 23.0}]
        }
      ]
    }
  ]
}
```

上面的数值只演示响应类型与层级，不是现场测量，也不保证请求的每种 statistic 都有数据。该接口没有目录式分页字段；时间范围过大时按上限分片，不附加猜测的 page 参数。[A2]

## 7. AOM v2：字段不能与 v1 混写

官方 API Overview 建议新用户使用 v2，并注明 v1 后续会下线，但未在该页给出下线日期。[A0] 这是一条版本说明，不要求已有适配在未联调时强行切换。

### 7.1 `POST /v2/{project_id}/series`

来源：[A3]，表 4-84 至 4-93。

| 层级 | 字段及约束 |
| --- | --- |
| body | `series` 必填，数组最多 100 项 |
| `series[]` | `namespace` 必填；`metric_name` 可选、1–255 字符；`dimensions` 可选 |
| `dimensions[]` | 对象字段为 `name`、`value`；目录接口的维度字段表标为可选，不擅自套用数据查询的必填约束 |
| response | `series[]` 包含 `namespace`、`metric_name`、`unit`、`dimensions`、`dimension_value_hash`；`meta_data` 包含 `count`、`offset`、`total`、`nextToken` |
| 分页缺口 | 功能文字提及分页，响应表也有下一页 `offset`（null 表示没有更多）、`nextToken`（Long）；但本节请求表**没有列出** `limit`、`offset`、`nextToken` 等输入参数，不据此猜请求契约 |

协议层级示意，不表示 DCS 可用：[A3]

```json
{
  "series": [
    {
      "namespace": "<discovered_namespace>",
      "metric_name": "<discovered_metric>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ]
}
```

```json
{
  "meta_data": {"count": 1, "offset": null, "total": 1},
  "series": [
    {
      "namespace": "<discovered_namespace>",
      "metric_name": "<discovered_metric>",
      "unit": "<returned_unit>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ]
}
```

过滤依据是上述 namespace、metric_name、dimensions。文档例子用 namespace 加 `appName`、`clusterName` 查询，没有明确介绍模糊匹配、通配符、正则或 AND/OR；不能把任意 metricName 风格参数传给它，也不能声称可以无 namespace 枚举全平台指标。[A3]

### 7.2 `POST /v2/{project_id}/samples`

来源：[A4]，表 4-94 至 4-106。

- query 为 **`fill_value`**，不是 `fillValue`；默认与选项同 v1。
- body 为 **`samples`**，最多 20 项；每项 `namespace`、**`metric_name`**、`dimensions` 必填，维度数组及 name/value 不得为空。
- body 同时有 `period`、`statistics`、**`time_range`**；period 秒数、统计枚举、时间格式与 1440 点约束同第 8 节。
- 响应为 `samples[].sample` 与 `samples[].data_points[]`，不是 v1 的 `metrics`、`metric`、`dataPoints`。

协议层级示意：[A4]

```json
{
  "samples": [
    {
      "namespace": "<discovered_namespace>",
      "metric_name": "<discovered_metric>",
      "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
    }
  ],
  "period": 60,
  "statistics": ["average", "maximum", "sampleCount"],
  "time_range": "-1.-1.5"
}
```

```json
{
  "samples": [
    {
      "sample": {
        "namespace": "<discovered_namespace>",
        "metric_name": "<discovered_metric>",
        "dimensions": [{"name": "<dimension_name>", "value": "<dimension_value>"}]
      },
      "data_points": [
        {
          "timestamp": 1467892800000,
          "unit": "<returned_unit>",
          "statistics": [{"statistic": "average", "value": 23.0}]
        }
      ]
    }
  ]
}
```

官方 v2 示例同样将 timestamp/value 写成字符串，而类型表是 Long/Double。上例用类型表对应的数值形式，仅示意解析层级，不是实测响应。[A4]

## 8. 时间、缺口、单位与低调用边界

以下时间契约同时见 [A2][A4]，仅字段名分别为 `timerange` 与 `time_range`：

| 项目 | 规定 |
| --- | --- |
| 格式 | `startTimeInMillis.endTimeInMillis.durationInMinutes`，三个段用点分隔 |
| 时间单位 | 起止时间为毫秒；duration 为分钟；period 为秒 |
| 两端 `-1` | 以服务端当前 UTC 时间为终点，向前取 duration 分钟 |
| 只起点为 `-1` | 起点 = 终点 − duration × 60 × 1000 |
| 只终点为 `-1` | 终点 = 起点 + duration × 60 × 1000；超过系统当前时间时取当前时间 |
| duration | 必须大于 0，且大于等于 `(end − start)/(60 × 1000) − 1` |
| 单次上限 | `durationInMinutes × 60 / period <= 1440` |
| period | 只列 60、300、900、3600 秒；不是应用任意 10/30 分钟档位都能直传 |

由点数上限计算，1 分钟粒度最多请求 1 天、5 分钟粒度最多 5 天、15 分钟粒度最多 15 天、1 小时粒度最多 60 天。这只是**单次请求跨度约束的算术结果**，不是可用历史保留期，也不证明某时段已有上报。上述 API 章节没有承诺本项目 31 天历史一定存在，也未明确起止边界是否为半开区间；应用层边界需要显式去重、筛选和测试。[A2][A4]

建议目录发现与数据查询分离：有合法 namespace 后查询目录，保留完整维度和返回单位；数据按最多 20 序列批量，按 1440 点约束切片。不要为每个时间桶重新发现目录。目录分页信息不足时应暴露未完整获取，而不是假装全量成功。这些是基于契约的应用实现建议，不是厂商配额。[A1][A2][A3][A4]

缺口选择 `fillValue=null` / `fill_value=null`，区分无序列、空点、填充值、错误与真实 0。`average` 填洞会产生插值，不应当成真实采样。多桶汇总若只有各桶平均值，不能无依据等权平均成“全部样本平均”；应同时保留有效样本数、统计类型与粒度。`maximum` 是该上游统计周期的最大值，不是原始每次请求延迟。[A2][A4]

## 9. 尚未证实与后续核验

1. **现网能力**：本次没有连接云端；AOM Endpoint、实际组件补丁、已授权项目、指标上报情况与历史保留策略仍需本机批准的联调核实。
2. **DCS → AOM**：没有发现可复用的官方 DCS 到 AOM namespace/metric/dimension 映射。DCS 监控目录与 AOM 契约应分别保留。[D6][A1][A2][A3][A4]
3. **ManageOne/OC**：没有取得精确 8.5.1 北向性能接口主文档正文，不确认路径、ManageOneToken 获取方式或与 IAM Token 的兼容性，不复用 AOM 客户端代替核验。
4. **v2 目录分页**：返回有分页元数据，但请求分页参数在本节缺失；暂不能由响应字段反推输入名、默认页长或平台总上限。[A3]
5. **CPU 同名字段差异**：v1.0/v2 统计文档冲突须按版本保留，未实测前不合并为同一百分比指标。[D3][D4]
6. **单位/时间边界**：DCS MB 未注明换算基数；AOM 响应 timestamp 表未单列单位、示例为毫秒形态；请求半开边界未在已核章节声明。[D1][D2][D3][A2][A4]

本次结论允许先完成 DCS 原生当前内存与真实本地采样历史、AOM 独立目录/查询适配；不据此宣称 AOM 已能提供 DCS 云端历史。

## 10. 证据复核方式

本次保存了官方页面加载后的原始 DOM 章节片段与纯文本，供主线程本机复核；未放入仓库、未混入私有源码或配置。临时证据不是项目运行依赖，也不保证长期保留：

| 本机文件（目录 `/tmp/hcs851-official-research/`） | 内容 |
| --- | --- |
| `aom-v1-official-sections.html` | AOM 8.5.1 `ListMetricItems`、`ShowMetricsData` 的原始章节 DOM，附文档标题与公开来源 URL |
| `ListMetricItems.txt`、`ShowMetricsData.txt` | 对应正文，含请求表、响应表、示例 |
| `aom-v2-official-sections.html` | `ListSeries`、`ListSample` 原始章节 DOM |
| `ListSeries.txt`、`ListSample.txt` | v2 请求、响应、限制的正文 |
| `dcs-life-official-sections.html` | DCS 8.5.1 列表与详情原始章节 DOM |
| `dcs-stat-official-section.html`、`dcs-stat.txt` | 推荐 v2 Running 统计正文，含内存 MB、CPU 秒说明 |
| `aom-v1-metricdata.png` | 官方 v1 数据查询页面截图，可核对相对路由；表格全文以 HTML 片段为准 |

后续复核应打开下列固定文档编号/章节，先看标题中的 HCS 与组件版本，再核对字段表。不能仅凭下载文件名或域名判断版本。

## 官方参考链接

[D0]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/1e71d08/api-reference
[D1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/f0c9651f/lifecycle-management#ListInstances
[D2]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/f0c9651f/lifecycle-management#ShowInstance
[D3]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/51cb72a6/instance-management#ListStatisticsOfRunningInstances
[D4]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/f170fa1f/instance-management-apis#dcs-api-0312014
[D5]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/d47beba5/session-management#ListClients
[D6]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/aa9e60f6/monitoring#dcs-ug-0326019
[D7]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/b4c8007e/authentication
[A0]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/cf1bd29d/api-overview
[A1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/bd0a2e6b/monitoring-v1#ListMetricItems
[A2]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/bd0a2e6b/monitoring-v1#ShowMetricsData
[A3]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/ca0d1efd/monitoring-v2#ListSeries
[A4]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/ca0d1efd/monitoring-v2#ListSample
[A5]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/5fa4cf0c/authentication
[A6]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/28a3ff9a/making-an-api-request
[A7]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/462e1f62/change-history
[A8]: https://support.huawei.cn/enterprise/en/doc/EDOC1100451569/daf6355a/api-for-querying-monitoring-data
