# 全系统复核：DataArts、MRS 采集与作业报表（2026-11-02）

> 补充状态：主代理随后授权共享层修复，并要求本轮实现真实24h P90。原文的7项共享交接与P90待补是首轮快照，当前状态/字段/0014/测试详见 `全系统复核_持续时间恢复证据与P90_20261102.md`。


## 1. 范围、依据与安全边界

本次复核从上传 API PDF 到采集 Observation、持久化作业快照、报表 SQL、前端列表/抽屉及 Excel，限于 DataArts 实时/批作业、MRS 集群/作业及专项报表。

- 已读仓库 `CLAUDE.md`、`backend/CLAUDE.md`、`frontend/CLAUDE.md`；保留共享工作树已有改动，无 reset/revert/commit/push，无公共 CHANGELOG 修改。
- 未读取正式 `.env`、`deploy/data/`、`deploy/backups/`、证书或凭据；没有调用正式 HCS、SMTP、SMS 或 AOM。
- 所有 pytest 命令均显式设置 `INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL=''`，使用独立 SQLite。
- PDF 文本复用 `/tmp/hcs-system-review-20261102/`；哈希与页数来自该目录 `pdf-inventory.json`。另将实际页 358/92 渲染为图像核对表格与页脚。
- 本地合成 HTTP 响应和浏览器拦截仅验证解析、隔离和界面行为，不证明正式 API 可用；不引用公有云同名接口替代本上传版次。

| 上传材料 | 版次 | 物理页数 | SHA256 |
| --- | --- | --- | --- |
| `dataartsstudio-api.pdf` | DataArts Studio 2.11.1.3，文档版本 03，2025-07-30 | 3288 | `30d1ffbeeeea922c0085afd56e080c41c32f6fa2f36952f022a39be3d4528af7` |
| `mrs-api.pdf` | MRS 3.5.1-LTS，文档版本 01，2025-03-30 | 160 | `53b66912be0e326f35b9a3b1f4f6f3698b14236eaede127b3a2065d5ddfb968e` |

**页码规则：本文物理页从 1 起数；“印刷页”为 PDF 页脚页码。** DataArts 本段偏移为 +20，MRS 本段偏移为 +4。不得把当前互联网页面修订版当作上传 PDF。

## 2. 逐项监控契约

| 监控项/链路 | PDF 印刷页 / 物理页 | 实际字段与确认情况 |
| --- | --- | --- |
| 实时作业列表及空间隔离 | 294–297 / 314–317 | `GET /v1/{project_id}/jobs`；`jobType=REAL_TIME`；`offset` 为从 0 起的页号；`total`、`jobs`；`workspace` 为 Header，不设置查询 default 空间 |
| 实时作业异常、非预期停止 | 337 / 357 | 明细 `status` 仅 `STARTING/NORMAL/EXCEPTION/STOPPING/STOPPED`；可选，缺失不能作为 NORMAL |
| 实时作业启动/停止超时 | 337 / 357 | `status` 有上述依据；`startTime/endTime/lastUpdateTime` 为 Date，未约定数值单位；本地 `status_since` 是状态观测时间，不是正式 API 启停时刻 |
| 实时节点异常 | 338–339 / 358–359 | `nodes[].status` 独立于父作业状态且可选；枚举同上；官方成功样例未给节点 status，不能补 NORMAL、零异常或节点状态起始时间 |
| 批实例列表、最新实例 | 339–343 / 359–363 | `GET /v1/{project_id}/jobs/instances/detail`；workspace 同样通过 Header；`offset` 是从 0 起页号；起止查询范围不超过 3 天，现有实现使用最近 24 小时 |
| 批实例失败/运行异常 | 340、342 / 360、362 | `status=waiting/running/success/fail/running-exception/pause/manual-stop`；不是批作业调度状态 `SCHEDULING/STOPPED/PAUSED` |
| 批实例运行时长 | 342–343 / 362–363 | `startTime/endTime` 为 Long 时间戳；以起止差计算分钟；运行中且无结束时间时使用当前时间；缺起点、时间倒置等保持无数据 |
| 批实例提交/调度明细 | 342 / 362 | `submitTime`、`instanceId`、`instanceType`；后者 `0/2/5/6/7` 区分正常/手工/补数据/子作业/单次调度；展示原始值，不猜业务含义以外的状态 |
| 批作业超时 P90 | 342–343 / 362–363 | **PDF 没有 `p90_minutes`**；`executeTime` 字段表称 Long 毫秒，但示例为 `0.3`，与 18 秒起止差冲突；不能拿它猜单位，也不能补固定 30 分钟或 P90=0 |
| MRS 集群列表/异常/冻结 | 86–88 / 90–92 | `GET /v1.1/{project_id}/cluster_infos`；`currentPage/pageSize`；`clusterTotal` 为 String；响应 `clusterState` 的九种小写状态明确列出 |
| MRS 集群节点数/版本 | 88、91 / 92、95 | `totalNodeNum` 为 String，`clusterVersion` 为 String；保留实际返回，不把缺失变为 0 |
| MRS 作业失败/排队/运行时长 | 概览 3–4 / 7–8；全 PDF 160 页检索 | **未列出既有兼容路径 `/v2/{project_id}/clusters/{cluster_id}/job-executions`**；不能据此确认状态枚举、分页、时间过滤/时间戳/elapsed_time 单位或局点支持性 |

