# 全系统复核：CCE / Kubernetes 容器事件（2026-11-02）

## 1. 范围、证据与安全边界

- 复核链路：CCE 管理面集群清单 → 原生 Kubernetes Pod / Event 分页 → Pod 状态差分、事件去重及背压指标 → 工作负载观测 → 事件查询、统计和时间线 → 容器事件页面。
- 只修改分配范围：`backend/app/k8s/`（不修改 `catalog.py`）、`backend/app/api/v1/pod_events.py`、`backend/app/schemas/pod.py`、`frontend/src/views/pods/`、新专项测试及本文档。模型、迁移、共享前端类型/API、pipeline/rules/catalog、metric_visibility、dashboard、reports 和健康新鲜度均交主代理处理。
- 已读项目 `CLAUDE.md`、`backend/CLAUDE.md`、`frontend/CLAUDE.md`，以及 PDF、Vue、Vue testing 技能及相关参考。
- 保留共享工作树已有改动；未执行 reset / revert / commit / push，未修改公共 CHANGELOG。
- 未读取正式 `.env`、`deploy/data`、`deploy/backups`、证书或凭据；未请求正式 HCS、SMTP、SMS。测试使用 `INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL=''`，临时 SQLite。没有 AOM 请求或 AOM 回退。
- 仅公开访问 Kubernetes 官方文档，未将项目代码、内部 URL、客户数据或认证信息放入远程查询。全部合成对象及数据库测试只证明本地语义处理，**不证明正式 HCS 返回、RBAC、版本兼容或生产连通性**。

## 2. PDF 主证据：印刷页与物理页

主材料：`/data/hcs/logs/pdf/cce-api.pdf`；复用 `/tmp/hcs-system-review-20261102/cce-api.txt` 与 `pdf-inventory.json`。

- 文档版本：03（2025-07-17）；1323 个物理页。
- SHA-256：`1b1332d3a68b92b62f059da097737a4de9cb80ddda4151e687835fb87b1f5d0f`。
- 本表“物理页”从 PDF 第 1 页起算，不是文本行号。核对了印刷 142 / 物理 148 页的渲染图，确认 URI、参数表和页脚，而非仅依靠文本偏移猜页码。

| PDF 位置 | 印刷页 | 物理页 | 可证实内容与实现约束 |
| --- | --- | --- | --- |
| 第 1 章，使用前必读 | 1 | 7 | 管理面 CCE API 与原生 Kubernetes API 分离；文档说明当前调用不支持 HTTP 长连接。本实现只轮询 list，不引入 watch 长连接。 |
| §4.2.3 获取指定项目下的集群 | 141 | 147 | 管理面按项目取得所有集群；不是原生 PodList。 |
| §4.2.3，URI、表 4-71 / 4-72 | 142-144 | 148-150 | `GET /api/v3/projects/{project_id}/clusters`；Query 参数含 detail / status / type / version，未给出 offset、limit、marker、continue 分页契约。不得把原生 K8s continue 套到管理面。 |
| §4.2.3，表 4-74 / 4-75 | 146-147 | 152-153 | 管理面返回 items 中的 Cluster；据 metadata.uid 识别集群。 |
| §4.2.3，ClusterStatus | 178-179 | 184-185 | Available、Unavailable、Creating、Hibernation、Error 等状态；非 Available 或状态缺失均记录采集缺口，不进入原生对象采集。 |
| 第 5 章，使用 Kubernetes API | 1274 | 1280 | API Server 与 API 网关两条访问方式；网关大规模请求可能触发流控。本文不新增获取证书动作，也不读取证书。 |
| 第 5 章，API 网关方式 | 1275 | 1281 | 原生 URI 使用显式 `{clusterid}.Endpoint/uri`；不能拿管理面 endpoint 替代，不自动探测端点或回退到其他云版本。 |

**PDF 证据缺口：**上述材料没有逐字段定义 PodStatus、EventSeries、原生 list 的 continue / resourceVersion / HTTP 410，也没有定义应用背压指标名、单位、标签、代理处理器的无副作用保证。其本身不能证明部署的原生 Kubernetes 版本、启用特性和 RBAC。以下官方资料只补充原生对象语义，不替代上传 PDF 的 HCS 管理面契约，也不宣称当前网页版本就是该 HCS 的 Kubernetes 版次：

- Kubernetes Pod lifecycle：<https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/>
- Kubernetes API concepts：<https://kubernetes.io/docs/reference/using-api/api-concepts/>
- Kubernetes core/v1 Event：<https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/event-v1/>
- Kubernetes owners and dependents：<https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/>

