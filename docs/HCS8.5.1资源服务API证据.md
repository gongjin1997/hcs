# HCS 8.5.1 资源服务 API 证据

本文“源码比对快照”为修改前留证。GaussDB 版本路径、DWS 内网字段、Kafka 分页等已按证据修复，当前结果见 [接口全量核查](HCS8.5.1接口全量核查.md)。

核对日期：2026-10-27。仅核对华为公开一手文档，未连接真实云、LAN 或数据库，未读取 `.env` 或生产凭据。本文记录的是指定产品/补丁的文档契约，不代表现网联调通过。适配源码仅只读对照 `backend/app/ledger/fetchers.py`、`backend/app/hcs/pager.py`；修复由主线程/Euclid负责。

## 证据采用规则

- “已核实”必须在正文/文档封面与所属指南确认 **for 华为云Stack 8.5.1 / for Huawei Cloud Stack 8.5.1**。搜索摘要、公有云 API、新版 HCS 8.6 文档仅作定位线索。
- 保留服务版本、修订号、具体章节和单位原文。文档冲突、未列字段、容量聚合范围未知均明确写出；不把空值补成 0，也不根据示例数值发明单位。
- 本文的列表契约不能证明所有可选字段每次必有。未实际授权/测试的端点、补丁、权限与产品变体均保留边界。
- 独立测试仅保护已确认的静态调用契约与证据约束，不导入应用，不使用 Mock 产生的响应证明上游契约，不声称真实云验收通过。

## 高风险服务

### EVS：普通 EVS 与 for 云块存储分别核对

**普通EVS与云块存储专版各自的正文都同时列出** `volumes/detail` 和 `cloudvolumes/detail`，不能把路径区别简化成产品互斥。另已读到更新后的 **EVS 8.5.1 for云块存储01** 网页 [EVC4]（2025-08-11），其cloudvolumes与volumes章节继续同时存在，旧25.3.0与新版8.5.1应分别登记。

| 来源 | 已确认契约 | 边界/冲突 |
| --- | --- | --- |
| [EVC0] 云硬盘(EVS) **25.3.0 API参考(for 云块存储)(for 华为云Stack 8.5.1) 01**；PDF封面 2025-03-30；官网指南发布时间 2025-04-14，EDOC1100455595 | [EVC1] §5.4.3，印刷页90–95，表5-122至5-127：`GET /v2/{tenant_id}/volumes/detail`；Query 有 `marker`、`limit`、`offset`；成功示例 `volumes[]` 和 `volumes_links[]` | `marker` 为上一页最后记录的 id；`limit>0`；`offset` 描述为大于0且小于卷总数的记录偏移，不是页号。不能据此把首次请求强改为1，跳过首条；应省略首个偏移或核实局点对0的支持 |
| 同书 [EVC2] §5.6.20，页184–188；[EVC3] §5.6.28，页216–221 | 分别为 `GET /v2/{tenant_id}/volumes/detail`、`GET /v2/{tenant_id}/cloudvolumes/detail`；§5.6.28同样支持 `marker/limit/offset`，示例列表key是 `volumes`，还有 `volumes_links` 的 `rel=next` | §5.6.28表5-299误列单数 `volume`，但同表对 `volumes_links` 的说明及响应示例均指向复数数组；未列 `count`，不能要求或编造它 |
| 容量与字段 [EVC1][EVC3] | §5.4.3 `size` 为 Integer、**GB**；`attachments[].server_id`、`volume_id`、`device`、`attached_at` 有定义；`availability_zone/created_at/name/volume_type/status` 可用于台账 | §5.6.28的 `size` 只写“云硬盘大小”，没有重述单位；GB证据来自同书§5.4.3，不能冒称该表直接写了GB。`bootable` 在§5.4.3表是Boolean，示例却为字符串；§5.6.28表是String。两处 `encrypted` 均注明当前服务不支持，缺失/false不能证明未加密 |