说明：DataArts 批实例接口亦可返回实时作业关联子作业实例（印刷 339/342，物理 359/362），并未承诺实例列表含 `jobType/engine`。现有 Flink 面板只根据已保存、明确返回的引擎/作业类型识别，不按作业名字猜引擎。缺少这些扩展字段时，Flink 面板不是所有 Flink 作业的完整清单。

## 3. 已复现并落地的问题

所有问题先加入失败断言，再修改实现。原始失败输出见第 6 节，不以“测试通过”替代正式 API 的版次证据。

| 编号 | 实质问题与失败证据 | 本轮改动 |
| --- | --- | --- |
| DA-MRS-01 | 当前 DataArts 快照中的 `success/RUNNING/SUCCEEDED/FAILED` 等非对应来源枚举被通用别名表认成运行/成功/失败；6 个参数化用例均先失败。依据：印刷 337/342，物理 357/362 | `job_report_service.py` 为有源状态字段的当前快照使用 PDF 精确枚举。若未知原文与标准展示码重名，使用 `UNRECOGNIZED:<原文>` 聚合键，避免未知 SUCCEEDED 进入成功统计；源 `status_raw/execution_state` 保留原文，UI 中性显示，Excel 不计作成功/失败 |
| DA-MRS-02 | 报表及 UI 把 MRS/实时作业的 `1792886400` 无条件当毫秒，Excel 得到 1970 年；后端 2 例及前端 2 例先失败。依据：实时 Date 表印刷 337/物理 357；MRS 作业接口缺契约 | Excel 与 `detailText` 仅对确认单位的 DataArts 批实例数值时间做日期转换；其他源保留原值，不猜秒或毫秒 |
| DA-MRS-03 | 采集器写 `submit_time`，报表只读 `submitted_time`，真实 `submitTime` 丢失；2 例先失败。依据：印刷 342/物理 362 | 采集器统一写 `submitted_time`；报表兼容已有 `submit_time`，但不覆盖新快照显式 null；补出 `instance_type` 详情。Excel 提交时间验证为日期 |
| DA-MRS-04 | `nodes=None` 在报表被改为 `[]`，把缺字段混成已采到零节点；失败断言 `None != []`。依据：印刷 337–339/物理 357–359 | 保留 `nodes=None`；Excel 可安全导出未知节点快照，节点数/异常数均保留 null，不生成 0 |
| DA-MRS-05 | 同一批实例 ID 以数值 `17` 与字符串 `"17"` 混返时，可绕过重复检测并接受矛盾清单。依据：印刷 342/物理 362 的 Long 身份字段 | 重复检测使用已有 `_millis()` 的标准化 ID；异常清单丢弃该 workspace 的观测并报错，其他空间不受影响 |
| DA-MRS-06 | MRS 两页若返回同一 job_id、不同状态，整页哈希不同而通过共享分页检查，采集器又折叠为一条，看似完整 | 在每个 cluster 内核对非空字符串 job_id，不重复计算或接受冲突执行；局部失败不丢弃其他 cluster。只属兼容适配的完整性校验，未宣称该字段获上传 PDF 确认 |
| DA-MRS-07 | MRS clusterState 缺失/空/未知仍作为成功的状态观测，未报告证据不足；3 例先失败。依据：响应九种状态印刷 88/物理 92 | 仅 PDF 响应枚举进入 cluster_state 规则事实；缺失/未知保留原始展示、无正常事实，并将本轮标为局部异常。查询参数表出现的 partial-error 未自动扩展为响应枚举 |
| DA-MRS-08 | 后端已把 `pause` 归一为 PAUSED，前端没有标签/筛选项，展示“未识别” | 增加“暂停”标签与 DGC/批作业筛选项。依据：印刷 340/342，物理 360/362 |
| DA-MRS-09 | 列表传递 project_ref/resource_type/job_type/status_raw/lifecycle/has_alarms，前端导出却丢掉这些筛选，可能导出更大范围 | 列表/导出复用显式筛选白名单；查询类型补齐已存在的后端参数；只有列表附分页，tenant 参数仍来自授权作用域 |
| DA-MRS-10 | MRS 作业兼容限制仅存在采集代码注释/stats，报表与工作簿未说明，容易被误认为正式 API 已确认 | 列表 data_notes 与 Excel 汇总“数据说明”同步披露 MRS PDF 缺契约、数值时间保留原文、批任务 24 小时与缺 P90 限制 |
| DA-MRS-11 | 报表直接回传数据库历史 health=OK，没有有效评估、缺字段或评估过期时仍显示正常；新增健康度用例中 4 例先失败 | 按主代理新契约调用 `app.services.inspection_health.current_health`；列表与 Excel 共用，缺元信息/零有效规则/部分不可判定/过期均 UNKNOWN；新鲜且有效才 OK；已有 MAJOR 等异常不因缺数据消失，读取不写库 |

