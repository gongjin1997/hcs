# HCS 8.5.1 上传 PDF 与现场日志核查

核查日期：2026-10-28。以本次上传的十一份组件 API 参考为依据，PDF 页码指文件页码，含目录。仅核查平台实际使用的查询接口及其依赖，不调用文档中的创建、修改或删除接口。

## 现场错误

- `api.log` 时间范围为 2026-10-28 05:39:41–06:01:39 UTC；业务服务失败均为 HTTP 状态 0、DNS 解析失败，尚未收到相应业务 API 的 HTTP 响应。
- ECS、DCS、GaussDB、MRS 各 9 次失败，CCE 18 次。原连通性流程在 CCE 列表探测后再次获取集群，造成重复失败；已复用首次结果。
- 当前日志不足以判定实际解析的服务域名是否正确，不能把 DNS 问题归因于接口 404、AK/SK 权限或证书。
- 原日志有一条 `/system` 的浏览器 Error 上报，没有脚本帧或原始异常。不能由该条记录确定具体 UI 故障。

## 文档来源

| 文件 | PDF 页数 | SHA256 |
| --- | ---: | --- |
| cce-api.pdf | 1323 | `1b1332d3a68b92b62f059da097737a4de9cb80ddda4151e687835fb87b1f5d0f` |
| dataartsstudio-api.pdf | 3288 | `30d1ffbeeeea922c0085afd56e080c41c32f6fa2f36952f022a39be3d4528af7` |
| dcs-api.pdf | 420 | `661c30babaffb3843005ca44c83d03327ac064ca385072c39c5416d8d60ef486` |
| dws-api.pdf | 529 | `bb3b0965fc6f00fbeb702b86159ef6631c36cca57be436d8a51fa1f9a78f3313` |
| ecs-api.pdf | 512 | `5cc7822e4dfe71279264994fd3e4d550b79fca3e9005d0df95485e260e3e4048` |
| eip-api.pdf | 93 | `1d528307b879533ecbf75a50a9ef55023d0c5be3da3c415f75388046e840a591` |
| elb-api.pdf | 813 | `c9ad73190ad5d69d473dec71a524c825665afb0d5433c84581bfd9538f5d470b` |
| evs-api2.pdf | 271 | `52eca3c2bde6489e6829a284ee77e9c81cca7c5c218b9da420abd1eb047a7b33` |
| gaussdb-api.pdf | 1101 | `3d7d543e9975acba2a665b5ee1c4d123952850d8e31b5158a9317f4022cfd760` |
| mrs-api.pdf | 160 | `53b66912be0e326f35b9a3b1f4f6f3698b14236eaede127b3a2065d5ddfb968e` |
| vpc-api.pdf | 524 | `77964e5c813a050ac97a37213ac16d4f88a1c227192981745bf3a1a93d06afd6` |

## Endpoint 与网络层

- ECS 文件第 12–13 页、DCS 第 7–8 页、MRS 第 156–158 页明确使用底座参数 `region0_id` 与 `external_global_domain_name`。不能从控制台主机或资源空间名称猜测这两个值。
- 正式租户设置不自动改写。连通性结果显示实际服务地址及配置来源；出现 `console.` 前缀或与项目名重合的区域后缀时提示人工核对导出表。
- `NETWORK` / `IAM.NETWORK` 保持兼容，同时增加固定 `network_reason` 与 `retryable`，不回显原始异常、代理口令或响应正文。
- 已确认的域名不存在、证书校验失败、网络访问被拒绝不自动重试；临时 DNS 故障与既有瞬态网络错误仍使用原重试预算。
- Endpoint 在签名和发送请求前拒绝内嵌账号密码、查询串、片段、错误端口、非 HTTP(S) 协议与未展开变量。
- 现场脚本新增 `--services all` 和 `--project-ref`：读取指定租户及启用项目的端点，逐服务检查 DNS/TCP/TLS/HTTP，不读取 AK/SK/密码，也不执行认证。相同地址只探测一次。

