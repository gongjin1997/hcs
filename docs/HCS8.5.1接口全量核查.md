# HCS 8.5.1 上游接口全量核查

> 2026-11-02 最新策略（`20261102-aom-disabled-1`）：所有资源暂时停用 AOM。本文此前（含同日早期）关于显式 AOM、映射编辑、探测或查询可用的结论仅保留为历史审计，已由 [AOM停用说明](AOM停用说明_20261102.md) 覆盖；无正式 HCS 调用证据，Mock 成功不代表生产可用。

2026-10-28 上传 PDF 补充核查及新增修复见 [上传 PDF 与现场日志核查](HCS8.5.1上传PDF核查.md)。其中 GaussDB v3.1 的内存 GB 单位已由上传版本证实，不推广到默认 v3.3 等接口族；此前公开文档的版次和页码不替代上传文件的版次和实际 PDF 页码。

核对日期：2026-10-27。范围：14 类台账资源、全部现有 API 采集任务，以及 IAM、AOM、CCE/Kubernetes 依赖。用户所述“AP 任务”按现有 API 采集任务检查。

核查依据是明确标注 HCS 8.5.1 的官方组件文档，逐项检查方法、URI、Endpoint、项目作用域、认证、参数、分页、响应字段、状态与单位。Mock 只验证实现，不证明真实平台支持。没有取得版本正文的接口保留待确认，不以公开云或 8.5.0 文档替代。

## 台账接口

| 资源 | 当前只读接口 | 本轮结论 |
| --- | --- | --- |
| ECS | GET `/v1/{project_id}/cloudservers/detail` | 已核对；offset 为页号，count/servers；vCPU 核数、RAM MB；磁盘不从无依据的 flavor 字段推算 |
| CCE | GET `/api/v3/projects/{project_id}/clusters`，以及 `/api/v3/projects/{project_id}/clusters/{cluster_id}/nodes` | 已核对；items、metadata.uid；控制面未声明分页，不套用 Kubernetes continue；节点失败不补成零节点 |
| EVS | GET `/v2/{project_id}/cloudvolumes/detail` | 已核对；同册也支持 volumes/detail，不认定两者互斥；marker/volumes_links 续页；没有假设 count；不猜测加密状态 |
| VPC | GET `/v1/{project_id}/vpcs` | 已核对；limit/marker；保留原生 ID |
| 子网 | GET `/v1/{project_id}/subnets` | 已核对；limit/marker；不混淆 id 与 neutron_subnet_id |
| ELB | GET `/v3/{project_id}/elb/loadbalancers` | 已核对；移除文档未声明的 limit/marker；核对 page_info.current_count；next_marker 是末条 ID，不直接解释为存在下一页；guaranteed 不代表物理独占 |
| EIP | GET `/v1/{project_id}/publicips` | 已核对；不替换成 Neutron floatingips；带宽为 Mbit/s |
| RDS | GET `/v3/{project_id}/instances` | 已核对；记录 offset、limit≤100、total_count；CPU 核数、内存和存储 GB，不乘节点数 |
| GaussDB | 默认 GET `/gaussdb/v3.3/{project_id}/instances` | 已修复无前缀的旧路径；台账、采集、连接检查、Probe 共用版本选择；可显式选 v3.2/v3.1/opengauss_v3，不自动试探；flavor_info.vcpu 有核数依据，内存/磁盘原值单位未明时不写 GB |
| DCS | GET `/v2/{project_id}/instances` | 已核对；记录分页；小于 1 GB 时兼容 capacity_minor；统计字段见下表 |
| Kafka | GET `/v2/{project_id}/instances?engine=kafka` | 已修复单次默认 10 条造成漏读；每页≤50，offset 为记录偏移；instance_num 不能作为已证实的总数终止条件 |
| MRS 集群 | GET `/v1.1/{project_id}/cluster_infos` | 已核对；currentPage 从 1、pageSize=50、字符串 clusterTotal；createAt/updateAt 为秒 |
| DWS | GET `/v1.0/{project_id}/clusters` | 已核对；使用 endpoints[].connect_info，不读取不存在的 private_ip；不由节点数虚构容量 |
| CSS | GET `/v1.0/{project_id}/clusters`（既有兼容适配） | **待确认组件版本**；未取得明确 HCS 8.5.1 正文，不把现有 start/limit/totalSize 或状态映射标成版本认证结论 |

台账同步仍按类型保存完整快照：异常分页、重复标识、结构错误或后续请求失败，均不得据部分清单判定资源下线。未知容量与状态不补零或“正常”。

已修复历史值残留：GaussDB 无依据的旧内存/存储 GB、ELB 旧“独享”规格与缺失成员数、EVS 未证实的加密结论、DWS 错误来源的内网 IP，均由适配器显式声明清理并记录变更。普通缺失字段保留原同步口径；不会全局清空未知值。GaussDB 状态采集也会清除已标记单位未核实的旧容量，并从已确认的部署形态和 vCPU 重建规格，不残留旧 GB 文本。