保留兼容边界：仅有旧 `status_raw`、尚无 `job_status/instance_status` 源字段的历史记录，保留原有文字别名兼容展示；它不是本上传 PDF 的实时契约证明。新采集快照已按 PDF 枚举识别，历史数据是否需要批量标注/重采由主代理统一决定，不在本次无授权迁移。

## 4. 交主代理的共享层复现与建议

专用文件：`backend/tests/test_job_duration_pipeline_review_20261102.py`。只创建回归，不改 shared pipeline/rules/catalog/bootstrap/resource_guides。以下均为纯本地 Observation/规则/数据库行为，无云 API 证明含义。

1. **MRS 新执行复用旧排队时长**：同一 `cluster/name` 首次 job_id=old、状态 ACCEPTED；20 分钟后 job_id=new、状态仍 ACCEPTED。当前按 Resource.status_since 得到 20 分钟，错误触发新执行排队超时。建议按执行 ID 变更重置计时与连续计数；缺执行 ID 时不可证明某个执行连续等待。MRS V2 作业契约仍需局点确认，不能用未确认的 submitted_time 猜等待起点。
2. **实时节点复用父作业时长**：父作业 NORMAL 已观测 30 分钟，新节点首次 STARTING，节点自定义超时规则立即触发。PDF 印刷 338/物理 358 给独立节点状态，不给节点起始时间。建议按节点身份与节点状态维护本地观测起点，不能用父作业 status_since。当前内置节点异常规则无时长条件，此例证明自定义节点时长规则风险。
3. **Observation 明确时长被覆盖**：节点事实给 `status_duration_min=20`，当前流水线无条件用父资源新建时长 0 覆盖，导致应触发的节点时长规则不触发。建议明确事实/采样元数据的可信来源及优先级；至少不能将所有 check_item 的持续时间强行设为父资源状态时长。
4. **MRS 未知状态解除排队告警**：`R-MRS-JOB-003` 的 `job_state != 'ACCEPTED'` 会把 `FUTURE_STATE` 当恢复。缺字段已由表达式 MissingFact 阻止恢复，但任意未知字符串仍有风险。建议采用明确的兼容状态恢复证据或按契约可信度跳过判定，不得从未识别值推正常。
5. **连续异常命中跨无样本拼接**：EXCEPTION 命中一次 → 空 Observation 集合 → EXCEPTION 再一次，仍凑满 for_count=2 并 FIRE。两次有效样本间隔 10 分钟，且中间明确缺采，不能当连续命中。
6. **连续恢复确认跨无样本拼接**：活动异常 → NORMAL 恢复一次 → 空 Observation 集合 → NORMAL 再一次，仍凑满 recover.for_count=2 并 RESOLVE。建议在目标任务缺采/采集失败/过期时清零相关指纹 hits/ok_hits，同时保持活动告警；在 EvalState 保存任务身份、最近有效评估/采样时间，不能被其他 check_item 的新鲜度掩盖。

