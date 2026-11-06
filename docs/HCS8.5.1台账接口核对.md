# HCS 8.5.1 台账接口核对

> 2026-11-02 最新策略（`20261102-aom-disabled-1`）：所有资源暂时停用 AOM。本文此前（含同日早期）关于显式 AOM、映射编辑、探测或查询可用的结论仅保留为历史审计，已由 [AOM停用说明](AOM停用说明_20261102.md) 覆盖；无正式 HCS 调用证据，Mock 成功不代表生产可用。

核对日期：2026-10-27。范围为 `backend/app/ledger/fetchers.py` 的 14 类资源和依赖查询。
只核对公开官方文档，不读取实际 `.env`、生产凭据或生产数据库，不连接生产云。
本文件严格区分“当前代码接口清单”“官方已核契约”“版本 / 产品变体待核项”。现有 Mock 不作为协议依据。

## 1. 开始核对时的代码清单

以下是本轮修改前的实际实现，不代表已经符合 8.5.1。`{project_id}` 由既有客户端替换；
endpoint 列是代码使用的服务键，不是对现网主机名或 IAM catalog service type 的推断。

| 资源 / 函数 | 服务键 | 当前 GET 路径 | 当前分页 / 响应 | 当前取值与单位 |
| --- | --- | --- | --- | --- |
| ECS / `fetch_ecs` | `ecs` | `/v1/{project_id}/cloudservers/detail` | `offset=1` 页码、limit；`servers[]/count` | flavor.vcpus；flavor.ram 按 MB÷1024 写 memory_gb；addresses 的 fixed/floating；metadata；created |
| EVS / `_evs_raw`、`fetch_evs` | `evs` | `/v2/{project_id}/cloudvolumes/detail` | offset 记录偏移、limit；`volumes[]/count` | size 直接写 storage_gb；attachments.server_id/device；bootable；encrypted；created_at |
| VPC / `fetch_vpc` | `vpc` | `/v1/{project_id}/vpcs` | limit/marker，末条 id 推进；`vpcs[]` | cidr/status/description；关联子网计数 |
| 子网 / `_subnets_raw`、`fetch_subnet` | `vpc` | `/v1/{project_id}/subnets` | limit/marker；`subnets[]` | cidr/gateway_ip/vpc_id/dhcp_enable/availability_zone |
| EIP / `fetch_eip` | `vpc` | `/v1/{project_id}/publicips` | limit/marker；`publicips[]` | bandwidth_size 按 Mbit/s 展示；share_type WHOLE；public/private_ip_address；create_time |
| ELB / `fetch_elb` | `elb` | `/v3/{project_id}/elb/loadbalancers` | limit/marker；`loadbalancers[]` | vip_address/eips；availability_zone_list；guaranteed；listeners 长度；member_count 缺失默认 0 |
| RDS / `fetch_rds` | `rds` | `/v3/{project_id}/instances` | offset/limit；`instances[]/total_count` | cpu；mem 直接写 GB；volume.size 直接写 GB；nodes[0].availability_zone；private_ips；created |
| GaussDB / `fetch_gaussdb` | `gaussdb` | `/v3/{project_id}/instances` | offset/limit；`instances[]/total_count` | type/flavor_ref；volume.size 写 GB；availability_zone；private_ips；created。共享 `gaussdb_attrs` 也被巡检引用 |
| DCS / `fetch_dcs` | `dcs` | `/v2/{project_id}/instances` | offset/limit=1000；`instances[]/instance_num` | instance_id；capacity 直接写 GB；engine/version；az_codes/ip/port/spec_code；created_at |
| Kafka / `fetch_kafka` | `dms` | `/v2/{project_id}/instances?engine=kafka` | 当前只调用一次；`instances[]` | instance_id；storage_space、used_storage_space 按相同单位算占用率；connect_address；available_zones；created_at |
| MRS / `fetch_mrs` | `mrs` | `/v1.1/{project_id}/cluster_infos` | currentPage=1 页码、pageSize=50；`clusters[]/clusterTotal` | clusterId/Name/State/Version；azName/masterNodeIp；totalNodeNum/componentList/createAt |
| DWS / `fetch_dws` | `dws` | `/v1.0/{project_id}/clusters` | 单次查询；`clusters[]` | id/name/status；availability_zone；private_ip[0]；node_type/number_of_node/version/created |
| CSS / `fetch_css` | `css` | `/v1.0/{project_id}/clusters` | 当前把 start=1 当页码、limit=10；`clusters[]/totalSize` | id/name/status 100/200/303 映射；instances[].specCode/azCode；endpoint/created/datastore |
| CCE / `fetch_cce` | `cce` | `/api/v3/projects/{project_id}/clusters` | 单次查询；`items[]` | metadata.uid/name/creationTimestamp；spec.version/flavor/type；status.phase/endpoints |
| CCE 节点依赖 | `cce` | `/api/v3/projects/{project_id}/clusters/{cluster_id}/nodes` | 每集群单次；`items[]` | 节点条数作为 node_count，不采集 Kubernetes Events |