**普通EVS独立补核 [EV0][EV1][EV2]：** 官方正文 **云硬盘(EVS)8.5.1 使用指南(for华为云Stack8.5.1)02**，EDOC1100453160，2026-05-07更新。云硬盘章节的表3-281/3-282及表3-323/3-324分别明确 `GET /v2/{tenant_id}/volumes/detail` 与 `GET /v2/{tenant_id}/cloudvolumes/detail`。二者均支持marker末条ID、limit>0、offset记录偏移且文字写>0；成功列表是 `volumes[]`、有 `volumes_links`，未列count；size在这两章只写“云硬盘大小”。同书云硬盘apicom的查询所有云硬盘详情 [EV3] 则明确size Integer、GB；因此普通EVS的GB口径可从本产品同书取得，须标注为跨章节佐证，而非拿云块存储表替代。因此普通EVS的cloudvolumes路径也有精确8.5.1支持，不能拿云块存储的章节替代其字段/单位证明，亦不能反过来宣称普通EVS只支持volumes。

只读比对快照：`_evs_raw` 使用 `cloudvolumes/detail`、`page_all` 默认记录偏移、`total_key="count"`。这一路径在上述两类指定8.5.1指南中都有依据，但不代表全部补丁/局点实测可用；没有 `count` 时必须仍能完整分页。`volumes_links` 可能指向另一路径，不能无验证跨服务主机追随上游 URL。

### DWS：路径与返回 key 已确认

[DWS1] 官网正文：**数据仓库服务(DWS) 9.1.0.218 使用指南(for 华为云Stack 8.5.1) 06**，EDOC1100475350；页面更新时间 **2026-07-21**。搜索摘要仍可能显示02版，本文以当前实际正文06版为准。

- API参考 > API说明 > 集群管理 > **查询集群列表**，表7-30至7-38：`GET /v1.0/{project_id}/clusters`，该章请求参数写“无”；返回 `clusters[]`，`count` 为总数。不能根据公有云新版擅改路径、返回key或追加分页。
- `id/name/status/version/created/updated/availability_zone/node_type/number_of_node` 有定义；内网连接是 `endpoints[].connect_info`（含端口），不是 `private_ip[]`。不能把 JDBC URL/连接域名直接当IP。
- 列表未定义CPU、内存、磁盘容量字段。`node_type` 是规格标识，`number_of_node` 是节点数，不是资源容量。
- [DWS2] **查询集群规格详情**：`GET /v2/{project_id}/clusters/{cluster_id}/flavor`，响应 `flavor`，含 `attribute[]`、`volume_node.volume_num/capacity/volume_size`；该章没有为这些容量数值标注单位。不能仅根据示例 `cpuNum/memSize` 推断单位、逐节点/集群总量或副本乘数。
- 原章特别注明HCS纳管模式响应可能与示例不同；字段缺失须保留未知。

只读比对：`fetch_dws` 的列表路径和 `clusters` 正确；使用 `private_ip` 缺乏 [DWS1] 支持；不能为填容量而臆造列表字段。

### DMS Kafka：必须考虑分页，不能只读默认10条

[DMS1] 官网正文：**分布式消息服务(DMS) 2.3.0.7 使用指南(for 华为云Stack 8.5.1) 01**，EDOC1100450009，API参考 > 生命周期管理 > 查询所有实例列表，表4-9至4-12；页面更新2026-06-04。

| 项目 | 已确认契约 | 不确定性/限制 |
| --- | --- | --- |
| 路径与必填项 | `GET /v2/{project_id}/instances`，Query **`engine=kafka` 必填** | 示例URI未展示engine，不得以简化示例覆盖必填表 |
| 分页 | `offset` 可选String，表示从该偏移量开始查询，`>=0`；`limit` 可选String，默认 **10**，范围 **1–50** | offset默认值未写；按已返回记录数推进属于实现推导，不冒称原文给出了算法。全量台账不能单次默认请求，也不能使用通用默认limit=100 |
| 响应 | `instances[]`；`instance_num` Integer，描述仅为“实例数量” | 未明确它是全局总数还是当前页数，不把这条描述升级为可靠总数终止契约 |
| 存储 | `storage_space/used_storage_space/total_storage_space` 均Integer、**GB** | 三字段有不同名字与示例值，不能擅自当作同一容量；文档未定义副本扣除等公式 |
| 标识/连接/时间 | `instance_id/name/status/engine_version/available_zones`；`connect_address` 是连接IP，`port` 单独返回；`created_at` String，Unix **毫秒** | 官方示例connect_address可为逗号分隔纯IP，并不总含`:port`；不能无条件裁去IPv6冒号部分 |
| CPU/内存 | 列表没有独立CPU/内存字段；[DMS2]规格API返回产品/规格标识 | 不从 `kafka.4u16g.cluster` 的编码猜u/g单位，不用product_id当实际资源数量 |