## 服务契约

| 服务 | 核对范围与实际 PDF 页 | 结论与本次修复 |
| --- | --- | --- |
| ECS | §5.2.5，第85–97页，关键 Query/Response 第86/88页 | 原路径正确；`tenant_id` 路径参数实际是项目 ID。offset 为页号，从1开始；逐页严格验证整数 count。巡检、台账、连接测试和 Probe 均拒绝缺总数的异常清单。 |
| CCE | §4.2.3 第147–187页；§4.3.3 第447–486页；第5章第1280–1281页 | 管理面 clusters/nodes 的 items 为完整列表，不能套用 K8s continue。拒绝非空或畸形管理面续页标记；保留原生 K8s 分页。复用集群列表，选择 Available 且 UID 有效的集群检查一个命名空间样本。 |
| DCS | §4.1.2 第32–35页；§4.2.6 第72–74页 | 实例 v2 与单数 statistic 路径正确；统计查询不加分页。修复 `capacity_minor` 小规格值被巡检覆盖。内存统计为 MB，规格容量为 GB，used_cpu 是累计秒，不转换成 CPU 使用率。 |
| GaussDB | §4.3.4 第83–97页；§5.2.3 第876页；§5.2.4 第894页；§5.2.6 第908–910页 | 默认保留 `/gaussdb/v3.3/...`。仅显式 v3.1 的 flavor_info.mem 已声明 GB；其他接口族保留原值及未知单位。修复台账与巡检内存口径，补充集中式和混合部署枚举。 |
| MRS | §6.1.3 第89–103页，关键表第92页、示例第98页 | 集群 v1.1、currentPage/pageSize、clusterTotal、秒级时间戳正确。总数兼容字符串整数和整数，拒绝缺失、非法状态和跨页重复标识。 |
| DataArts | §5.3.3 第313–318页；§5.3.15 第356–359页；§5.3.16 第359–363页 | 保留 V1 路径、workspace、从0起页号和毫秒窗口。未知节点状态记局部失败；报表与 XLSX 补齐 pause、manual-stop，保留原始状态。 |
| ELB | §1.2 第11页；§5.1.1 第40–48页，Query 第41页、PageInfo 第47页 | 默认服务名固定 vpc，已修复全局默认和前端覆盖提示，保留租户显式覆盖。Probe 去掉无声明的 limit 和 member_count；校验 current_count 与本次清单长度一致，不将 next_marker 非空当作还有下一页。 |
| DWS | 8.2.1.230，§5.2.6 第42–51页 | 保留 v1.0 无分页查询、内网 endpoints 与公开地址区分。count 已提供时须与全量列表一致；官方示例省略 count，因此缺失仍兼容。Probe 与台账统一，不能将独立节点接口的 offset/limit 移植过来。 |
| EVS for 云块存储 | 25.3.0，§5.6.28 第222–227页；第7章获取项目ID | 当前 v2 cloudvolumes/detail 有文档依据，tenant_id 与 project_id 同义。Probe 去掉非法 offset=0 和未声明的 count；保留 marker/volumes_links 的部分列表保护与不跟随链接策略，encrypted 字段不支持，仍不生成加密结论。 |
| VPC / 子网 | 8.5.1，第183–185、214–218页；认证第22–24页 | 现有 v1 查询、vpc Endpoint、项目作用域、limit/marker、资源ID及原生状态已匹配，未重复改写。新增真实同步 HcsClient 的只读契约回归。 |
| EIP | 8.5.1，第43–46页；认证第15–17页 | 保持 vpc Endpoint 下 /v1/{project_id}/publicips，顶层带宽字段、create_time 和嵌套 port_details 语义已核对，未按旧草稿更改已正确字段。数值带宽单位的本版证据不足，不把已有显示口径宣称为本次PDF新证实。 |

### 上传版本与边界

