# ManageOne 8.5.1 ECS 指标来源

本目录只记录官方来源与核验摘要，不包含华为原始文档或提取全文。原始 ZIP 和文档仅保留在 `/tmp/manageone851-public/`。这些文件标有版权声明；不要未经授权复制、发布或提交原件。

## 官方入口与下载

- 入口：<https://support.huawei.com/carrier/docview?nid=DOC1101451954&path=PBI1-253383977/PBI1-267026473/PBI1-21431666/PBI1-253386765/PBI1-23864287>
- 文档编号：`DOC1101451954`，标题《华为云Stack 8.5.1 API参考 03》，发布日期 2025-08-11。
- 直接下载：<https://download.huawei.com/dl/download.do?actionFlag=download&nid=DOC1101451954&partNo=6001&mid=SUP_DOC&_t=20250811204111000>
- 匿名下载核验：官方 metadata 返回 `securityId=SQTY-ANONY`；不带 Cookie/Authorization 下载返回 HTTP 200、154936736 字节、有效 ZIP。
- ZIP SHA-256：`0654b0b6ab944f42496f35db16796ceda0a98893db66f2384e0e8602a53ff021`

## 核验文件

以下均为官方 ZIP 中的原始成员。提取副本在临时目录，未复制到仓库。

| 官方文件名 | SHA-256 | 核验位置 |
|---|---|---|
| `ManageOne 8.5.1 运维面API参考 03.docx` | `99482786d4b5bb27c4ac5acdaf06957ab2f44f623f3c18ef2aafb76cdf30c41d` | `7.1 认证用户`；`租户保障 > 查询指定CI实例数据`；`性能监控 > 查询历史性能数据`、`获取所有监控对象类型`、`获取监控对象类型支持的监控指标`、`获取监控指标` |
| `ManageOne 8.5.1 OTenantSecurity服务API参考 01.docx` | `f697e4ad56e640c2a704da08c27de2e5ab7ab5828878f8acb29acca034039be3` | `获取用户Token`：`POST /v3/auth/tokens`，响应头 `X-Subject-Token` |
| `ManageOne 8.5.1 性能指标统一模型 01.xlsx` | `30711abbd2a3cd6f3788bfb0ebdbb44351018414813dc363bbc1f726655872c0` | `资源类型` 第 42 行；`采集指标` 第 126、127、130 行 |
| `ManageOne 8.5.1 资源北向模型 02.xlsx` | `bf1f7395c160d3f5ba5b5bccc3da587d494edca1f05160cc188cb8b84fb5575f` | `CLOUD_VM`：`resId`、`nativeId`、`projectId`、`tenantId`、`ownerId`、`ownerType` |
| `ManageOne 8.5.1 数据集说明文档 03.docx` | `bd1d1973263ff51b40f8ff949718f58a3161473a40e8e61fd94aeca8cdecf670` | `perf-ecs`：`cpuUsage`、`memoryUsage`、`diskUsage` 与分开的磁盘 I/O 指标 |

## 契约事实

- `CLOUD_VM` 的类型 ID 为 `562958543355904`，数据集为 `perf-ecs`。
- FusionSphere OpenStack 来源的 CPU、内存、云硬盘使用率指标分别为 `cpuUsage` (`562958543421441`)、`memoryUsage` (`562958543486979`)、`diskUsage` (`562958543618052`)；单位均为 `%`，模型标记主动上报、周期 1 分钟、HCS 支持、不支持实时数据。
- 资源查询的只读入口为 `GET /rest/tenant-resource/v1/tenant/resources/CLOUD_VM`，分页使用 URL query 参数 `limit`、`offset`；响应以 `nativeId` 对应输入 ECS ID，并要求 `projectId` 完全等于当前项目，性能请求的 `obj_ids` 使用唯一 `resId`。资源查询权限为“查询实例详情”。
- 性能查询为 `POST /rest/performance/v1/data-svc/history-data/action/query`，请求 `obj_ids` 使用资源 `resId`，不是 ECS `nativeId`；每次对象数乘指标数不超过 100。文档接受 `IAM_OP` 或 `SSO` token。普通租户 IAM token 不能按文档推定为 `IAM_OP`。
- 推荐的三方运维用户认证为 `PUT /rest/plat/smapp/v1/sessions`，请求 `grantType=password`、`userName`、`value`，可选 `organizationName`；响应 `accessSession` 用于后续 `x-auth-token`，非 GET 请求携带 `roarand`。不转发现有 HCS IAM 凭据。
- 资源模型将 `ownerId` 描述为“来源系统ID”、`ownerType` 描述为“系统类型”，但没有给出其取值到性能模型 `Source System Type=FusionSphere OpenStack` 的映射。实现仅把两字段非空作为来源元数据存在性检查，不猜测二者取值。现网来源仍需独立确认。
- `memoryUsage`、`diskUsage` 的正式名称与单位支持展示其使用率；文档没有定义分母、来宾/挂载点聚合口径或已用字节。磁盘 I/O、I/O 利用率不是磁盘空间使用量。`boot_time` 没有本来源指标映射。

以上是文档核验及实现约束，不代表已连接或验证生产环境。实际启用前仍需确认 endpoint、独立凭据、权限、项目映射、来源系统及真实采样。