公开材料的只读缓存位于 `/tmp/hcs-cce-review-20261102/`。没有请求正式集群的 discovery、证书、metrics、exec 或其他探测接口。

## 3. 每类监控项的覆盖与限制

| 项 / 类型 | 本轮核对与修复 | 规则边界 |
| --- | --- | --- |
| RESTART / `pod_restart` | Pod UID、容器名、计数及终止时间参与身份；同名 Pod 替换不比较累计数；计数重置后再次重启不丢失；多次增量不全算成最后一次的退出原因。 | 输出 10m / 1h，资源指标 24h；跨窗口无法定位的次数不输出推测值。 |
| OOM_KILLED / `pod_oom` | 同时读取当前 terminated 与 lastState.terminated；未重启的 OOM 也记录；一次 OOM 仅归因一次，不将 delta=4 全算为 4 次 OOM；明确 restart_counted。 | OOM 终止与容器重启分别计数；Node SystemOOM 不等于 Pod 重启。 |
| CRASH_LOOP / `pod_crashloop` | 包括 init container；运行但未 Ready 不确认恢复；终止、删除、UID 替换分别标注非恢复；无 Ready 证据时不发布 0 的恢复事实。 | 删除资源 / 状态未知的告警收尾由主代理 pipeline 统一处理，不能将其自动写成“已恢复”。 |
| BACKPRESSURE / `pod_backpressure` | 自定义事件和显式探针分开；查询失败、缺样、非有限数、非 Running 断开连续窗口；配置签名变化重计时；部分目标失败不能代表整个工作负载已清除。 | 无原生信号且无成功探针，不产生 0 事实或资源指标。事件过期不构成恢复采样。 |
| EVICTED / `pod_evicted` | 状态与同 Pod UID 的 K8s 事件合并；状态 marker 防止长期留存 Failed Pod 被当作新驱逐；累计 count 跨窗口按未知处理。 | 状态初次发现时间是采集检测时刻，不是上游实际驱逐时间；精确发生时间仍以 Event 时间为证。 |
| PROBE_FAILED | 保留 Unhealthy / ProbeWarning，Pod 与容器 fieldPath 对应；不把健康检查失败变成 OOM / 重启次数。 | 当前可见事件；是否加独立告警规则由主代理 rules/catalog 决定。 |
| SCHEDULE_FAILED | 保留 FailedScheduling；Pending / 无容器状态不会被制造为 Running。 | 同上，不假设调度失败已有独立自动告警。 |
| IMAGE_PULL_FAILED | 保留 ErrImagePull、ImagePullBackOff、镜像类 BackOff；容器状态原因在时间线中保留。 | 不将所有 BackOff 当作 CrashLoop 或重启。独立规则待主代理。 |
| NODE_PRESSURE | Node 内存 / 磁盘等压力事件按 Node 保留，不伪造 Pod 或工作负载归属。 | 节点级规则 / 资源映射待主代理；不能据 Node SystemOOM 推算 Pod 重启数。 |
| SCALED | 保留 ScalingReplicaSet / SuccessfulRescale 等 Normal 事件及实际 involvedObject.kind。 | 提示信息，不擅自升级为故障。 |
| WARNING | 保留未被其他分类覆盖的 Warning，不添加捏造的业务归属。 | 具体告警策略待主代理。 |
| Pod phase / readiness / terminated | Ready condition 为 False 时覆盖容器 ready=True；readinessGates 不被容器布尔值越过；当前终止 reason / exitCode / finishedAt 可见，Succeeded 不计入未就绪数量。 | 历史无 conditions 的数据仍保留原有“无 gates 且普通容器全 ready”兼容分支；正式版本完整 conditions 契约未验真。非 OOM terminated / 单独未就绪的专用规则仍需主代理补齐。 |
| namespace / tenant / project | 清单、状态、事件、清理、观测和 API 保持 tenant + project + cluster 范围；同名不同 kind 不混计；排除 namespace 不等于 Pod 被删除。 | PodProbe 目前是 tenant 级配置；显式 project 级探针绑定需要主代理模型 / 迁移。 |

## 4. 已落地的实质缺陷

### 4.1 身份与归属

