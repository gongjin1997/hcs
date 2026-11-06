# HCS 8.5.1 MRS 与 CCE 接口核对

> 2026-11-02 最新策略（`20261102-aom-disabled-1`）：所有资源暂时停用 AOM。本文此前（含同日早期）关于显式 AOM、映射编辑、探测或查询可用的结论仅保留为历史审计，已由 [AOM停用说明](AOM停用说明_20261102.md) 覆盖；无正式 HCS 调用证据，Mock 成功不代表生产可用。

核对日期：2026-10-27。范围：MRS 集群与作业巡检、CCE 集群/节点台账接口、原生 Kubernetes Pod/Event 与 Pod 指标代理。

本次仅访问公开官方文档，在独立浏览器会话读取正文。未访问真实云平台、凭据、现有业务数据库或局域网服务。

## 文档版本与证据边界

| 文档 | 本次实际网页标题 | 网页更新日 | 核对内容 |
| --- | --- | --- | --- |
| EDOC1100452559 | MapReduce Service (MRS) 3.5.1-LTS Usage Guide **(for Huawei Cloud Stack 8.5.1) 11** | 2026-07-20 | API Overview、API V1 / Cluster Management、API V2 目录 |
| EDOC1100453109 | Cloud Container Engine (CCE) 25.3.0-HCS Usage Guide **(for Huawei Cloud Stack 8.5.1) 05** | 2026-09-01 | Cluster Management、Node Management、Kubernetes APIs、Authentication、Permissions and Supported Actions |
| Kubernetes 官方仓库 | **v1.23.17** 的 `api/openapi-spec/swagger.json` | 固定版本文件 | 原生 Pod/Event 路径、ListMeta、分页与 EventSeries 定义 |

搜索摘要可能仍显示 MRS 旧版编号或 CCE 04；本次以浏览器中实际打开的完整标题为准。CCE 产品版本不等于集群 Kubernetes 版本：官方示例出现 v1.25，本次使用固定 v1.23.17 OpenAPI 交叉核对已有原生 v1 字段，不能据此声称现网集群必为这两个版本。升级集群仍须核对该集群实际 API discovery 与权限。

按官方产品页 `huawei-cloud-stack-pid-23864287` → 8 → 8.5.0 → 8.5.1 → User Guide / More 重新核验，当前目录列出的 MRS 与 CCE 文档 ID 和上表一致。该筛选同时列出继承的 MRS 3.5.0-LTS **for 8.5.0**（EDOC1100403830）与 CCE 24.9.0-HCS **for 8.5.0**（EDOC1100403286）；不能只看筛选项就把这些旧版正文称为 8.5.1 原生契约。产品目录留证在 `product-851-guides.json`、`product-851-guide-identities.json`。

正文的本地留证在 `/tmp/hcs851-mrs-cce/`：`mrs-overview.json`、`mrs-v1-cluster.json`、`mrs-cluster-list.txt`、`mrs-v2-cluster.json`、`cce-clusters.json`、`cce-cluster-list.txt`、`cce-nodes.json`、`cce-kubernetes.json`、`cce-auth.json`、`cce-permissions.json`、`k8s-v1.23.17-openapi.json`。这些文件仅含公开厂商资料，临时目录不作为仓库的永久依赖。

## MRS

### 集群列表：已在 8.5.1 正文核实

```text
GET /v1.1/{project_id}/cluster_infos
    ?pageSize={page_size}&currentPage={current_page}
```

- `currentPage` 是页码；`pageSize` 是每页最多集群数。代码从第 1 页开始，页大小 50，使用共享分页器的 page 模式，不能改为记录偏移。
- 响应字段为 `clusters` 和 **字符串** `clusterTotal`；不能仅因为某页短于请求页大小就忽略已报告的总数。
- `clusterId` / `clusterName` 是身份字段。标识缺失、重复、清单缺失、总数缺失及分页提前结束均报错，不作为“没有集群”的正常快照。
- `masterNodeNum`、`coreNodeNum`、`totalNodeNum` 是字符串计数；`clusterVersion` 为原始版本值。
- `createAt` / `updateAt` 的正文定义为 10 位时间戳，即秒，不是毫秒；台账转换由 `ledger/fetchers.py` 所属主线程维护。
- 响应的 `clusterState` 枚举为 `starting`、`running`、`terminated`、`failed`、`abnormal`、`terminating`、`frozen`、`scaling-out`、`scaling-in`。保留小写原值。查询过滤参数另有 `existing`、`history`、`partial-error` 等值，不能把所有过滤值直接当成响应状态枚举。