### 共享依赖及失败处理基线

- ECS 还依赖完整 EVS 列表，按 attachments.server_id 累加 size；缺失容量时不以 0 冒充已知容量。
- `_evs_raw`、`_subnets_raw` 在同轮 ctx 内缓存完整列表；VPC 名称、ECS 名称及 IP 归属由前序 fetcher 共享。
- HTTP / 认证 / 限流 / 重试由既有 HcsClient 负责，本轮不修改 IAM expiry 缓存、10 QPS / 4 并发策略。
- `response_items` 对缺失或畸形列表报错；已有分页器会对重复页、提前结束和预算耗尽报错。
  本轮只在 fetchers 层修复有官方证据的参数、字段或单位，不修改共享 pager。
- 台账查询不调用 CES；AOM 必须显式配置的来源策略也不属于台账库存查询依赖。

## 2. 官方证据与逐项结论

本轮采用主线程 / Cicero 提供的官方定位，并以独立、无登录的本地浏览器复读 ECS、普通 EVS、
ELB、DWS、CCE、MRS 正文；GaussDB 另下载官方 k001 PDF到内存解析并渲染表4-56/57复查。
未把仓库 Mock 当作证据，也没有发送私有源码、内部地址或客户信息到搜索引擎。

| 资源 | 8.5.1 版本证据 / 正文 | 核对结论与本轮处理 |
| --- | --- | --- |
| ECS | ECS 8.5.1 Usage Guide for HCS8.5.1，EDOC1100452338，浏览器标题04，[ECS1] | 保留 v1 cloudservers/detail。正文 tenant_id 指 Project ID，所以内部 `{project_id}` 变量替换不改变实际 URI。offset 是页码，0/1 均首页，**省略 offset 只返回 count**；保留显式1、limit100（上限1000）。vcpus 核数；ram 为MB；不把 flavor.disk 当实际挂载容量。 |
| EVS | 普通 EVS 8.5.1 for HCS8.5.1 02，EDOC1100453160，[EVS1]、[EVS2]、[EVS3] | 同书同时定义 volumes/detail、cloudvolumes/detail，不能以另一个章节否定当前路径。保留 cloudvolumes。marker 为上一页末条id；显式使用有依据的 marker/limit，不发送文档表述存在歧义的首页 offset。 |
| VPC | VPC for HCS8.5.1，EDOC1100452400，[VPC1] | 保留 v1/vpcs、vpc服务键；limit/marker，marker末条id。API允许 limit0/省略不限制，平台仍固定正数100控制请求预算。 |
| 子网 | 同上，[VPC2] | 保留 v1/subnets，`id` 为台账资源ID；`neutron_subnet_id` 不是同一字段，不替换。共享原生 vpc_id 关联与子网缓存。 |
| EIP | EIP 8.5.1，EDOC1100452355 原生章，[EIP1]、[EIP2]、[EIP3] | 保留 v1/publicips 与 publicips[]，limit/marker。tenant_id 命名不构成迁移到 Neutron的依据。带宽 Mbit/s；不换成 floating_ip_address/qos_policy_id。 |
| ELB | ELB 8.5.1 for HCS8.5.1 05，EDOC1100452378 原生V3章，[ELB1] | 保留 /v3/{project_id}/elb/loadbalancers。该Query表只有 cluster_id，不发送兼容V2章的limit/marker。page_info.current_count 可检查本页一致性；next_marker 只定义末条ID，不能据此断言有下一页。guaranteed 是兼容预留标记，HCS实际仍共享集群承载，不标“物理独享”。 |
| RDS | RDS25.1.30 for HCS8.5.1，EDOC1100458141，[RDS1] | 保留 /v3/instances，offset记录偏移默认0，limit1–100；instances/total_count。cpu String vCPU、mem String GB、volume.size Integer GB，均按实例字段，不乘节点数。现有容量字段保留。 |
| GaussDB | GaussDB25.1.32 API Reference for HCS8.5.1 01，EDOC1100457467 k001，[G1] | §4.3.4 为 /gaussdb/v3.3/{project_id}/instances，不是裸/v3。用主线程共用 gaussdb_instances_path()；历史v3.2/v3.1/opengauss_v3只能显式配置，不自动试探。flavor_info.vcpu有vCPU单位；mem/volume.size列表表没有单位，保留原始值，不写入GB。 |
| DCS | DCS2.3.0.5 for HCS8.5.1 01，EDOC1100450216，[DCS1] | 保留v2 instances，offset记录偏移、limit1–1000。capacity为GB；capacity_minor String为小于1GB规格的GB容量，修复capacity为0时丢失该值。台账规格容量与原生统计的used_memory/max_memory MB不是同一指标。 |
| Kafka | DMS2.3.0.7 for HCS8.5.1 01，EDOC1100450009，[K1] | 保留v2 instances，engine=kafka必填；offset记录偏移，limit默认10、最大50。instance_num只定义实例数量，未证明为全量total：使用 total_key=None，按50条短/空页完成。storage_space/used_storage_space等存储字段GB，不变换单位。 |
| MRS | MRS3.5.1-LTS for HCS8.5.1 11，EDOC1100452559，[MRS1] | 保留v1.1 cluster_infos；currentPage页码、pageSize页大小，表中均为String，HTTP参数序列化为字符串。clusterTotal表为String、示例为整数，现pager兼容二者。createAt为10位示例秒时间戳；不误当毫秒。现有节点数、主节点IP、组件字段有定义，不增详情请求。 |
| DWS | DWS9.1.0.218 for HCS8.5.1 06，EDOC1100475350，[DWS1] | 保留v1.0 clusters，无查询参数，不添公有云分页。返回clusters/count；正文示例可省count，因此仅在提供时校验。内网来自 endpoints[].connect_info，不是private_ip，不退到公网。列表没有CPU/RAM/容量，保持未知。 |
| CCE | CCE25.3.0-HCS for HCS8.5.1 05，EDOC1100453109，[CCE1]、[CCE2] | 保留原生api/v3集群和逐集群节点API。集群Query为detail/status/type/version，节点列表无分页Query，不造limit/continue。items为当前范围全体对象。metadata.uid作ID、spec字段作规格、status.phase作状态；Internal端点按URL解析host，修复按冒号切IPv6地址。节点数取实际Node列表长度，不由flavor上限推算。 |
| CSS | 现有v1.0 clusters实现；851聚合EDOC1100453325暂未定位到可验证的CSS API正文 | **未证实，不等于不支持。** 本轮没有用公有云/新版本正文改URI、start含义或单位。保留既有实现并单列待确认，不能宣称CSS的分页/字段已完成851认证。 |