1. **同名 Pod UID 替换串用旧 restartCount / CrashLoop 计时。**UID 写入 `PodState.data`、`PodEvent.detail`，替换时重置对比基线；旧持续事件结束原因为 POD_REPLACED，recovery_confirmed=false。事件锚点不得挂接替换后的当前快照。
2. **计数重置后相同 restartCount 被旧去重键吞掉。**重启身份增加 UID / 终止时间；计数增加但 lastState 仍指向旧 OOM 时，新增重启记为原因未知，不重复归因 OOM。
3. **ReplicaSet / 数字后缀 Job / 已删除 Pod 名被猜成 Deployment、CronJob、StatefulSet。**采集器只相信 controller=true 的直接 ownerReference；没有证据的历史 Pod 保持 Pod 归属。`k8s/catalog.py` 原有推断函数未被修改，采集器已不调用这些推断函数。
4. **同 namespace、同 workload 名、不同 kind 的事件混计。**事件聚合键增加 workload_kind。
5. **旧 UID Event 或缺失对象 UID 的 Event 继承当前 Pod 的 owner。**已知当前 Pod UID 时，involvedObject.uid 不匹配或缺失都不继承其归属；记录 identity_precision=NAME_ONLY，而不是伪造 UID 匹配。

### 4.2 状态、次数与恢复

6. **init container 重启 / OOM / CrashLoop 漏采。**纳入 initContainerStatuses，并在容器快照中标注 init。
7. **最后一次 OOM 被用于解释全部重启增量；当前 OOM 但 restartCount=0 漏采。**拆分一个可证实 OOM 与其余未知原因重启；终止和重启分别计数、同一次终止不重复新增。
8. **Ready 只看容器布尔值；Running 被立即当作 CrashLoop 恢复。**使用 Pod Ready 条件和 gates；Running / ready=false 不结束 CrashLoop。terminated / 删除 / 替换显示非恢复且阻止下游 0 恢复事实。
9. **Event.count 累计数全部归入最后时间点，且旧 payload 可以倒退已有次数 / 时间。**存储初始区间与后续增量区间，跨统计边界返回未知；bool / 字符串 / 负数 / 小数不转成 1 / 0，异常不发布受影响的已知 0。倒退证据不覆盖现有记录。
10. **Pod 状态与 Event 同时出现 Evicted 会计数两次。**同 UID / 同 scope 合并为一个 Event，保留原生时间与 count，长期 Failed 状态不重复新建。
11. **K8s 背压事件从列表消失、超过恢复时限，就被当作故障恢复。**标注 EVENT_EXPIRED / recovery_confirmed=false，保留证据缺口；仅有事件过期不会产生背压 0。

### 4.3 探针与只读边界

12. **指标查询失败 / 缺样仍沿用中断前 over_since / under_since，随后误触发或误恢复。**失败打断连续窗口；整个采样周期缺失按照已配置任务周期加抖动容差打断计时；失败进入任务 errors，不隐藏成 SUCCESS + 0。
13. **一个目标成功足以掩盖同工作负载另一个目标失败。**恢复要求所有配置匹配目标的当前有效采样；资源指标同步收紧，不仅仅隐藏事件页的 0。
14. **修改阈值 / 来源 / 目标后继续沿用旧计时。**对探针定义签名，变化时旧事件标记 PROBE_CHANGED / 非恢复，新定义重新累积。
15. **NaN 与有效 0 共存时丢弃坏序列，聚合得到 0；错误 label_filter 被静默忽略，相当于匹配所有序列。**匹配样本任一非有限 / 无效值使本次聚合未知；过滤语法错误、重复标签、空条件在输入校验中拒绝，历史坏配置在运行时也不匹配所有样本。
16. **允许任意应用 GET 路径，GET /restart 等仍可能写入。**输入与运行时双重限制路径为以 `/metrics` 结尾的查询路径，拒绝查询串、fragment、编码、路径跳转、控制字符、任意 action 路径；仍支持 `/custom/metrics`。阈值要求有限数，字符串长度与数据库列对齐。只调用 get_text，不调用 exec / attach / POST / PATCH / DELETE。历史不支持的 path / label_filter 保持可读，以便操作员修改，但运行时不执行。

**只读不是 HTTP 方法的形式保证：**`pods/proxy` 应单独、最小化授予 get 权限，且应由应用方确认 metrics handler 无写副作用。CCE PDF 不包含该应用指标处理器契约，本文不据合成测试宣称正式探针已经验真。

### 4.4 分页、查询与 UI

17. **重复 Pod 快照、缺失 / 非法 restartCount 可能被当成可用快照或 0。**在任何状态更新前拒绝；带 continue 的清单必须有一致 resourceVersion。原有 403 / 404 / 410、重复 token、页数 / 条数预算均失败闭合，不返回部分列表。410 不接受服务端提供的新快照续页混接；下一轮从头采集。
18. **排除 namespace 时把原有 Pod 当删除。**排除范围不参与删除判定或资源下线。
19. **stats 静默只取前 50000 条；时间线静默截断 500 条并用截断后的事件算重启。**stats 改为流式聚合；时间线仍限制可见 500 条，但返回 total / truncated，完整窗口单独计算重启。未知次数返回 null，趋势跨桶无法分配时返回 null。
20. **页面把缺样探针显示为 0 个超阈值，把旧终止态显示成缺少当前原因。**实际模板区分已恢复、目标删除、UID 替换、事件过期、容器终止、探针变更与采样中断；无目标 / 无有效样本显示未知；时间线显示截断提示和当前 terminated exitCode / finishedAt；未知重启不显示为 0。