合成结构样本（标识不来自现网）：

```json
{
  "clusterTotal": "1",
  "clusters": [{
    "clusterId": "synthetic-cluster-a",
    "clusterName": "synthetic-mrs-a",
    "clusterVersion": "MRS 3.5.1-LTS",
    "clusterState": "running",
    "totalNodeNum": "5",
    "createAt": "1710000000"
  }]
}
```

### V2 作业列表：不能标成已验证的 8.5.1 原生接口

现有适配调用：

```text
GET /v2/{project_id}/clusters/{cluster_id}/job-executions
```

**本次打开的 HCS 8.5.1 MRS API Overview 没有列出此接口，API V2 目录也没有 Job Management 章节。** 其 V2 目录为 Cluster Management、Cluster HDFS File、Agency Management、IAM Synchronization；V1 目录为 Cluster Management、Tag Management APIs。不能因为其他华为公有云区域具有同名接口，就把它当成该局点已确认的契约；缺少公开条目也不能反向证明所有局点补丁都绝不支持。

因此，代码将现有路径保留为兼容适配，并在运行统计中记录 `job_api_contract=unverified_hcs851_compatibility`。需要现场核对服务组件、补丁、接口响应与分页语义后才能确认可用；没有改为臆造的 Manager、YARN 或 AOM 路径。

| 项目 | 当前处理 | 尚需局点证明 |
| --- | --- | --- |
| `offset` / `limit` | 保留已有 page 模式，`offset` 从 1 开始；可显式设置 `job_offset_mode=offset` 表示从 1 起的记录偏移，按实际返回条数推进 | 同名参数并不能证明其表示页码还是记录偏移。当前默认仅是既有兼容行为，不是 851 认证结论；从 0 起的其他变体不在本适配的确认范围 |
| `submitted_time_begin` | 现有兼容请求保持 UTC 毫秒时间窗；同轮分页复用同一个起始时间 | 需局点确认该请求字段、单位与筛选边界；不以公有云文档当作 851 证明 |
| `job_list` / `total_record` | 两者必须存在且结构有效；缺失总数、提前空页、重复页或后续页失败均不能返回该集群的部分作业清单 | 局点实际响应是否为同一契约 |
| 最近一次执行 | `submitted_time` 支持非负有限数值与数值字符串，按数值比较 | 不按字符串字典序选取；字段缺失时该集群报采集缺口 |
| `elapsed_time` | 保存原值；没有明确单位时 `facts.elapsed_min`、`metrics.elapsed_min` 都为 null，并报告采集限制 | **没有证据就不默认毫秒，不再以缺失值补 0** |
| 明确的时长单位 | 任务 `params.elapsed_time_unit` 仅接受 `milliseconds` / `seconds`；分别除 60000 / 60。开发 Mock 的合成契约单独按毫秒处理 | 这些是显式配置的适配约定，不是厂商已确认的 851 单位；真实局点不得靠 Mock 结果确认 |
| 起止时间 | 兼容读取原始 `started_time` / `finished_time`，保留旧 `start_time` / `end_time` 别名 | 不对未知字段单位擅自换算 |
| 状态 | `job_state` 与 `job_result` 分开保留；运行中 `UNDEFINED` 结果不得覆盖 `RUNNING` 执行状态 | `ACCEPTED/RUNNING/FINISHED/FAILED/KILLED`、`UNDEFINED/SUCCEEDED/FAILED/KILLED` 等为既有兼容值；本次未找到它们在 851 作业接口中的版本正文证明，不声明穷举 |
| 服务不存在 / 无权限 | HcsError 写入该集群的采集错误，不能当作健康空作业列表 | 不能用重试或改走 CES 掩盖无服务、无权限或不匹配版本 |

当前只开放 AOM 的任务编辑接口并不自动开放这些兼容参数。主线程如需在产品中开放 MRS 局点契约配置，应增加有限字段校验及明确的确认流程，不能开放任意 params、URL、请求头或凭据。本组未修改任务配置 API 或现有数据库。

### 作业专版文档续查与整合结果