### 已有台账的字段纠正

适配器停止推算字段后，旧数据不能继续作为当前事实展示。`LedgerRecord.clear_fields/clear_attrs` 仅声明已确认不可靠的字段，`apply_snapshot` 允许这些值明确置空并记录变更；普通缺失字段仍保留原值。覆盖 GaussDB 未知单位的旧容量、ELB 旧独享规格及缺失成员/监听数、EVS 无依据的加密结论、DWS 错误来源的私网地址。GaussDB 巡检同样清理单位未核实的旧容量，并重建不含未经证实 GB 数值的规格文本。

新增 8 项回归验证已有值清理、跨项目隔离、审计、幂等及无效声明拒绝。整合后 SQLite 全量 1258 passed / 21 skipped，PG16 全量 1264 passed / 15 skipped；均包含 4 个 subtests。

### 2.1 EVS 正文自身差异与部分页处理

- 普通EVS同书 `/volumes/detail` 与 `/cloudvolumes/detail` 查询表均支持 marker/limit/offset。
  offset文字写“大于0”，与OpenStack常见首页0表达不同；本轮使用同章明确支持的marker，既不机械改为offset1，也不切接口族。
- 列表字段表写 `volume` 单数 / Map，但同章示例、`volumes_links` 说明均为 `volumes[]`；适配使用数组，缺失/畸形时报错，不转空库存。
- 列表无count；`volumes_links` 被说明为“仅部分列表时返回”。即便不足请求limit，也必须续页。
  字段表写字符串数组，而示例为href/rel对象数组，本轮兼容这两种已展示结构；只按末条id推进marker，**不跟随href、不把凭据发给响应指定的主机**。