7. **同一父作业内节点缺采也会拼接**：父作业每轮 NORMAL 都有效，node-a 的 EXCEPTION → 本轮节点无状态观测 → EXCEPTION，仍凑满节点 for_count=2。不能只对整轮空列表/整个任务 FAILED 清零，还需覆盖相同父资源下缺失 check_item/sub_object。DataArts 官方样例允许节点状态缺省（印刷 338–339/物理 358–359），这不是合成 API 才有的场景。

共享层已能在进入规则且 MissingFact/ExprError 时清零计数；**未产出 Observation 或未进入评估时没有这一步**，因此仅新增 inspection_monitoring 元信息不能自动解决连续性问题。

另有两项证据/设计缺口须统一处理：

- `R-DGC-BAT-003` 依赖 p90_minutes；上传 PDF 无此字段，当前没有受证据支持的近 7 天样本基线。已有代码只兼容显式扩展字段，不能将扩展视为正式 API 字段。建议未建立本地历史/样本量/有效执行口径前显示不可判定或停用该规则，不补固定值；未来窗口若按 API 查询，必须遵守每次不超过 3 天且有完整分页，并明确窗口与最小样本业务口径。
- 最近 24 小时实例窗口不覆盖所有更早开始且仍在运行的实例；报表是最新本地快照，不是执行历史或完整失败率。若需覆盖超长任务，须另行确认受 PDF 支持的查询与采集策略，不能声称当前清单已完整覆盖。

## 5. 改动路径

实现文件：

- `backend/app/collectors/dataarts.py`
- `backend/app/collectors/mrs.py`
- `backend/app/services/job_report_service.py`
- `backend/app/reports/job_excel.py`
- `frontend/src/api/job-reports.ts`
- `frontend/src/types/job-reports.ts`
- `frontend/src/views/job-reports/report-utils.ts`
- `frontend/src/views/job-reports/report-config.ts`
- `frontend/src/views/job-reports/JobReportPanel.vue`
- `frontend/src/views/job-reports/JobReportDrawer.vue`

新增专用测试：

- `backend/tests/test_system_review_dataarts_mrs_20261102.py`
- `backend/tests/test_job_duration_pipeline_review_20261102.py`（共享层跟进的失败复现）
- `frontend/tests/jobReportsReview20261102.test.ts`

按新健康契约对齐旧测试：`backend/tests/test_job_reports.py` 只将缺评估元信息的旧 OK 断言改为 UNKNOWN，修改前已报告主代理，未伪造健康评估元信息。