[DMS2] `GET /v2/{engine}/products`（示例engine=kafka）：仅列可选 `product_id`，未列分页；`products[].properties.min_storage_per_node/max_storage_per_node` 明确GB，另有 `product_id/ecs_flavor_id/product_alias`，未提供独立CPU/内存数值。[DMS3]产品规格表同样只给规格名称，不足以证明编码解析规则。

只读比对：研究初始 `fetch_kafka` 仅请求一次，存在默认10条截断风险；收尾复核已由并行修复改为offset分页、limit=50并保留engine=kafka。独立静态断言已通过，未用Mock响应作证；通过不代表上游联调成功。

### CSS：8.5.1契约尚未确认

已检查华为公开精确版本搜索、8.5.1产品用户指南目录，以及 **华为云Stack 8.5.1 API参考03** [API0] 所有15类云服务目录，未定位到正文明确for8.5.1的CSS列表接口。**“本次未找到”不等于“产品不支持”。**

因此：`GET /v1.0/{project_id}/clusters`、`start/offset/limit`、页号起点、`totalSize` 和容量单位均不列为8.5.1已证实契约。当前实现 `mode="page", start=1, offset_param="start"` 只能记为待核；不能据公有云/8.6资料断言CSS offset是页号，不能为让测试通过改成另一种未证实分页。需补充该局点CSS的官方8.5.1服务文档或明确产品版本后再接入契约断言。

## RDS与GaussDB：真实字段及单位边界

### RDS

[RDS1] **Relational Database Service (RDS) 25.1.30 Usage Guide (for Huawei Cloud Stack 8.5.1)**，EDOC1100458141，**01**版，官网更新2026-08-26；DB Instance Management > Querying DB Instances。

- `GET /v3/{project_id}/instances`；`offset` 是记录偏移、默认0；`limit` 1–100、默认100；响应 `instances[]/total_count`。
- `instances[].cpu` String为vCPU数；`mem` String为**GB**；`volume.size` Integer为**GB**；`flavor_ref` 为规格码。不能沿用ECS的MB换算，也不能按nodes中主/备/只读节点个数再乘一遍实例容量。
- 当前 `fetch_rds` 字段与此表相符；可选字段缺失仍保留未知。

### GaussDB

[G1] **GaussDB 25.1.32 API Reference (for Huawei Cloud Stack 8.5.1) 01**，EDOC1100457467；已直接读取所属官方PDF封面25.1.32、2025-06-30及§4.3.4正文（官网文档包发布时间另为2026-07-03），非仅依据信息中心URL中的版本字符串。

- **推荐列表** §4.3.4，印刷页81起：`GET /gaussdb/v3.3/{project_id}/instances`；`offset` 记录偏移、默认0；`limit` 1–100、默认100；返回 `instances[]/total_count`。[G1][G2]
- 同一PDF的Historical APIs还列 `/gaussdb/v3.1/{project_id}/instances`、`/gaussdb/v3.2/{project_id}/instances`、`/opengauss/v3/{project_id}/instances`。不能把所有GaussDB版本/兼容路径混成RDS的裸 `/v3`；实际相对路径还要结合endpoint前缀核对，本文未读取客户端或配置去假定前缀。
- 推荐列表 `flavor_ref` 为规格码，`flavor_info.vcpu` Integer明确为vCPU数，`flavor_info.mem` Integer在表4-56只写Memory size；`volume.size` Integer在表4-57只写Disk size。**这两张列表表格没有明确单位**。
- [G3]同版规格接口 `flavors[].vcpus` String为vCPU数，`ram` String明确**GB**；`spec_code` 可与 `flavor_ref` 对齐。若要补内存，使用确切规格映射而非解析规格名；匹配失败保留未知。
- [G4]同版创建实例的 `volume.size` 描述有“40表示40GB”及“分片数×40GB至分片数×24TB”的规则，可作GB的**跨章节佐证**，不能冒称列表表格直接写GB，也不能据此擅乘副本/节点/分片数。BMS本地盘的创建size由规格计算、传入值无效，不能把创建请求值当实测容量。
- 列表查询type区分集中式 `Centralization_standard` 与分布式 `Combined`；响应描述及旧示例存在大小写/历史值差异，不能只按示例枚举设计硬过滤。[G2]