## 5. 改动文件

- `backend/app/k8s/collector.py`：UID、init、状态 / OOM / 重启、次数区间、异常闭合、恢复证据、探针连续性及聚合。
- `backend/app/k8s/kube.py`：完整清单校验、分页快照约束、代理路径限制、指标缺样与非有限值处理。
- 新 `backend/app/k8s/semantics.py`：直接 owner 身份、metrics 路径、标签对过滤、窗口计数语义。
- `backend/app/api/v1/pod_events.py`：流式统计、窗口未知、UID 事件锚点、时间线 total / truncated。
- `backend/app/schemas/pod.py`：只读探针输入校验；保留历史不支持配置的输出可读性。
- `frontend/src/views/pods/PodEventsView.vue`、`PodTimelineDrawer.vue`、`ProbePanel.vue`：证据状态与展示。
- 新 `frontend/src/views/pods/PodSignalBadge.vue`、`PodProbeResult.vue`：可测试的事件状态和探针结果呈现；props 向下传递，不发业务写请求。
- 新 `backend/tests/test_cce_container_review_20261102.py`、`frontend/tests/podSignalReview20261102.test.ts`。

## 6. 测试证据

日志目录：`/tmp/hcs-cce-review-20261102/`。每批先运行新失败回归，再修改实现；不是仅补全可以通过的断言。

| 失败回归阶段 | 修复前结果 | 证实问题 |
| --- | --- | --- |
| `red.txt` | 26 failed | UID / 计数重置、OOM 归因、init 漏采、Ready、owner 猜测、kind 混计、窗口次数、探针中断、任意 GET 路径 |
| `red-second.txt` | 9 failed, 27 passed | 无证据背压 0、重复 Pod / 坏 restartCount、未重启 OOM 统计、时间线截断 / 替换快照 |
| `red-third.txt` | 5 failed, 37 passed | 资源指标出口的假 0、部分目标失败、定义变更计时、驱逐重复、异常事件对应的假 0 |
| `red-fourth.txt` | 7 failed, 53 passed | 混合 NaN / 0、过滤语法被忽略、terminated 被下游恢复事实清除 |
| `green-fourth.txt` | 60 passed | 专项回归通过，含额外事件分类与排除 namespace 正向覆盖 |
| `red-identity-gap.txt` | 1 failed, 60 deselected | 缺对象 UID 的 Event 被错误归入当前 Pod owner；修复后纳入最终回归 |
| `compat-second.txt` | 169 passed | 第一轮与现有 CCE / Pod API / scope / PDF / resource identity 兼容回归 |
| `cce-final.txt` | **160 passed** | 最终 CCE / Pod / scope / native contract / resource identity / 指标 / pipeline 范围回归，含新增 **62** 条专项及 50001 条统计容量验证 |
| `backend-final.txt` | 237 passed, 2 failed | 更广交叉检查；失败分别为 ECS UNKNOWN 事实断言、DataArts submitted_time 未进入 catalog，均已报告主代理，未跨界修改 |
| `frontend-final.txt` | **731 passed / 43 files** | 当前共享工作树前端完整测试通过；不是宣称全部为本模块新增 |
| `frontend-build.txt` | **built successfully** | 使用空 envDir、输出 `/tmp/hcs-cce-review-20261102/frontend-dist`；不读取前端正式环境文件、不覆盖共享 dist |
| `mypy-final.txt` | **Success: no issues found in 7 source files** | 最终复查已无本范围或其检查依赖的诊断；先前 GaussDB 临时阻塞已由共享工作推进消除 |
| `frontend-typecheck-final.txt` | **1 个边界外错误** | `src/views/reports/ReportView.vue:220:55`：ledger.storage_gb 可能为 null；未修改 reports |

专项：`INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' .venv/bin/pytest tests/test_cce_container_review_20261102.py`。

前端两个专项文件：19 passed（新增 11 个真实 SFC 模板 SSR 渲染用例，另有原身份隔离 8 个用例）。不是仅对组件源码做字符串快照断言。