- DataArts 文件为数据治理中心 **2.11.1.3 / 文档03 / 2025-07-30**；MRS 为 **3.5.1-LTS / 文档01 / 2025-03-30**；GaussDB 为 **25.1.30 / 2025-03-30**。核对的是这些版本的具体查询契约，不声称不同版次整体等价。
- MRS V2 `/v2/{project_id}/clusters/{cluster_id}/job-executions` 未被上传 MRS PDF 覆盖。保留原有兼容配置、未核实标记及显式时间单位，不猜 URI、分页或 elapsed_time 单位；Probe 单独输出 `contract_status=unverified`。
- 新增文件：ELB/VPC/EIP 为 **8.5.1 / 文档01 / 2025-03-30**；DWS 为 **8.2.1.230 / 文档01 / 2024-10-30**；EVS 文件为 **for 云块存储 25.3.0 / 文档01 / 2025-03-30**。不能据文档文件名推断现场产品或把这些版本整体视作此前公开资料版本。
- VPC/EIP 的 AK/SK 示例与部分限制/参数表表述存在差异，未由本次业务响应证实现场认证支持。保持当前认证选择，DNS恢复后再据实际HTTP响应验证，不将Mock签名通过当作正式IAM授权通过。
- DCS `instance_num` 表述为“实例个数”，未充分说明是全量总数还是分页内计数；include_failure 的描述也不足以证明行过滤。保留当前行为并记录此限制，未擅自增发逐状态查询。
- CCE PDF 委托原生 Kubernetes API，未逐字段展开 Pod/Event continuation。仅据它核对网关访问方式；原有 K8s 分页和快照保护由隔离回归验证，不把实现测试冒充文档证据。
- DataArts executeTime 的字段表与样例存在单位/数值矛盾；仍按有效起止时间计算时长，不据样例猜测其他单位。
- 未取得业务服务成功响应，所以没有据本次 DNS 失败推断 AK/SK 权限、现场组件开通情况或具体接口版本。

## 验证

新增回归先复现缺陷，再验证最小修复；覆盖完整性、容量回写、原始状态、任务运行状态、报表与原生 XLSX、租户/项目选择及无凭据网络诊断。最终 `make check` 通过：后端1646项通过、21项条件跳过、4项子测试通过；前端452项通过；脚本回归147项通过；Ruff、mypy、TypeScript和生产构建通过。既有ARM64镜像在禁止网络的短命容器内完成正式发布管理器 prepare/verify 与新模块导入、重试分类、版本单位、ELB端点、Probe参数和Web文件验证。浏览器插件不可用，使用已安装 Playwright CLI 的独立本地会话，合成 API 响应，未复用现有浏览器或连接正式后端。

## 现场复核

```bash
cd /data/hcs-inspector
bash deploy.sh update /离线更新包路径.tar.gz
bash /工具目录/network-check.sh --tenant 实际租户编号 --services all > /工具目录/network-services.json
```

目标机继续使用现有 Docker 镜像与独立 `docker-compose`；不要求宿主 Python、Node 或 v2 插件。既有 TLS 开关、主密钥、数据库及采集选择保持不变。

HTTP 探测收到 401/403 仅证明链路可达，不证明认证和业务权限有效。内网 DNS、底座导出参数与正式环境业务连通性须在现场核对，不以本机隔离测试替代。

## 交付

仅代码更新包位于 `/data/hcs/deploy/api-pdf-fix/`，发布标识 `20261028-api-pdf-1`。包大小 1,067,033 字节，SHA256：`dceeea34219d3b5c855de1286856ab90ab8d099e0782b9f001905949f308a035`。附只读现场诊断脚本、核查说明、verification.json及SHA256SUMS；没有镜像、客户日志、PDF原文件、.env、凭据、证书或前端source map。

代码包依赖清单与上一网络诊断代码包完全一致；复用既有Python3.12运行时和独立docker-compose。当前用户的TLS选择、采集开关、任务及数据库均由既有部署入口保留，本轮未在目标内网机实际应用。