`backend/app/hcs/dataarts_api.py`、`mrs_api.py` 当前不存在，未为补模块制造其他 API；作业 API 路由已具备只读权限、分页/条件校验，本轮未改 `backend/app/api/v1/job_reports.py`。无模型变更、无迁移、无新依赖。

### 新增/变更字段登记（供主代理统一补 resource_guides）

| 字段 | 精确语义/来源 | 单位、空值与兼容 |
| --- | --- | --- |
| `dgc_batch_job.attrs.submitted_time` | 实例提交运行时间，源 `Instance.submitTime`，印刷 342–343/物理 362–363 | Unix 毫秒时间戳；有效非负整数/整数字符串经 `_millis` 归一为 int；非法/缺失为 None，无默认 0。替代写入名 `submit_time`；旧快照读兼容，不覆盖显式新 None |
| `dgc_batch_job.attrs.instance_type` | 既有采集字段，本轮仅增加报表白名单/展示；源 `Instance.instanceType`，印刷 340/342、物理 360/362 | 无量纲原始枚举：0 正常/2 手工/5 补数据/6 子作业/7 单次；未返回 None，不扩展 API 枚举或推断引擎 |
| `facts.cluster_state` | 既有 MRS 集群事实，本轮收紧有效性 | 只写 PDF 响应的九种字符串状态；缺失/未知时省略，不写 running/0 |
| `display_status/status_counts` 中 `UNRECOGNIZED:<原文>` | 仅报表派生的未知状态聚合键 | 非持久化 attrs/metrics/facts；原状态字段保持原文，避免未知文本与标准成功/失败码碰撞 |

本轮**没有新增 metrics/facts 键**。`attrs.inspection_monitoring` 是主代理的新契约，本分工仅通过 current_health 读取；不在此擅自登记或修改公共 resource_guides。

## 6. 验证证据与复验命令

### 失败证据

均存 `/tmp/hcs-dataarts-mrs-audit/`：

- `backend-red.txt`：首轮新增回归 **9 失败、2 通过**；数值时间错误出现 `1970-01-22`、未知集群状态进入 facts、重复实例/执行被接受、nodes null 被改为空列表等。
- `frontend-red.txt`：前端初始 **4 失败**（pause、两个来源时间单位、导出筛选不一致）。
- `status-red.txt`：DataArts 当前快照状态别名 **6 失败**。
- `submit-red.txt`：提交时间字段断链/历史兼容 **2 失败**，另 18 例通过。
- `health-red.txt`：健康元信息新增 **4 失败、2 通过**。
- `pipeline-red.txt`：最初共享持续时间/未知恢复 **4 失败**。
- `counter-gap-red.txt`：共享缺采命中/恢复拼接 **2 失败**。
- `node-gap-red.txt`：父作业仍有观测、仅子节点缺状态的连续命中 **1 失败**。

### 通过证据

首轮相关后端组 `219 passed in 10.80s`（加入健康契约前）；接入健康度后的同组最终 **225 passed in 11.98s**，覆盖既有 DataArts/MRS 契约、HTTP 请求与分页、workspace/cluster/租户/项目隔离、未知值保留、时长换算、报表 SQL 分页、活动告警计数及真实 xlsx；没有无意义重跑全部 3230 后端/711 前端。

健康契约接入后的作业报表/Excel/本轮新增用例另行复验，**78 passed in 10.82s**，结果见 `health-and-report-green.txt`。新增健康测试包含真实形状的合成 inspection_monitoring 元信息，新鲜正常/缺采/过期/保留异常均独立验证。

前端定向 **64 passed**（原作业报表 59 + 本轮 5），见 `frontend-targeted-final.txt`。Ruff 仅检查本分工文件；Mypy 使用 `--config-file backend/pyproject.toml --follow-imports=silent`，四个实现文件通过。