四轮和身份补充共保存 **48 个修复前失败用例**；另有分类 / 兼容 / 容量等正向用例。范围内 ruff、mypy 和 `git diff --check` 通过。早期 GaussDB / RuleListView 静态错误已随共享工作推进消除；最终前端完整类型检查仅剩共享 `ReportView.vue:220:55` 的 ledger.storage_gb 可空错误。前端测试和构建通过，不把全局类型检查冒充通过。

最终后端范围命令：

```sh
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' .venv/bin/pytest \
  tests/test_cce_container_review_20261102.py tests/test_pod_events.py \
  tests/test_pod_scope_isolation.py tests/test_cce_hcs851_contract.py \
  tests/test_resource_identity.py tests/test_review_collection_metrics.py tests/test_pipeline.py
```

没有为了单模块重复跑 3230 条后端全量；只拓展到与 CCE 输出有关的现有用例。正式 API 联调、正式版本契约及浏览器整页交互不在合成 / SSR 模板测试的证明范围内。

## 7. 已报告主代理、不能在本分工内擅改的事项

1. **共享类型需接收未知。**`PodEventStats.restarts`、Top workload restarts、`PodTimeline.restarts_in_window` 可为 null；restart trend 元素可为 null；timeline 新增 total / truncated；Probe last_result 新增 observed / status。请更新共享 `frontend/src/types/index.ts`，不要在客户端把 null 自动填成 0。本轮只在容器页做安全显示。
2. **pipeline / 健康新鲜度。**缺少 fact 的观测 / 未输出某项，不是该项恢复。BACKPRESSURE 和 CRASH_LOOP 的非恢复收尾，不得因本轮未出现对象、删除、禁用探针、采集失败而转成“恢复通知”。旧资源 metrics 中遗留的 0 必须遵循来源 / 样本新鲜度，不可用旧缓存代表当前正常。
3. **规则覆盖。**现有自动观测为 restart / OOM / crashloop / backpressure / eviction。健康检查、调度、镜像拉取、节点压力和其他 Warning 虽有事件，尚不能据此宣称已有完整自动告警。非 OOM terminated、Pod not-ready 的独立规则和指标目录需主代理决定并补齐。
4. **owner 上溯与已有资源身份。**当前只使用可证实的直接控制 owner。因此真实 ReplicaSet / Job 不再被猜成 Deployment / CronJob。若产品要求上溯，需明确的 ReplicaSet / Job 父对象只读契约及 RBAC；本轮未自动增加查询或回退猜测。旧推断的 Deployment resource_id 与新 ReplicaSet resource_id 之间，是否迁移规则 / 告警 / 监控开关，由主代理统一决定，不应把身份纠正解释为故障恢复。
5. **原生版本证据。**请补充该 HCS 集群版本对应的离线 OpenAPI / 脱敏样本与 RBAC 说明，特别是 EventSeries、readiness、init sidecar 与代理 metrics。无需凭据，不进行正式 API 探测；不能以公有云 URL 或本地 Mock 代替。
6. **历史精度。**旧 Event 缺 UID / 次数区间无法精确重建；跨窗口累计计数返回未知。为限制 JSON 增长，超过 512 个次数区间时合并旧区间，合并区间跨统计边界时同样未知。旧无 UID 时间线无法证明同名对象始终是同一 Pod；完整历史容器快照 / UID 索引需模型及迁移另行设计。
7. **探针项目绑定。**当前 PodProbe 是 tenant 配置，cluster_id 相同的多个项目会按各自只读 client 执行，采集存储仍按 project 隔离；无法配置只绑定其中一个 project_ref。若需此能力，由主代理新增模型 / 迁移和共享契约，不能把现有 tenant-wide 语义偷偷改掉。
8. **共享静态检查与交叉回归。**早期 GaussDB / RuleListView 错误已消除，最终 mypy 通过；最新前端类型错误是 `views/reports/ReportView.vue:220:55` 的 ledger.storage_gb 可空访问。建议在 reports 中保留未知语义，不用非空断言 / 填 0 掩盖它。更广交叉回归曾有 ECS UNKNOWN 状态事实与 DataArts submitted_time 目录的两处断言冲突，已交主代理判断共享变更后的期望，不在此擅改。所有报告中的检查结果按各日志保存时刻陈述，避免把共享并发中间态冒充本模块失败。
9. **历史覆盖不能凭空补足。**首次只回填一小时内可证实的最近一次终止，不能据 restartCount 的最后原因还原全部历史；Event 自身还受上游保留期影响。统计是本系统已采集记录的次数及其可定位窗口，不宣称是正式集群完整历史。页面已明确说明；需要完整窗口覆盖 / 采集中断的恢复门禁时，由主代理在共享 pipeline / freshness 建模，不能填补正常 / 0。
