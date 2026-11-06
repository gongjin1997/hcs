# HCS 8.5.1 作业接口核对

## DataArts Factory V1

核对日期：2026-10-27。范围为实时作业列表、实时作业状态、批量作业实例列表三个只读接口，以及 `backend/app/collectors/dataarts.py` 的调用和观测字段。

### 官方版本与证据边界

来源均为华为企业支持网站的 **Huawei Cloud Stack 8.5.1** 文档正文，没有使用公有云或 HCS 8.6 接口替代：

- [DataArts Factory API V1 Overview](https://support.huawei.com/enterprise/en/doc/EDOC1100450764/8b8cd163/dataarts-factory-api-v1-overview)
- [Job Development APIs：Viewing a Job List，锚点 dataartsstudio_02_0086](https://support.huawei.com/enterprise/en/doc/EDOC1100450764/8f1c2845/job-development-apis#dataartsstudio_02_0086)
- [Job Development APIs：Viewing Running Status of a Real-Time Job，锚点 dataartsstudio_02_0093](https://support.huawei.com/enterprise/en/doc/EDOC1100450764/8f1c2845/job-development-apis#dataartsstudio_02_0093)
- [Job Development APIs：Viewing a Job Instance List，锚点 dataartsstudio_02_0094](https://support.huawei.com/enterprise/en/doc/EDOC1100450764/8f1c2845/job-development-apis#dataartsstudio_02_0094)

提供线索的标题为 **DataArts Studio 2.11.1.3 Usage Guide (for Huawei Cloud Stack 8.5.1) 04**。核对当天，同一 EDOC1100450764 公网页实际标题已是 **DataArts Studio 2.11.1.4 Usage Guide (for Huawei Cloud Stack 8.5.1) 05**。下述结论来自当前可访问的 05 正文；未取得历史 04 正文，不能声称两个修订逐字相同，也不能据此确认特定已部署补丁版本的全部行为。目标云的只读联调仍需运维授权；本次仅使用合成数据及本地 HTTP transport 测试。

### 请求契约

|用途|方法和相对 URI|实际请求参数|分页与时间窗口|
|---|---|---|---|
|实时作业列表|`GET /v1/{project_id}/jobs`|显式 `jobType=REAL_TIME`，另传 `offset`、`limit`|`offset` 是从 0 开始的页号，不是记录偏移；当前每页请求 100 条|
|单个实时作业状态|`GET /v1/{project_id}/jobs/{job_name}/status`|无查询参数；作业名作为一个路径段进行百分号编码|列表返回的每个作业均查询状态，不从列表推测节点状态|
|作业实例列表|`GET /v1/{project_id}/jobs/instances/detail`|`minPlanTime`、`maxPlanTime`、`offset`、`limit`|`offset` 同样是从 0 开始的页号；时间为毫秒，官方限制窗口不超过 3 天，采集器固定查询本轮开始前 24 小时至本轮开始时刻，各页复用同一窗口|

三个请求的 `workspace` 都是 **HTTP header**，不是查询参数。仅在服务端存在默认工作空间时可省略；查询其他工作空间、多实例场景或没有默认工作空间时应显式配置。采集器对已配置的每个工作空间分别发送 header，重复工作空间不重复请求；空配置保持兼容，交由服务端选择默认工作空间，不猜测 ID。

作业列表省略 `jobType` 时官方默认 **BATCH**，不能依赖 Mock 的默认实时作业行为。实例列表是集合接口，不能改为 `/jobs/{job_name}/instances/{instance_id}`。实例列表还支持可选 `jobName`、`status`、`instanceType`、`preciseQuery` 等过滤；全量巡检不发送这些过滤，也不向该接口添加未定义的 `jobType`。

官方作业列表功能段写单次最多 100 条，`limit` 参数表却允许 1～1000。当前选择 100，兼容两种描述。继续复用既有分页保护：最多 200 页、20000 条；缺失或无效 `total`、缺失列表、提前空页、重复分页、总数矛盾或超预算均记为本工作空间采集错误，不发布部分列表作为完整快照。分页保护不是数据总量保证；源数据在翻页期间变化也可能触发保守失败。

### 响应、状态与台账字段

|位置|官方正文|采集行为|
|---|---|---|
|作业列表|必需 `total`、`jobs`；作业含 `name`、`jobType`、`status` 等；`owner`、`priority`、`path` 可选|校验作业名称、实时类型和重复记录；不把列表调度状态替代 `/status` 的运行状态|
|优先级|`priority` 可选整数 0～2，0 最高、1 中、2 低|保留实际整数 0；缺失、布尔、字符串或越界值为空，不把缺失优先级补成最高|
|实时作业与节点状态|`STARTING`、`NORMAL`、`EXCEPTION`、`STOPPING`、`STOPPED`；根 `status`、`nodes`、节点 `status` 均可选|仅依据实际、已知枚举生成状态事实；作业状态缺失为 `UNKNOWN`；不把节点 `LastInstanceStatus` 当作 `status`|
|节点统计|官方成功样例有节点，但没有 `nodes[].status`|缺少节点清单时节点数和异常数均为空；显式空清单为 0/0；清单存在但任一节点状态缺失或未知时异常总数为空，仍保留实际节点数量及有状态节点的独立事实|
|节点类型|节点字段 `type` 描述节点任务类型，字段表标必需，但成功样例未提供|作为节点 `type` 保留，不推断整个作业执行引擎；缺失兼容为空|
|实时作业时间|状态接口把 `startTime/endTime/lastUpdateTime` 定义为 `Date`；列表与实例表的时间类型不同|保留状态接口实际日期字符串或有效数值，不擅自按批实例毫秒时间戳解释字符串|
|实例状态|`waiting`、`running`、`success`、`fail`、`running-exception`、`pause`、`manual-stop`|保留小写枚举；不替换为实时作业的大写枚举；未知字符串不按成功处理，并记采集错误|
|实例身份与排序|`jobName`、`planTime`、`instanceId` 等字段用于定位；时间及 `instanceId` 表为 Long|关键定位字段无效时拒绝该工作空间快照，避免漏掉坏记录后把旧成功实例当最新；仍按作业取最新 `planTime`，相同计划时间按 `submitTime`、数值 `instanceId` 决定先后|
|实例运行时长|`startTime/endTime` 为毫秒时间戳；`executeTime` 表和样例矛盾，见下节|用有效起止差除以 60000，单位分钟，保留一位小数；只有 `running/running-exception` 且缺结束时间时用本轮时刻；缺开始时间、已结束状态缺结束时间、倒序或无效时间为空，不补 0|
|实例附属字段|`instanceType`、`submitTime`、`instanceId` 等有明确定义|保留到 `instance_type`、`submit_time`、`instance_id` 属性，便于区分普通、手工、补数据、子作业或一次性调度|
|作业引擎、历史基线|这三个响应表均未定义作业 `engine/jobEngine` 或 `p90_minutes`|仅兼容上游明确返回的扩展字段；不从节点类型推断引擎，不再将任意 30 分钟冒充 P90；没有实际基线时不提供 `facts.p90_minutes`|

作业名称、实例定位字段、节点名称等基础结构异常不会转换为“正常”或“空清单”。单个实时状态请求失败时保留其他成功作业，失败作业不伪造停机状态；继续保留线程日志上下文、HTTP 并发边界及既有云请求限流，没有变更 IAM、全局 10 QPS / 4 并发或服务限额。

### 不能从当前正文消除的差异

1. **耗时单位冲突。** 实例表把 `executeTime` 写为 Long 毫秒；成功样例却为 `0.3`，同一条记录 `1551671598000 - 1551671580000 = 18000` 毫秒，即 18 秒、0.3 分钟。不能据此认定生产 `executeTime` 一律为分钟或一律可靠。采集器使用起止时间，不使用该字段作备用耗时。
2. **时间筛选含义。** 参数名是 `minPlanTime/maxPlanTime`，但表中解释又称按实际开始时间筛选。采集器按官方参数名发送明确毫秒窗口，不在本地二次猜测过滤；真实边界是否包含端点、按计划还是实际开始筛选，需要目标修订的只读响应证实。
3. **必需字段与示例不一致。** 作业列表样例未给字段表标必需的 `id`；实时状态样例未给必需的 `startTime` 和节点 `type`。不因这些非定位字段缺失丢弃官方成功样例，也不据表格默认补值。
4. **实例并非仅独立批作业。** 功能段明确包含实时作业节点关联的调度子作业，名称形如“实时作业名_节点名”。本次保持既有 `dgc_batch_job` 台账和按作业聚合口径，不新增列表请求推断所属类型、不丢弃 `instanceType=6`；界面/说明不能把所有实例都解释成独立批作业。
5. **P90 不是当前三接口能力。** 没有官方基线字段，现有超 P90 规则在未获得真实基线时不能可靠评估。本次没有新增历史计算或改规则范围。
6. **翻页期间不提供快照令牌。** 文档没有固定快照或游标机制。保留检测到异常分页时失败的行为，但不能宣称无并发作业变动下的绝对完整性。

### 分组检查时的集成点（已整合，以下保留过程记录）

主线程已对齐 Mock 的页码/类型、合成替身的 total 与状态、MRS 显式毫秒契约及资源指南。最终 SQLite 全量 1258 passed / 21 skipped，PG16 全量 1264 passed / 15 skipped；下列早期失败均已修复，严格生产校验未放宽。

- `mock_hcs/app.py`：两个列表按 `offset * limit` 解释页号；实时列表默认应遵守 BATCH，实时请求必须显式 REAL_TIME。可选状态/节点示例应能覆盖缺失状态，不构造“缺失等于正常”。
- `backend/tests/test_collection_performance.py`：`JobClient` 当前使用记录偏移切片，需改页码切片；实时替身状态应使用 `NORMAL`，而非未定义的 `RUNNING`。
- `backend/tests/test_worker_business_logging.py`：DataArts 日志测试的作业列表替身应带必需 `total`，否则只会触发列表失败，无法继续验证逐作业失败的线程上下文和限量堆栈。
- `backend/app/tools/probe.py`：若发送这三个接口，应复用 V1 路径、显式实时类型、workspace header 和毫秒窗口；第一页 `offset=0` 仍正确。不要通过一次缺失可选节点状态的响应宣告节点健康能力完整。
- `backend/app/services/resource_guides.py`：删除“缺失 P90 回退 30”的旧说明；引擎属于未证实扩展，节点状态、耗时缺失应说明为空。

本次仅编辑采集器、本章文档和独立测试，没有修改上述集成文件。

### 回归验证

独立测试：`backend/tests/test_dataarts_hcs851_contract.py`。测试使用按上述三节正文构造的响应 fixture，以及 `httpx.MockTransport` 检查真实 HcsClient 的 URI、header、查询和编码，不依赖现有 Mock 的实现作契约判据。覆盖页号、工作空间、固定窗口、可选状态、结构错误、重复清单、优先级、耗时、最新实例和扩展字段；不访问真实云或生产数据库。

在 `backend` 目录执行，显式禁用环境配置文件与外部测试数据库：

```sh
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' .venv/bin/pytest tests/test_dataarts_hcs851_contract.py -o addopts='' -q
.venv/bin/ruff check app/collectors/dataarts.py tests/test_dataarts_hcs851_contract.py
INSPECTOR_ENV_FILE='' .venv/bin/mypy app/collectors/dataarts.py
git diff --check -- app/collectors/dataarts.py tests/test_dataarts_hcs851_contract.py ../docs/HCS8.5.1作业接口核对.md
```

结果：**76 passed**；Ruff 通过；mypy 通过（1 个源码文件）；差异检查通过。包含两个独立内存 SQLite `run_task` 回归：分别验证实时/批量两轮执行成功、资源数为 1、实际 API 次数为每轮 2/1，本轮未知指标覆盖旧台账值而不是沿用旧值或补 0。

另运行既有相关回归：

```sh
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' .venv/bin/pytest tests/test_collection_performance.py tests/test_pipeline.py tests/test_worker_business_logging.py -k 'dataarts or job_names or three_hundred_jobs or encoded_job' -o addopts='' -q
```

结果为 **4 passed、3 failed、27 deselected**，失败逐项交主线程整合：

- `test_three_hundred_jobs_use_bounded_parallel_requests`：旧 `JobClient` 用记录偏移，按官方页号调用后返回重复作业；应修替身分页，不放松生产完整性校验。
- `test_dataarts_detail_failures_are_bounded_and_thread_context_reaches_http`：旧列表替身缺 `total`，在列表阶段被拒绝；补 `total=9` 后才能验证原定逐项失败日志。
- `test_dataarts_and_mrs_pipeline`：两轮 DataArts 均为 `SUCCESS`；失败断言是随后 MRS 返回 `PARTIAL`，不属于本次 DataArts 写集，未修改。

未运行全库 gate、独立 PG 或目标云联调。上述失败不能据此声称全库通过，本地 fixture 通过也不等同于生产联调成功。