```bash
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' backend/.venv/bin/pytest -o addopts='' -q \
  backend/tests/test_system_review_dataarts_mrs_20261102.py \
  backend/tests/test_dataarts_hcs851_contract.py backend/tests/test_mrs_hcs851_contract.py \
  backend/tests/test_pdf_dataarts_mrs_contract.py backend/tests/test_job_collection_details.py \
  backend/tests/test_job_reports.py backend/tests/test_job_excel.py
npm --prefix frontend test -- --run tests/jobReports.test.ts tests/jobReportsReview20261102.test.ts
```

共享跟进复验：

```bash
INSPECTOR_ENV_FILE='' INSPECTOR_TEST_DATABASE_URL='' backend/.venv/bin/pytest -o addopts='' -q \
  backend/tests/test_job_duration_pipeline_review_20261102.py
```

Vue 类型检查早先通过；共享 Pod 改动曾在 `PodEventsView.vue:79`（DefaultRow 缺 ongoing）及 `podSignalReview20261102.test.ts:42`（联合类型缺 recovery_confirmed）阻塞，已报告相应分工。最终复验输出已无这两项，当前仍被以下共享规则页面问题阻塞（未越界修改）：

- `frontend/src/views/rules/RuleListView.vue:30:48`：DefaultRow 不能赋给 Rule。
- `frontend/src/views/rules/RuleListView.vue:63:140`：DefaultRow 不能赋给 Rule。
- `frontend/src/views/rules/RuleListView.vue:68:76`：DefaultRow 不能赋给 Rule。

最新完整输出为 `/tmp/hcs-dataarts-mrs-audit/frontend-typecheck-final.txt`；不得将本轮定向 Vitest 通过写成全前端类型检查通过。

### 本地浏览器定向验证

使用本地 Vue/testing 技能及 Playwright；Browser plugin 的 `browser` 运行时未提供，CLI 沙箱初试又遇 `URL is not defined`，随后复用已安装的本地 Playwright 包运行临时脚本，没有安装依赖或远程浏览器会话。

- Vite 使用 `envDir:false`、清空代理、单独临时缓存；仅监听 `127.0.0.1:5184`。所有 `/api/` 请求均在独立浏览器 context 内返回合成数据，禁止外域请求及非 GET 请求，无正式后端连接。
- 路径：MRS/批/实时作业页面 → 点击各行作业名 → 详情抽屉；核对数值时间保留、暂停标签、提交时间/调度方式、未知 SUCCEEDED 中性状态及只读用户无告警/台账操作按钮。
- 桌面 1440×1080；移动端 390×844。页面标题/正文正确、无空白/框架覆盖层，pageerror/console error 为 0，移动端整页横向溢出 false。截图禁用动画后复核，避免把抽屉过渡帧误判为裁切。
- 证据：`browser-proof.json`、`browser-qa.txt`、`browser-mrs-desktop.png`、`browser-batch-desktop.png`、`browser-realtime-desktop.png`、`browser-realtime-mobile.png`。临时脚本及截图位于 `/tmp/hcs-dataarts-mrs-audit/`，未写入公共测试/构建配置。
- 该证据只覆盖前端合成数据显示，不能证明正式 API/局点枚举、时间单位、真实权限或通知可用性。

## 7. 仍需跟进

1. 主代理处理第 4 节 7 个共享层失败回归；本分工不修改 shared pipeline/rules。
2. 对 MRS V2 作业接口取得与实际局点匹配、可追溯的补充 API 契约，确认 URI、权限、分页、job_id、状态/结果、过滤与所有时间单位；确认前维持兼容/证据不足标识，不探测其他 API 回退。
3. 确认 DataArts P90 历史基线、子作业分类与超过 24 小时运行任务的业务覆盖要求；不制造正常、零或默认基线。
4. 主代理/相应分工修复并复验共享 RuleListView 类型检查阻塞；历史只含 status_raw 的兼容数据是否要重采或标记，统一决定。
5. 本轮未连接正式局点；正式只读联调仍需由已授权的后续流程按对应 PDF/补充局点契约验证，不使用 Mock 证明正式接口。