只读比对快照：`fetch_gaussdb` 使用 `/v3/{project_id}/instances`，且未读取 `flavor_info`；`gaussdb_attrs` 将 `volume.size` 命名为GB。应向主线程登记路径/版本以及单位来源，不能从这些未确认字段造0或强制转换。本文不修改适配源码，也不把推荐路径当作唯一兼容版本写入断言。

## ECS与网络服务

### ECS

[ECS1][ECS2] **Elastic Cloud Server (ECS) 8.5.1 Usage Guide (for Huawei Cloud Stack 8.5.1) 04**，EDOC1100452338；官网API参考更新2026-07-09。

- 列表 `GET /v1/{tenant_id}/cloudservers/detail`；该接口 `tenant_id` 明确指Project ID。`offset` 是**页号**：0或1均是第一页；**省略offset仅返回count**。顺序逐页遍历宜从1开始，避免0→1重复第一页；`limit` 最大1000；响应 `count/servers[]`。
- `servers[].flavor.vcpus` String为CPU核数；`ram` String为**MB**。MB→GB采用何种基数属于台账显示约定；原表未明确定义MB是MiB，不把1024除数冒称文档规定。
- `flavor.disk` 是规格中的系统盘约束，0代表不限，并非实例实际挂盘汇总。[ECS2] `GET /v1/{tenant_id}/cloudservers/{server_id}/block_device` 的响应示例是 `volumeAttachments[]`，`size` Integer明确**GB**，`bootIndex=0`标识系统盘。
- `addresses` 按网卡名称组织，网卡元素 `addr/OS-EXT-IPS:type` 等有定义；`metadata`不应自动映射成固定必有属性。缺卷容量或挂载关系失败时，不据此生成0GB。

### VPC/子网

[VPC1][VPC2] **Virtual Private Cloud (VPC) 8.5.1 Usage Guide (for Huawei Cloud Stack 8.5.1) 04**，EDOC1100452400，官网API参考更新2026-07-13；同书VPC原生接口。

| 资源 | 方法/路径 | 分页/响应 | 字段边界 |
| --- | --- | --- | --- |
| VPC [VPC1] | `GET /v1/{tenant_id}/vpcs` | `limit/marker`；limit=0或省略表示不限制；marker为上一页末条ID；`vpcs[]` | `id/name/cidr/status`等，不含CPU/内存/磁盘容量 |
| 子网 [VPC2] | `GET /v1/{tenant_id}/subnets` | 同类marker分页；`subnets[]` | `vpc_id/cidr/gateway_ip/dhcp_enable/neutron_network_id/neutron_subnet_id`等；子网资源ID与Neutron subnet ID不能混用 |
| VPC v3 [VPC3] | URI写 `GET /v3/{tenant_id}/vpc/vpcs` | 同书另一版本接口，不能当v1同一路由 | 原章请求示例仍写v1，保留文档不一致，不能据示例重写URI |

### EIP/ELB：原生接口与社区兼容接口分章

[EIP0][EIP1] **弹性IP(EIP) 8.5.1 使用指南(for 华为云Stack 8.5.1) 03**，EDOC1100452355：社区兼容的查询接口为 `GET /v2.0/floatingips`；响应 `floatingips[]/floatingips_links`。`limit` Integer为每页数，`marker`为起始资源ID、空表示首分页；links含 `href/rel`（next/previous），示例允许null。字段是 `floating_ip_address/fixed_ip_address/qos_policy_id` 等；`qos_policy_id`是带宽ID，不是带宽数值。该兼容章节不能证明原生 `/v1/{project_id}/publicips` 不存在；不能把两套返回字段混用。

[ELB1][ELB2] **弹性负载均衡(ELB) 8.5.1 使用指南(for 华为云Stack 8.5.1) 05**，EDOC1100452378，官网API参考更新2026-07-13：社区兼容接口为 `GET /v2.0/lbaas/loadbalancers`，返回 `loadbalancers[]`；该章Query仅声明 `cluster_id`，不能替它发明limit/marker契约。原文另注明技术预览 `/v2/{project_id}/elb/loadbalancers`有同样查询能力。**同书还明确有V3 API目录 [ELB0]**，因此不能仅核兼容章就宣称现有v3路径不支持。