- size列表表没有显式单位；同书创建云硬盘/创建云硬盘v2的size明确GB，[EVS3] 是保留既有GB语义的同组件旁证，不能说列表表自身已写GB，也不擅改MiB/byte。
- bootable存在Bool/String版本差别，兼容true/false；缺失保持未知，不默认“否”。
- 当前普通EVS列表把encrypted标为“当前云硬盘服务不支持该字段”。保留 `encrypted_reported` 原始布尔信息，`encrypted`保持未知，不能把兼容占位false当已证实“未加密”。

### 2.2 GaussDB 列表与规格 API 必须分开

- 官方书目标题为25.1.32、for HCS8.5.1、01；网页发布日期2026-07-03，PDF封面为Issue01 / 2025-06-30。
  两个日期含义不同，版本依据为官方书目与PDF内容，不把网页日期当组件版本。
- 列表§4.3.4，PDF物理页91–107（印刷页81–97）。表4-56/57在物理页106：
  vcpu Integer明确为vCPU数；mem Integer只写Memory size；volume.size Integer只写Disk size。
  示例16/40不能代替GB单位定义。节点AZ由nodes[].availability_zone读取并去重。
- `gaussdb_attrs`保留兼容键volume_gb=None，新增memory_size/volume_size原值、vcpus和
  memory_unit_verified=False、volume_unit_verified=False；台账vcpus可进入Guide容量字段，内存/磁盘仍未知。
- 同PDF §4.1.3（物理页25–30、印刷页15–20）另有真实规格接口：

  ```http
  GET /gaussdb/v3.2/{project_id}/flavors?offset=0&limit=100&spec_code=<exact_flavor_ref>
  ```

  project_id必填；spec_code、version、ha_mode可选；offset为记录偏移默认0；limit1–100、默认100。
  ha_mode枚举为centralization_standard、combined。响应字段flavors[]、total；表4-9明确vcpus String为vCPU数，
  **ram String为GB**，还有spec_code、availability_zone、version、group_type等字段。

  ```json
  {"flavors":[{"vcpus":"2","ram":"16","spec_code":"<exact_flavor_ref>",
    "availability_zone":["az-test"],"version":"<returned_version>","group_type":"normal2"}],"total":1}
  ```

  同书创建实例flavor_ref说明指向规格查询的spec_code。后续可按精确spec_code、版本/部署模式和唯一匹配做规格补充，
  不能按名称相似匹配、拿v3.3臆造flavors路径，或将CN/DN/ETCD等不同group_type混合。
  本轮**没有增加规格API运行依赖**，也没有把规格ram直接等同列表mem或乘节点数推算整实例物理容量；
  volume.size单位缺口仍未被这个ram字段解决。

### 2.3 不混淆网络接口族与标志含义

- EIP同书Neutron `/v2.0/floatingips` 的floatingips/floatingips_links、floating_ip_address、qos_policy_id
  属于另一接口族，不替换已证实的原生 `/v1/{tenant_id}/publicips`。
- ELB同书兼容v2.0/lbaas章节与原生v3章节并存，不能移植兼容章Query参数。
  原生PageInfo没有可据以猜出的请求分页契约；next_marker只是返回末条ID，仍需厂商/现场确认未公开的分页边界。
  current_count与实际条数矛盾会报不完整；无正文依据的member_count不再填0，listeners只统计明确返回的关联列表。
- endpoint列中的ecs/evs/vpc/elb/rds/gaussdb/dcs/dms/mrs/dws/css/cce是内部解析键，
  实际主机与证书由既有配置提供；本轮未更改域名模板、IAM认证或服务发现。

## 3. 已证实修改和独立回归

测试文件为 `backend/tests/test_ledger_hcs851_contract.py`；fixture 按以上官方字段结构独立整理，
标识/地址为测试值，不复制真实云数据，不导入 `mock_hcs`，不连接真实云或生产数据库。

重点回归：ECS显式页码/MB与挂盘关联、EVS无count且短页有续页标记/缓存/Bool-String、
VPC与子网原生ID、EIP原生字段与Mbit/s、ELB原生无伪造分页与保留字段、RDS实例级GB、
Gauss四个显式路径与未知单位、DCS小于1GB、Kafka多页/页内计数/后页失败、MRS页码与秒时间戳、
CCE原生UID与URL主机解析，以及14类资源的403/503传播和畸形列表不转空库存。
CSS只包含错误/畸形列表的保底回归，不声称其正向fixture已获851正文认证。