新增 8 项回归覆盖已有数据、跨项目隔离、变更记录、重复同步幂等、普通缺失值保留及无效清理声明拒绝。

## 采集任务及共用接口

| 任务/依赖 | 当前接口与处理 | 结论 |
| --- | --- | --- |
| ECS 状态 | 原生实例清单；性能仅来自显式配置的指标源 | 路径/页号已核对；缺失本轮指标清空旧值 |
| GaussDB 状态 | 共用版本化实例路径；性能指标须显式映射 | 路径与结构已修复；不以节点数推算 CPU/内存使用率 |
| DCS 状态 | GET `/v2/{project_id}/instances/statistic`，每项目批量一次 | 原生内存 MB；used_cpu 是累计秒数，不是 CPU%；不累计节点连接数；可选 AOM 映射单独处理 |
| DataArts 实时 | GET `/v1/{project_id}/jobs?jobType=REAL_TIME`，GET `/v1/{project_id}/jobs/{encoded_name}/status` | 已修复 offset 页号；workspace header、名称编码、必需 total、节点未知状态均验证 |
| DataArts 实例 | GET `/v1/{project_id}/jobs/instances/detail` | 已修复 offset 页号；固定 24 小时毫秒窗口；耗时用有效起止时间，不使用单位矛盾的 executeTime；缺失 P90 不填 30 |
| MRS 作业 | GET `/v2/{project_id}/clusters/{cluster_id}/job-executions`（兼容适配） | **851 契约待确认**；明确记录 unverified_hcs851_compatibility。未知 elapsed_time 单位不换算；当前 offset 模式不宣称获得厂商证明 |
| CCE Pod/Event | core/v1 pods、events，以及显式配置的只读 pods/proxy | 已核对 ListMeta、原样 continue、resourceVersion、410、事件时间/次数和 RBAC；未把证书直连认证替换成 IAM |
| AOM 元数据 | POST `/v1/{project_id}/ams/metrics` | 已核对 camelCase、limit/start、metaData.start；显式 namespace/指标/维度，拒绝歧义映射 |
| AOM 样本 | POST `/v2/{project_id}/samples?fill_value=null` | 已核对 snake_case、秒 period、毫秒 timestamp、每批 20、单段≤1440 点；不补零；缓存、请求预算与部分失败保留 |
| IAM | POST `/v3/auth/tokens`；AK/SK 使用相应签名认证 | Token 按真实 expires_at 缓存，进程内及加密 Redis 合并刷新；401 按代次处理，不逐业务接口换 Token |

CES 默认禁用，AUTO 不探测 CES。局点 AOM 的 Endpoint、namespace、指标及实例维度必须显式配置。无历史接口证据时，DCS 本地历史只保存真实接口采样，不把当前值回填到过去。

## 尚未确认的两项

1. **CSS**：需要局点组件版本及对应接口章节；HCS 8.5.1 文档目录未找到正面证据，不等于已证明该服务不存在。
2. **MRS 作业**：已定位 3.5.1-LTS 的 JobGateway 专版 `EDOC1100453451`，正文要求客户/合作伙伴访问。公开 8.5.1 使用指南的 YARN 原生接口是 Kerberos/SPNEGO、apps.app[]，不能只替换 URL 继续用 IAM；8.5.0 V1/V2 也不能直接作为 851 证明。需要脱敏的查询、分页、鉴权章节后再完成专版适配。

## 回归结果与边界

- SQLite 全量：1258 passed、21 skipped、4 subtests；覆盖率 90.98%（门槛 88%）。
- PostgreSQL 16 独立容器全量：1264 passed、15 skipped、4 subtests。
- 前端：25 suites、451 tests，vue-tsc 与生产构建通过。
- Ruff 与 mypy（151 个源码文件）通过；跨进程 Redis 专项另有 51 项真实 Redis 回归。
- 所有测试使用独立数据库、公开文档 fixture、Mock HCS 与本地邮件环境；没有对真实 HCS 做压测，也没有把 Mock 结果写成生产联调结论。
- 局域网已更新并完成实际调用验证，详见《系统检查记录》：台账 196 个资源、按租户开关检查 8 类任务、每份台账 XLSX 15 个工作表；GaussDB 使用原生版本路径，CES 调用为 0。

## 版本正文与逐项依据

- [资源服务 API 证据](HCS8.5.1资源服务API证据.md)：ECS、EVS、VPC、EIP、ELB、RDS、Kafka、DWS 等版本、字段和单位。
- [台账接口核对](HCS8.5.1台账接口核对.md)：14 类资源、分页、GaussDB 路径和容量边界。
- [DataArts 作业接口核对](HCS8.5.1作业接口核对.md)：三条接口与厂商正文冲突。
- [MRS 与 CCE 接口核对](HCS8.5.1MRS与CCE接口核对.md)：MRS 待确认项、CCE 控制面及 Kubernetes 授权和分页。
- [IAM、DCS、AOM 接口核对](HCS8.5.1接口核对.md)：Token、原生 DCS 与显式 AOM 查询。