**原生接口补核：** [EIP0]组合接口明确 `GET /v1/{tenant_id}/publicips`，响应 `publicips[]`，支持 `limit/marker`；含 `bandwidth_id/bandwidth_name/bandwidth_size`（Long）、`bandwidth_share_type/bandwidth_direction`，当前台账路径因此有同版支持。另有v3 `/v3/{tenant_id}/eip/publicips`，其带宽信息改为嵌套 `bandwidth`，不能把v3字段表套入v1；v3响应表publicips与示例publicip存在不一致。[EIP2]

[ELB2]V3原生章明确 `GET /v3/{project_id}/elb/loadbalancers`，响应 `loadbalancers[]/page_info`，后者有 `previous_marker/next_marker/current_count`。**当前ELB路径正确。** 但此章Query表同样只列 `cluster_id`，没有给 `limit/marker` 的取值/默认值；响应分页信息的存在不等于请求参数表已经完备。保留分页参数证据缺口，不用自己的Mock填补官方正文。

兼容响应 `guaranteed` 虽解释独享/共享，但原文明示为兼容CCE/高阶服务的**预留字段**，HCS场景实例实际仍承载共享ELB集群；不宜简单据true就承诺物理独享。`listeners/pools`是关联列表，`vip_address/vip_subnet_id/vpc_id`有定义。网络资源的CPU/内存/磁盘单位不适用。EIP列表表格未为bandwidth_size重述单位；同版用户指南 [EIP3] 申请弹性IP写“带宽大小(Mbit/s)”，创建共享带宽明确Mbit/s，可作为**跨章节单位佐证**，不从ID/规格名推断，也不改写成MB/s。该指南03版页面更新于2026-07-13。

同书组合接口 [EIP4] 支持 `GET /v1/{tenant_id}/bandwidths/{bandwidth_id}`（`bandwidth`对象）、`GET /v1/{tenant_id}/bandwidths`（`bandwidths[]`），另有 `/v2.0/{tenant_id}/bandwidths` 的limit/marker分页。原生publicips直接有bandwidth_id；社区floatingips可通过qos_policy_id关联，需按对应产品/权限核验，不能把带宽ID当大小。

## 具体官方来源