已定位官方 MRS 产品页的 **MRS 3.5.1-LTS JobGateway Rest API Reference (for Huawei Cloud Stack 8.5.1) 01**，文档 ID **EDOC1100453451**，目录日期 2025-03-31。条目标为客户/合作伙伴访问，匿名会话未取得正文。本次没有登录或绕过限制，不能从提交 POST 示例推导查询 GET、分页或鉴权算法。

公开 851 使用指南 `EDOC1100452559` 的 JobGateway 章节示例为 `POST /v2/job-executions`，使用 `JobServerAuthorization`，并非目前 project-scoped 云管查询的证明。其 YARN 开发指南明确的原生查询是 `GET /ws/v1/cluster/apps/`、`apps.app[]`，安全模式使用 Kerberos/SPNEGO；并非 IAM Token。Apache Hadoop 3.3.1 对应规范明确 elapsedTime 为毫秒，但 limit 是返回总数上限，没有 offset/continue/total_record 分页契约。没有实现一个仅替换地址的自动降级。

同时读取了 **for HCS 8.5.0** 的 `EDOC1100403830`：V2 job-executions 的 elapsed_time 明确毫秒、offset 默认 1 但未证明页码/记录语义；V1 `/v1.1/{project_id}/job-exes` 则为 current_page/page_size、job_executions/totalRecord、spend_time 秒及不同枚举。两个契约不能互套，更不能冒称 3.5.1-LTS 已验证。

后续需要局点组件版本、获授权的 JobGateway 查询/分页/鉴权章节脱敏文字。现有兼容适配继续保留未知契约标记；没有新增凭据或未经证实的原生接口调用。

主线程已完成台账、Mock 和既有合成测试整合：未知时间单位不换算，Mock 合成毫秒显式声明，缺失状态与不完整分页的严格校验保留。后端 SQLite 全量 **1258 passed / 21 skipped**、PG16 全量 **1264 passed / 15 skipped**；这些是隔离回归，不是 MRS 真实局点验证。下文的旧失败条目属于整合前过程记录。

官方入口与章节：

- https://support.huawei.cn/enterprise/en/cloud-computing/mapreduce-service-pid-22396131
- https://support.huawei.cn/enterprise/en/doc/EDOC1100453451
- https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/8b6d5aeb/using-jobgateway#EN-US_TOPIC_0000002302154740
- https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/6a2d5f38/yarn-development-guide#EN-US_TOPIC_0000002336181281
- https://support.huawei.cn/enterprise/en/doc/EDOC1100403830/cad45b1c/job-objects#EN-US_TOPIC_0000002107926213
- https://support.huawei.cn/enterprise/en/doc/EDOC1100403830/23b7fe29/job-objects-not-recommended#EN-US_TOPIC_0000002072126550
- https://hadoop.apache.org/docs/r3.3.1/hadoop-yarn/hadoop-yarn-site/ResourceManagerRest.html#Cluster_Applications_API

## CCE 控制面

以下两条均在 8.5.1 正文中核实：

```text
GET /api/v3/projects/{project_id}/clusters
GET /api/v3/projects/{project_id}/clusters/{cluster_id}/nodes
```

- 集群列表响应包含 `kind`、`apiVersion`、`items`。`kind` 的官方示例是 **Cluster**，不能强制要求为 Kubernetes 的 List；节点列表的 `kind` 是 **List**。
- 集群列表的可选查询为 `detail`、`status`、`type`、`version`。这两条控制接口的正文没有声明 `offset/limit` 或 `metadata.continue` 分页；不得把原生 Kubernetes 分页参数套上去。
- `metadata.uid` 是集群/节点 ID。资源名称不能代替 ID，读取节点时必须使用本项目列出的集群 ID。
- `status.phase=Available` 才进入当前 Pod/Event 采集。缺失 status/phase 不再默认为 Available；Unavailable、Creating、Hibernation 等状态会记录“未采集”，不伪报容器健康。
- 如果控制面返回未核实的 continuation，本组容器采集会报不完整清单，不自行猜测其分页协议。台账读取同类响应时应采用同样边界。
- `creationTimestamp` / `updateTimestamp` 是字符串；官方示例包含 `2018-08-02 03:48:58.968214406 +0000 UTC`，不能只假设固定 RFC3339 格式。台账解析由主线程负责。
- IAM 控制面动作：集群列表 `cce:cluster:list`，节点列表 `cce:node:list`。这不等于 Kubernetes RBAC 权限。