小范围验证命令（backend目录）：

```bash
env -u INSPECTOR_TEST_DATABASE_URL INSPECTOR_ENV_FILE= INSPECTOR_LOG_DIR= \
  .venv/bin/pytest -o addopts='' tests/test_ledger_hcs851_contract.py -q --tb=short --show-capture=no
.venv/bin/ruff check app/ledger/fetchers.py tests/test_ledger_hcs851_contract.py
INSPECTOR_ENV_FILE= .venv/bin/mypy app/ledger/fetchers.py
```

本轮最后组合验证：`test_ledger_hcs851_contract.py`、`test_review_collection_paging.py`、
`test_review_collection_ledger.py` 共 **100 passed**；本写集Ruff通过，fetchers.py普通mypy通过，
`git diff --check`通过。组合中的数据库测试只使用独立SQLite fixture；官方结构契约文件本身不使用数据库fixture。

仅修改fetchers.py、该独立测试、本核对文档。没有更改共享pager/限流/认证/Mock/collectors/生产配置，也没有迁移或清理已有库存。

## 4. 跨范围需求与未解边界

- 主线程已提供gaussdb_instances_path()与共享pager total_key=None；本轮只调用，不修改它们。
- Mock/Probe需与本轮契约保持一致：EVS支持marker/volumes_links，不以count完成；DWS用endpoints[].connect_info；
  ELB不能从guaranteed推物理独享，不伪造兼容章分页；Gauss/ Kafka已由主线程同步版本路径和50条分页。
- Guide `_CAPACITY`需纳入Gauss vcpus；原始memory_size/volume_size与unit_verified标志由主线程维护展示。
- endpoint 主机名和目录发现需由局点配置 / 官方服务目录核实，不能从本表的内部服务键猜域名。
- GaussDB列表内存/磁盘单位、规格补充后的节点/实例容量范围、CSS851正向契约、ELB未公开分页边界仍需明确。
- 当前ECS storage_gb为可关联EVS容量合计，不由flavor.disk推导，也不宣称包含未出现在EVS清单中的本地盘。
- 本轮不清除已保存的旧GB/加密/ELB类型标记；适配层不再产生这些未经证实的值，历史数据是否纠正交主线程核实，
  不能从“新响应缺少单位”擅自覆盖历史人工确认信息。
- API正文核对不代替现场只读验证；没有执行全量/PG/应用浏览器/LAN验收，这些由主线程统一进行。

## 官方参考

固定文档号/章节供复核。主线程提供的网络/RDS/Kafka定位也保留原链接，不换成公共云“相似API”。

[ECS1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100452338/ca0d1efd/lifecycle-management#EN-US_TOPIC_0000002124260517
[EVS1]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/cd60dae4#ZH-CN_TOPIC_0000002240926657
[EVS2]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/cd60dae4#ZH-CN_TOPIC_0000002240926649
[EVS3]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100453160/cd60dae4#ZH-CN_TOPIC_0000002206046290
[VPC1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100452400/e654702c/vpc#EN-US_TOPIC_0000002076345310
[VPC2]: https://support.huawei.cn/enterprise/en/doc/EDOC1100452400/e654702c/vpc#EN-US_TOPIC_0000002076500050
[EIP1]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/ca0d1efd#ZH-CN_TOPIC_0000002112123313
[EIP2]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/d578b42e
[EIP3]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100452355/a27f84b8
[ELB1]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100452378/bd0a2e6b#ZH-CN_TOPIC_0000002112112189
[RDS1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100458141/51d21a18/db-instance-management#EN-US_TOPIC_0000002342919457
[G1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100457467?section=k001
[DCS1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450216/f0c9651f/lifecycle-management#ListInstances
[K1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100450009/bd0a2e6b#ListInstances
[MRS1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100452559/1c9c9c61/cluster-management#EN-US_TOPIC_0000002301999772
[DWS1]: https://support.huawei.cn/enterprise/zh/doc/EDOC1100475350/2b0530af#ZH-CN_TOPIC_0000002270497945
[CCE1]: https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/daeb8233/cluster-management#cce_02_0239
[CCE2]: https://support.huawei.cn/enterprise/en/doc/EDOC1100453109/f318fe13/node-management#cce_02_0244