- [EVC0] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100455595?section=k001>，云块存储专版API PDF所属指南；[PDF正文](https://support.huawei.cn/supportgateway/view/v1/enterprise/doc/main-content?nid=EDOC1100455595&partNo=k001)。
- [EVC1] 同PDF §5.4.3，印刷页90–95；PDF物理页偏移依封面/目录，不把印刷页当PDF索引。
- [EVC2] 同PDF §5.6.20，印刷页184–188。
- [EVC3] 同PDF §5.6.28，印刷页216–221。
- [DWS1] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100475350/2b0530af#ZH-CN_TOPIC_0000002270497945>。
- [DWS2] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100475350/2b0530af#ZH-CN_TOPIC_0000002235498444>。
- [ECS1] <https://support.huawei.cn/enterprise/en/doc/EDOC1100452338/ca0d1efd/lifecycle-management#EN-US_TOPIC_0000002124260517>。
- [ECS2] <https://support.huawei.cn/enterprise/en/doc/EDOC1100452338/236ebbc8/disk-management#EN-US_TOPIC_0000002124180273>。

- [DMS1] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100450009/bd0a2e6b#ListInstances>。
- [DMS2] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100450009/c4d696cf#ListEngineProducts>。
- [DMS3] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100450009/62498ac7>。
- [API0] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100453325>，API参考03（2025-08-11）；仅目录定位，不将未列CSS解释为不存在。
- [RDS1] <https://support.huawei.cn/enterprise/en/doc/EDOC1100458141/51d21a18/db-instance-management#EN-US_TOPIC_0000002342919457>。
- [G1] <https://support.huawei.cn/enterprise/en/doc/EDOC1100457467?section=k001>；[官方PDF正文](https://support.huawei.cn/supportgateway/view/v1/enterprise/doc/main-content?nid=EDOC1100457467&partNo=k001)，§4.3.4、表4-56/4-57。
- [G2] <https://doc.hcs.huawei.com/db/en-us/gaussdb/25.1.32/api/gaussdb_api_328.html>。
- [G3] <https://doc.hcs.huawei.com/db/en-us/gaussdb/25.1.32/api/gaussdb_api_084.html>。
- [G4] <https://doc.hcs.huawei.com/db/en-us/gaussdb/25.1.32/api/gaussdb_api_017.html>。

- [EVC4] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100496444/cd60dae4#ZH-CN_TOPIC_0000002170985122>（cloudvolumes，表3-313至3-318）；同页 `#ZH-CN_TOPIC_0000002170985090` 为volumes。
- [EV0] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160>，已在官方产品目录确认“云硬盘(EVS)8.5.1 使用指南(for华为云Stack8.5.1)02”；官网正文更新2026-05-07；精确列表章节见[EV1][EV2]。
- [VPC1] <https://support.huawei.cn/enterprise/en/doc/EDOC1100452400/e654702c/vpc#EN-US_TOPIC_0000002076345310>。
- [VPC2] <https://support.huawei.cn/enterprise/en/doc/EDOC1100452400/e654702c/vpc#EN-US_TOPIC_0000002076500050>。
- [VPC3] <https://support.huawei.cn/enterprise/en/doc/EDOC1100452400/e654702c/vpc#EN-US_TOPIC_0000002076345314>。
- [EIP1] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/bd0a2e6b#ZH-CN_TOPIC_0000002112208321>。
- [ELB1] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452378/53044f47#ZH-CN_TOPIC_0000002112197297>。
- [ELB0] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452378/bd0a2e6b>，V3 API目录，不据此目录独自断言所有参数。

- [EIP0] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/ca0d1efd#ZH-CN_TOPIC_0000002112123313>，v1组合接口。
- [EIP2] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/ca0d1efd#ZH-CN_TOPIC_0000002112208329>，v3组合接口。
- [ELB2] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452378/bd0a2e6b#ZH-CN_TOPIC_0000002112112189>，V3查询负载均衡列表。

- [EIP3] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/d578b42e#ZH-CN_TOPIC_0269731820>（申请弹性IP带宽大小Mbit/s）；<https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/a27f84b8#ZH-CN_TOPIC_0269731808>（创建共享带宽）。
- [EIP4] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/ca0d1efd#ZH-CN_TOPIC_0000002112123321>（带宽详情）；同页 `#ZH-CN_TOPIC_0000002076409228` 为v1列表，`#ZH-CN_TOPIC_0000002112123329` 为v2.0列表。

- [EV1] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/cd60dae4#ZH-CN_TOPIC_0000002240926649>（普通EVS volumes）。
- [EV2] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/cd60dae4#ZH-CN_TOPIC_0000002240926657>（普通EVS cloudvolumes）。

- [EV3] <https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/24037fd1#ZH-CN_TOPIC_0000002205886482>，普通EVS apicom查询所有云硬盘详情，size明确GB。

## 独立检查与交接

本任务写集只有本文和 `backend/tests/test_hcs851_resource_evidence.py`；没有修改适配源码、Mock、Probe或调用目录。后续接入由主线程/Euclid负责。

- 标准库AST只解析fetchers与pager：ECS页号、RDS记录偏移和limit、DWS路径/key及无凭空分页、EVS不跳首条、VPC/子网marker、EIP/ELB原生路径、Kafka必填engine与分页上限。
- 不导入应用、不加载.env、不触发conftest/DB/云请求。静态护栏只核指定源码组织下的明确请求约束；不能代替响应解析测试、真实云联调或证明所有服务支持。
- `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 backend/.venv/bin/pytest --noconftest -p no:cacheprovider backend/tests/test_hcs851_resource_evidence.py`：**7 passed，4 subtests passed**。初测暴露的Kafka单页问题已由并行修复消除，没有放宽证据断言。
- `backend/.venv/bin/ruff check --no-cache backend/tests/test_hcs851_resource_evidence.py`：通过；本文引用标识全部有来源登记，外链全部属于华为官方域名。
- 尚未确认：CSS精确8.5.1契约；ELB v3请求分页参数表缺失；GaussDB列表内存/容量表未直接标单位及endpoint/profile适配。不得将这些开放项写成已验收。