合成响应结构：

```json
{
  "kind": "Cluster",
  "apiVersion": "v3",
  "items": [{
    "metadata": {"uid": "synthetic-cluster-a", "name": "synthetic-cce-a"},
    "spec": {"version": "v1.25", "flavor": "cce.s2.small"},
    "status": {"phase": "Available"}
  }]
}
```

## Kubernetes Pod / Event / proxy

### 接入与授权范围

HCS 8.5.1 正文区分两种方式：

1. API Server 直连：由集群提供的地址、CA 和客户端证书访问；不能直接把本项目 IAM Token 当成通用直连凭据。本项目此次没有新增证书下载、管理或直连能力。
2. API Gateway：使用文档给出的 `https://{clusterid}.Endpoint/uri` 结构。Endpoint 以局点实际配置为准，不自动猜测域名。本项目的 `cce_k8s` 是此方式，通过现有统一客户端复用租户认证、限流、重试和请求预算。

所需 RBAC：跨命名空间 `list pods`、`list events` 需要相应集群范围授权；仅有某一个 namespace 的 RoleBinding 并不允许当前全命名空间采集。指标探针额外需要 `get pods/proxy`。不能仅因拥有 CCE 控制面只读 IAM 权限就判定这三项可用。只授予实际需要的 namespace 或集群范围；不建议为了探针直接授予 cluster-admin。

HCS CCE Authentication 正文要求缓存 IAM Token，使用 project scope，并支持 Token / AKSK；复用当前项目级认证与缓存即可，不因每个 Pod 或每一页请求重新取 Token。

### 路径与分页

```text
GET /api/v1/pods
GET /api/v1/events
GET /api/v1/namespaces/{namespace}/pods
GET /api/v1/namespaces/{namespace}/events
GET /api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy/{metrics_path}
```

- Pod/Event 是 **core/v1** 路径，不混用 `/apis/events.k8s.io/v1` 的 `regarding/note/deprecatedCount` 字段。
- `limit=500`，后续页原样回传响应 `metadata.continue`；该值是不可解析的 token，不是页码。请求范围、筛选条件和页大小保持不变。
- 短页甚至空页只要带 continue 就须继续。有效的空清单和没有 items 字段是不同情况。
- 对已有 `metadata.resourceVersion` 的分页检查同一快照版本；版本变化、重复 token、超过 50 页 / 25000 条或后续页失败均报不完整，不让部分清单触发资源下线。
- 410 ResourceExpired 不拼接新旧快照，不使用错误响应附带的新 token 继续已开始的清单；当前任务报告失败，后续调度轮次从头读取。403 / 404 同样不降级为健康空清单。
- Pod 缺少名称或 namespace、Event 缺少 UID / involvedObject、错误的 ListMeta 或 series 结构均报错误，不能按空资源处理。
- proxy 是 GET 子资源，不调用 exec/attach。指标路径不能包含查询参数、fragment、路径跳转、控制字符等；端口范围 1–65535。文本指标值保留有限数值，单位由显式探针配置定义，不从指标名推断为百分比。

### 时间与次数

core/v1 Event 中 `eventTime` 是首次观测时间，`series.lastObservedTime` 是聚合事件最后发生时间，`series.count` 是聚合次数；旧 `firstTimestamp` / `lastTimestamp` / `count` 字段仍需兼容。代码优先采用 series 的最后时间与次数，再兼容旧字段。若没有任何有效事件时间，不再用采集当前时间伪造新事件，而是记录采集限制。

`restartCount` 是容器累计重启次数，不是每分钟重启次数。Kubernetes Event 本身具有保留期限，是补充性事件流；仅靠 list events 不保证恢复所有历史事件。本项目的本地事件留存不能宣称等同集群完整历史审计。

## 本组变更、测试和主线程联动

写入范围：`collectors/mrs.py`、`k8s/kube.py`、预先说明必要性的 `k8s/collector.py`、两个新测试文件和本文。没有修改 `ledger/fetchers.py`、Mock、Probe、共享 pager、任务 API、认证、AOM、限流设置或部署。

新增隔离测试：

```bash
cd backend
env -u INSPECTOR_TEST_DATABASE_URL INSPECTOR_ENV_FILE= INSPECTOR_LOG_DIR= \
  .venv/bin/pytest -o addopts='' tests/test_mrs_hcs851_contract.py \
  tests/test_cce_hcs851_contract.py -q --tb=short --show-capture=no
```

结果：**111 passed**。覆盖官方原生结构、字符串总数、分页保全、时长单位未知、显式兼容单位、数值字符串排序、状态保留、403/404/410、代理路径、集群状态、事件 series、时间缺失和作用域字段。与 `test_pod_scope_isolation.py`、`test_pod_scope_migration.py`、`test_review_collection_paging.py`、`test_no_ces_defaults.py` 合并回归为 **235 passed**。

相关旧回归曾运行 133 项，结果 131 passed、2 failed：

- `tests/test_job_collection_details.py::test_mrs_running_state_is_not_hidden_by_undefined_result`：合成 DetailClient 没有声明时长单位，仍期待 120000 自动变成 2 分钟。
- `tests/test_pipeline.py::test_dataarts_and_mrs_pipeline`：合成 build_client 配置没有显式 Mock 标记或任务单位，仍期待无单位的 MRS 采集为 SUCCESS。

这两项须由主线程把合成契约明确配置为 milliseconds，并保留“真实未知单位不换算”的新回归，不能为恢复旧断言而重新给生产接口默认毫秒。本组未跨写授权之外的旧测试。

需要主线程对齐：

- **台账 / Euclid**：MRS 使用 currentPage/pageSize 和字符串 clusterTotal；CCE 控制列表不猜分页，未知 continuation 必须报不完整；时间解析须兼容上述秒时间戳与 Go 风格时间字符串；节点失败不能被当作 0 节点。
- **Mock**：MRS job-executions 仍属合成兼容契约，应实际应用所声明的 offset/limit 和 submitted_time_begin，防止一个忽略分页/筛选的 Mock 掩盖缺陷；合成时长单位可以明确为毫秒，不能用 Mock 存在证明真实 851 有此接口。
- **Probe / 共享 client**：MRS 作业端点不得标注为“8.5.1 已验证”；CCE gateway 仍使用同租户、同项目认证与显式 endpoint。403/404 应显示权限/服务缺口，不能报正常空数据。保持当前 Token 缓存、无 CES 默认请求、AOM 显式配置及全局 10 QPS / 4 并发。
- **配置 / 指南**：若产品开放 MRS 局点兼容配置，应通过有限字段校验；资源指南现有“缺失 elapsed_time 时 facts 为 0”与固定 /60000 文案需要同步修正。

## 官方来源

- [M1] MRS 8.5.1 API Overview：<https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/501b825e/api-overview>
- [M2] MRS 8.5.1 集群列表正文：<https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/1c9c9c61/cluster-management#EN-US_TOPIC_0000002301999772>
- [M3] MRS 8.5.1 API V2 目录：<https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/6cf668ee/api-v2>
- [M4] MRS 8.5.1 V2 Cluster Management 正文：<https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/7817e148/cluster-management>
- [C1] CCE 8.5.1 集群列表正文：<https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/daeb8233/cluster-management#cce_02_0239>
- [C2] CCE 8.5.1 节点列表正文：<https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/f318fe13/node-management#cce_02_0244>
- [C3] CCE 8.5.1 Kubernetes APIs：<https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/4dc4b64b/kubernetes-apis>
- [C4] CCE 8.5.1 Authentication：<https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/73b35fbe/authentication>
- [C5] CCE 8.5.1 Permissions and Supported Actions：<https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/dacad2df/permissions-and-supported-actions>
- [K1] Kubernetes v1.23.17 OpenAPI，固定版本：<https://raw.githubusercontent.com/kubernetes/kubernetes/v1.23.17/api/openapi-spec/swagger.json>
- [K2] Kubernetes API Concepts：<https://kubernetes.io/docs/reference/using-api/api-concepts/>

[K2] 是补充概念文档，不用于推定 HCS 已部署的 Kubernetes 版本；具体原生字段以 [K1] 固定版本和局点实际版本核对。公有云区域的 MRS `mrs_02_0087.html` 只能作为接口同名的对照资料，未作为本次 HCS 8.5.1 的确认来源。
