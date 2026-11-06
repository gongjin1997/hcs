<template>
  <div class="report-page">
    <PageHeader title="报表中心" :refresh="refresh" :refreshing="listLoading || detailLoading" />

    <div class="layout">
      <!-- 左：报表列表 -->
      <aside class="hi-card list">
        <div class="table-toolbar table-heading">
          <b>报表列表</b>
          <div class="table-actions">
            <el-button v-perm="'report:manage'" type="primary" :icon="Plus" @click="openGen">生成报表</el-button>
          </div>
        </div>
        <el-segmented v-model="kind" :options="KIND_OPTIONS" block size="small" @change="reloadList" />
        <el-select v-if="tenantFilterOptions.length > 2" v-model="tenantFilter" size="small" class="tf" @change="reloadList">
          <template #prefix><el-icon><OfficeBuilding /></el-icon></template>
          <el-option v-for="o in tenantFilterOptions" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <div v-loading="listLoading" class="items">
          <div v-for="r in reports" :key="r.id" class="item" :class="{ active: r.id === current?.id }" @click="open(r.id)">
            <div class="i1"><i class="k" :style="{ background: REPORT_KIND[r.kind].color }">{{ REPORT_KIND[r.kind].label.slice(0, 1) }}</i>
              <b class="ellipsis">{{ periodName(r) }}</b>
              <TenantTag v-if="r.tenant_id" :id="r.tenant_id" :name="r.tenant_name" />
              <span v-else-if="showTenant" class="merged">合并</span>
              <el-tooltip v-if="r.send_error" :content="r.send_error"><el-icon class="send fail"><WarningFilled /></el-icon></el-tooltip>
              <el-tooltip v-else-if="r.sent_at" :content="`已发送 ${r.sent_to.join('、')}`"><el-icon class="send"><Promotion /></el-icon></el-tooltip>
            </div>
            <div class="i2 text-3"><span>{{ itemBrief(r) }}</span><span>{{ r.created_by === 'system' ? '自动' : r.created_by }}</span></div>
          </div>
          <el-empty v-if="!reports.length && !listLoading" description="暂无报表" :image-size="60" />
        </div>
        <div v-if="reports.length < listTotal" class="more"><el-button link type="primary" @click="loadMore">加载更多（{{ reports.length }}/{{ listTotal }}）</el-button></div>
      </aside>

      <!-- 右：报表详情 -->
      <section v-loading="detailLoading" class="detail">
        <template v-if="current">
          <div class="hi-card head table-heading">
            <div class="h-left">
              <div class="t"><el-tag :color="REPORT_KIND[current.kind].color" effect="dark" class="kind-tag" size="small">{{ REPORT_KIND[current.kind].label }}</el-tag>
                <h3>{{ current.title }}</h3>
                <TenantTag v-if="current.tenant_id" :id="current.tenant_id" :name="current.tenant_name" />
                <el-tag v-else-if="showTenant" size="small" type="info" effect="plain">全部租户合并</el-tag></div>
              <div class="text-3">{{ current.kind === 'LEDGER' ? '台账快照' : `统计周期 ${d.period.label}` }} · 生成于 {{ fmtTime(current.created_at, 'YYYY-MM-DD HH:mm') }}（{{ current.created_by === 'system' ? '定时任务' : current.created_by }}）
                <template v-if="current.sent_at"> · 已发送 {{ current.sent_to.join('、') }}（{{ fmtTime(current.sent_at, 'MM-DD HH:mm') }}）</template>
                <span v-if="current.send_error" class="text-danger"> · 发送失败：{{ current.send_error }}</span>
              </div>
            </div>
            <div class="h-right table-actions">
              <el-button v-perm="'report:manage'" :icon="RefreshRight" :loading="regenLoading" @click="regenerate">重新生成</el-button>
              <el-button :icon="Download" @click="onExport">导出 Excel</el-button>
              <el-button v-perm="'report:manage'" type="primary" :icon="Promotion" @click="openSend">发送邮件</el-button>
              <el-button v-perm="'report:delete'" text type="danger" :icon="Delete" :loading="deleting" @click="onDelete" />
            </div>
          </div>

          <!-- 结论 -->
          <div class="hi-card conclusion">
            <div class="hi-card__title">巡检摘要</div>
            <ol><li v-for="(c, i) in d.conclusion" :key="i">{{ c }}</li></ol>
          </div>

          <template v-if="ov">
            <!-- 核心指标 -->
            <div class="kpis">
              <div v-for="k in kpis" :key="k.label" class="kpi">
                <span class="k">{{ k.label }}</span>
                <b class="num" :style="{ color: k.color }">{{ k.value ?? '-' }}<small v-if="k.value != null">{{ k.unit }}</small></b>
                <span v-if="k.cmp !== undefined" class="cmp" :class="cmpClass(k.cmp, k.better)">
                  <template v-if="k.cmp === null">上期无数据</template>
                  <template v-else>较上期 {{ k.cmp > 0 ? '↑' : k.cmp < 0 ? '↓' : '' }}{{ Math.abs(k.cmp) }}%</template>
                </span>
                <span v-else class="cmp text-3">{{ k.sub }}</span>
              </div>
            </div>

            <el-row :gutter="16">
              <el-col :xs="24" :lg="16">
                <div class="hi-card fill">
                  <div class="hi-card__title">新增告警趋势</div>
                  <EChart :option="trendOption" height="260px" />
                </div>
              </el-col>
              <el-col :xs="24" :lg="8">
                <div class="hi-card fill">
                  <div class="hi-card__title">按服务分布</div>
                  <div v-for="s in d.by_service ?? []" :key="s.service" class="svc">
                    <div class="s1"><b>{{ s.name }}</b><span class="num">{{ s.count }} 条</span></div>
                    <div class="bar"><i v-for="lv in SEVS" :key="lv" :style="{ width: pct(s[lv], maxSvc), background: SEVERITY[lv.toUpperCase()].color }" /></div>
                    <div class="text-3">P1 {{ s.p1 }} · P2 {{ s.p2 }} · P3 {{ s.p3 }} · P4 {{ s.p4 }}</div>
                  </div>
                  <el-empty v-if="!d.by_service?.length" description="本期无告警" :image-size="56" />
                </div>
              </el-col>
            </el-row>

            <!-- 运行态：ECS 状态 / 容器事件 / 各租户 -->
            <el-row v-if="d.ecs || d.pods" :gutter="16" class="mt-16">
              <el-col v-if="d.ecs" :xs="24" :lg="Math.max(12, rtSpan)" :xl="rtSpan">
                <div class="hi-card fill">
                  <div class="hi-card__title">ECS 运行状态<span class="sub">生成时状态</span>
                    <el-link type="primary" underline="never" @click="$router.push({ path: '/ledger', query: { type: 'ecs_server' } })">台账</el-link></div>
                  <div class="rt-nums">
                    <div><span>云服务器</span><b class="num">{{ d.ecs.total }}</b></div>
                    <div><span>运行中</span><b class="num text-ok">{{ d.ecs.active }}</b></div>
                    <div><span>已关机</span><b class="num" :class="{ 'text-warn': d.ecs.shutoff }">{{ d.ecs.shutoff }}</b></div>
                    <div><span>故障</span><b class="num" :class="{ 'text-danger': d.ecs.error }">{{ d.ecs.error }}</b></div>
                    <div><span>状态变更</span><b class="num">{{ d.ecs.status_changes }}</b></div>
                    <div><span>重启</span><b class="num" :class="{ 'text-warn': d.ecs.reboots }">{{ d.ecs.reboots }}</b></div>
                  </div>
                  <div class="tl-title text-3">重启次数 Top</div>
                  <div v-for="(x, i) in d.ecs.top_reboot" :key="x.name" class="tl"><i>{{ i + 1 }}</i><span class="ellipsis">{{ x.name }}</span><b class="num">{{ x.count }} 次</b></div>
                  <div v-if="!d.ecs.top_reboot.length" class="tl-none text-3">本期无 ECS 重启</div>
                </div>
              </el-col>
              <el-col v-if="d.pods" :xs="24" :lg="Math.max(12, rtSpan)" :xl="rtSpan">
                <div class="hi-card fill">
                  <div class="hi-card__title">CCE 容器事件<span class="sub">本期 {{ d.pods.events }} 起</span>
                    <el-link type="primary" underline="never" @click="$router.push('/pod-events')">详情</el-link></div>
                  <div class="rt-nums">
                    <div><span>容器重启</span><b class="num" :class="{ 'text-warn': d.pods.restarts }">{{ d.pods.restarts ?? '—' }}</b></div>
                    <div><span>OOMKilled</span><b class="num" :class="{ 'text-danger': d.pods.oom }">{{ d.pods.oom ?? '—' }}</b></div>
                    <div><span>CrashLoop</span><b class="num" :class="{ 'text-danger': d.pods.crashloop }">{{ d.pods.crashloop }}</b></div>
                    <div><span>背压</span><b class="num" :class="{ 'text-warn': d.pods.backpressure }">{{ d.pods.backpressure }}</b></div>
                    <div><span>背压时长</span><b class="num">{{ d.pods.backpressure_min }}<small>分钟</small></b></div>
                    <div><span>驱逐</span><b class="num" :class="{ 'text-warn': d.pods.evicted }">{{ d.pods.evicted ?? '—' }}</b></div>
                  </div>
                  <div class="tl-title text-3">重启最多的工作负载</div>
                  <div v-for="(x, i) in d.pods.top_restart.slice(0, 5)" :key="`${x.tenant_id}:${x.project_ref}:${x.cluster_id}:${x.workload_kind}:${x.workload}`" class="tl link" @click="goWorkload(x)">
                    <i>{{ i + 1 }}</i><span class="ellipsis">{{ wl(x.workload).name }}<em class="text-3">{{ wl(x.workload).ns }} · {{ x.workload_kind || '工作负载' }}</em></span><b class="num">{{ x.count ?? '—' }} 次</b></div>
                  <div v-if="!d.pods.top_restart.length" class="tl-none text-3">本期无容器重启</div>
                </div>
              </el-col>
              <el-col v-if="d.by_tenant?.length" :xs="24" :lg="Math.max(12, rtSpan)" :xl="rtSpan">
                <div class="hi-card fill">
                  <div class="hi-card__title">各租户概览<span class="sub">本期告警 · 当前健康分</span></div>
                  <div v-for="t in d.by_tenant" :key="t.tenant_id" class="bt">
                    <div class="bt-l"><TenantTag :id="t.tenant_id" :name="t.name" />
                      <span class="text-3">新增告警 <b class="num">{{ t.new_alarms }}</b> · P1 <b class="num" :style="{ color: t.p1 ? SEVERITY.P1.color : undefined }">{{ t.p1 }}</b>
                        · P2 <b class="num" :style="{ color: t.p2 ? SEVERITY.P2.color : undefined }">{{ t.p2 }}</b></span></div>
                    <b class="num bt-s" :style="{ color: t.health_score == null ? undefined : scoreColor(t.health_score) }">{{ t.health_score ?? '-' }}<small>分</small></b>
                  </div>
                </div>
              </el-col>
            </el-row>

            <el-row :gutter="16" class="mt-16">
              <el-col :xs="24" :lg="12">
                <div class="hi-card fill">
                  <div class="hi-card__title">告警最多的资源 Top {{ d.top_resources?.length }}</div>
                  <el-table :data="d.top_resources" size="small">
                    <el-table-column type="index" label="#" width="44" />
                    <el-table-column label="资源" min-width="160"><template #default="{ row }"><b>{{ row.name }}</b><div class="text-3">{{ row.type_name }}</div></template></el-table-column>
                    <el-table-column label="最高级别" width="96"><template #default="{ row }"><SeverityTag :level="row.max_severity" short /></template></el-table-column>
                    <el-table-column label="告警数" width="80" align="right"><template #default="{ row }"><b class="num">{{ row.count }}</b></template></el-table-column>
                  </el-table>
                </div>
              </el-col>
              <el-col :xs="24" :lg="12">
                <div class="hi-card fill">
                  <div class="hi-card__title">命中最多的规则 Top {{ d.top_rules?.length }}</div>
                  <el-table :data="d.top_rules" size="small">
                    <el-table-column type="index" label="#" width="44" />
                    <el-table-column label="规则" min-width="180"><template #default="{ row }">{{ row.name }}<div class="text-3 mono">{{ row.rule_id }}</div></template></el-table-column>
                    <el-table-column label="命中" width="80" align="right"><template #default="{ row }"><b class="num">{{ row.count }}</b></template></el-table-column>
                  </el-table>
                </div>
              </el-col>
            </el-row>

            <div class="hi-card mt-16">
              <div class="hi-card__title">重点告警（P1 / P2）<span class="sub">共 {{ d.key_alarms?.length ?? 0 }} 条</span></div>
              <el-table :data="d.key_alarms" size="small">
                <el-table-column label="级别" width="96"><template #default="{ row }"><SeverityTag :level="row.severity" /></template></el-table-column>
                <el-table-column label="告警" prop="title" min-width="220" show-overflow-tooltip />
                <el-table-column label="资源" min-width="150"><template #default="{ row }">{{ row.resource_name }}<div class="text-3">{{ row.type_name }}</div></template></el-table-column>
                <el-table-column label="发生时间" width="120"><template #default="{ row }">{{ fmtTime(row.first_seen, 'MM-DD HH:mm') }}</template></el-table-column>
                <el-table-column label="持续" width="96"><template #default="{ row }">{{ row.duration_min != null ? humanize(row.duration_min * 60) : '-' }}</template></el-table-column>
                <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
                <el-table-column label="处理人" width="90"><template #default="{ row }">{{ row.ack_by ?? '-' }}</template></el-table-column>
                <template #empty><el-empty description="本期无 P1 / P2 告警" :image-size="56" /></template>
              </el-table>
            </div>

            <el-row :gutter="16" class="mt-16">
              <el-col :xs="24" :lg="14">
                <div class="hi-card fill">
                  <div class="hi-card__title">巡检任务执行<span class="sub">已完成任务成功率 {{ ov.run_success_rate ?? '-' }}% · {{ ov.inspection_runs ?? 0 }} 条记录</span></div>
                  <el-table :data="d.tasks" size="small">
                    <el-table-column label="任务" prop="name" min-width="150" show-overflow-tooltip />
                    <el-table-column label="记录" width="56" align="right" prop="runs" />
                    <el-table-column label="成功 / 部分 / 失败" width="116" align="center">
                      <template #default="{ row }"><span class="text-ok">{{ row.success }}</span> / <span class="text-warn">{{ row.partial }}</span> / <span :class="{ 'text-danger': row.failed }">{{ row.failed }}</span></template>
                    </el-table-column>
                    <el-table-column label="跳过 / 运行中" width="100" align="center"><template #default="{ row }">{{ row.skipped ?? 0 }} / {{ row.running ?? 0 }}</template></el-table-column>
                    <el-table-column label="成功率" width="116"><template #default="{ row }"><span v-if="row.success_rate == null">—</span><el-progress v-else :percentage="row.success_rate" :stroke-width="6" :color="row.success_rate >= 95 ? '#2e7d32' : '#f57c00'" /></template></el-table-column>
                    <el-table-column label="平均耗时" width="78" align="right"><template #default="{ row }">{{ row.avg_sec ?? '-' }} s</template></el-table-column>
                  </el-table>
                </div>
              </el-col>
              <el-col :xs="24" :lg="10">
                <div class="hi-card fill">
                  <div class="hi-card__title">生成时容量水位<span class="sub">仅列有效采样</span></div>
                  <div v-for="c in d.capacity ?? []" :key="c.name + c.metric" class="cap">
                    <span class="n ellipsis">{{ c.name }}<em class="text-3">{{ c.metric }}</em></span>
                    <el-progress :percentage="c.value" :color="usageColor(c.value)" :stroke-width="8" :show-text="false" />
                    <b class="num" :style="{ color: usageColor(c.value) }">{{ c.value }}%</b>
                  </div>
                </div>
              </el-col>
            </el-row>
          </template>

          <!-- 资源台账 -->
          <div class="hi-card mt-16">
            <div class="hi-card__title">资源台账<span class="sub">{{ current.kind === 'LEDGER' ? '生成时快照 · 近 30 天变更' : '生成时快照 · 本期同步发现的变更' }}</span>
              <el-link type="primary" underline="never" @click="$router.push('/ledger')">查看台账</el-link></div>
            <div class="ledger-sum">
              <div><span>资源总数</span><b class="num">{{ d.ledger.total }}</b></div>
              <div><span>已知 vCPU / 内存</span><b class="num">{{ d.ledger.vcpus ?? '—' }} / {{ d.ledger.memory_gb ?? '—' }}<small>GB</small></b></div>
              <div><span>已知块存储</span><b class="num">{{ d.ledger.storage_gb == null ? '—' : (d.ledger.storage_gb / 1024).toFixed(1) }}<small>TB</small></b></div>
              <div><span>未分配负责人</span><b class="num text-warn">{{ d.ledger.unassigned }}</b></div>
              <div><span>疑似闲置</span><b class="num text-warn">{{ d.ledger.idle }}</b></div>
              <div><span>新增 / 变更 / 释放</span><b class="num"><em class="text-ok">{{ d.ledger.changes_count.CREATED ?? 0 }}</em> / <em>{{ d.ledger.changes_count.UPDATED ?? 0 }}</em> / <em class="text-danger">{{ d.ledger.changes_count.DELETED ?? 0 }}</em></b></div>
            </div>
            <el-row :gutter="16">
              <el-col :xs="24" :lg="current.kind === 'LEDGER' ? 10 : 9">
                <el-table :data="d.ledger.by_type" size="small" :max-height="current.kind === 'LEDGER' ? 560 : 360">
                  <el-table-column label="资源类型" prop="name" min-width="130" />
                  <el-table-column label="数量" width="70" align="right"><template #default="{ row }"><b class="num">{{ row.count }}</b></template></el-table-column>
                  <el-table-column label="未分配" width="70" align="right"><template #default="{ row }"><span :class="{ 'text-warn': row.unassigned }">{{ row.unassigned }}</span></template></el-table-column>
                </el-table>
              </el-col>
              <el-col :xs="24" :lg="current.kind === 'LEDGER' ? 14 : 15">
                <el-table :data="d.ledger.changes" size="small" :max-height="current.kind === 'LEDGER' ? 560 : 360">
                  <el-table-column label="时间" width="110"><template #default="{ row }">{{ fmtTime(row.detected_at, 'MM-DD HH:mm') }}</template></el-table-column>
                  <el-table-column label="类型" width="70"><template #default="{ row }"><el-tag :type="CHANGE_TYPE[row.change_type]?.type ?? 'info'" size="small">{{ CHANGE_TYPE[row.change_type]?.label ?? row.change_type }}</el-tag></template></el-table-column>
                  <el-table-column label="资源" min-width="150"><template #default="{ row }">{{ row.name }}<div class="text-3">{{ row.type_name }}</div></template></el-table-column>
                  <el-table-column label="变更内容" min-width="200" show-overflow-tooltip><template #default="{ row }">{{ row.summary || CHANGE_DEFAULT[row.change_type] || '-' }}</template></el-table-column>
                  <template #empty><el-empty description="本期无台账变更" :image-size="56" /></template>
                </el-table>
              </el-col>
            </el-row>
          </div>
        </template>
        <div v-else-if="!detailLoading" class="hi-card"><el-empty description="选择左侧报表查看，或点击右上角“生成报表”" /></div>
      </section>
    </div>

    <!-- 生成报表 -->
    <el-dialog v-model="gen.visible" title="生成报表" width="440px">
      <el-form label-width="72px">
        <el-form-item label="类型">
          <el-radio-group v-model="gen.kind"><el-radio-button v-for="(v, k) in REPORT_KIND" :key="k" :value="k">{{ v.label }}</el-radio-button></el-radio-group>
        </el-form-item>
        <el-form-item v-if="genTenantOptions.length > 1" label="租户">
          <el-select v-model="gen.tenant" style="width: 100%">
            <el-option v-for="o in genTenantOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="gen.kind !== 'LEDGER'" label="周期">
          <el-date-picker v-model="gen.date" :type="pickerType" :format="pickerFormat" value-format="YYYY-MM-DD" :disabled-date="(t: Date) => t > new Date()"
                          placeholder="默认上一个完整周期" clearable style="width: 100%" />
        </el-form-item>
        <p class="text-3 gen-tip">{{ gen.kind === 'LEDGER' ? '生成当前资源台账快照（含近 30 天变更）。' : `不选择时生成上一个完整周期（${REPORT_KIND[gen.kind].desc}）；同租户、同类型、同周期再次生成会覆盖。` }}
          <template v-if="genTenantOptions.length > 1"><br>合并报表汇总全部租户并附“各租户概览”；租户报表只统计该租户的数据。</template></p>
      </el-form>
      <template #footer>
        <el-button @click="gen.visible = false">取消</el-button>
        <el-button type="primary" :loading="gen.loading" @click="doGenerate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 发送邮件 -->
    <el-dialog v-model="send.visible" :title="`发送报表：${send.reportTitle}`" :close-on-click-modal="!send.loading" :close-on-press-escape="!send.loading" :show-close="!send.loading" width="480px">
      <el-form label-width="72px" :disabled="send.loading">
        <el-form-item label="收件人">
          <RecipientSelect v-model="send.recipient_user_ids" :tenant-id="send.reportTenantId" :global-only="send.reportTenantId == null" :active="send.visible" :disabled="send.loading" />
        </el-form-item>
        <p class="text-3 gen-tip">邮件包含摘要和 Excel 附件。默认收件人取系统或租户配置，可在发送前修改。</p>
      </el-form>
      <template #footer>
        <el-button :disabled="send.loading" @click="send.visible = false">取消</el-button>
        <el-button type="primary" :loading="send.loading" :disabled="!send.recipient_user_ids.length" @click="doSend">发送</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Delete, Download, OfficeBuilding, Plus, Promotion, RefreshRight, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onScopeDispose, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createReport, deleteReport, exportReport, getReport, listReports, sendReport } from '@/api'
import RecipientSelect from '@/components/RecipientSelect.vue'
import EChart from '@/components/EChart.vue'
import PageHeader from '@/components/PageHeader.vue'
import { reportPeriodName, reportRegenerationDate } from './report-title'
import { useLatestRequest } from '@/composables/useLatestRequest'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { ReportBrief, ReportData, ReportDetail, ReportKind } from '@/types'
import { CHANGE_TYPE, REPORT_KIND, SEVERITIES, SEVERITY, scoreColor, usageColor } from '@/utils/dict'
import { fmtTime, humanize } from '@/utils/format'

const SEVS = ['p1', 'p2', 'p3', 'p4'] as const
const KIND_OPTIONS = [{ label: '全部', value: '' }, ...Object.entries(REPORT_KIND).map(([value, v]) => ({ label: v.label.replace('报表', ''), value }))]
const route = useRoute()
const router = useRouter()
const user = useUserStore()
const app = useAppStore()
let disposed = false
onScopeDispose(() => { disposed = true })
/** 用户拥有全部租户权限时才能看到 / 生成“全部租户合并”报表 */
const fullScope = computed(() => user.user?.tenant_ids == null)
const showTenant = computed(() => app.tenants.length > 1)

// ---- 列表 ----
const kind = ref<string>(typeof route.query.kind === 'string' ? route.query.kind : '')
const tenantFilter = ref<string>('all')
const tenantFilterOptions = computed(() => {
  const ts = app.currentTenant ? [app.currentTenant] : app.tenants
  return [{ label: '全部报表', value: 'all' }, ...(fullScope.value ? [{ label: '全部租户合并', value: 'global' }] : []),
    ...ts.map((t) => ({ label: t.name, value: String(t.id) }))]
})
const reports = ref<ReportBrief[]>([])
const listTotal = ref(0)
const listPage = ref(1)
const listRequest = useLatestRequest()
const listLoading = listRequest.loading
async function fetchList(append = false) {
  const query = { kind: (kind.value || undefined) as ReportKind | undefined, page: listPage.value, page_size: 15,
    tenant_id: tenantFilter.value === 'all' ? undefined : tenantFilter.value }
  return listRequest.run(() => listReports(query), res => {
    reports.value = append ? [...reports.value, ...res.items] : res.items
    listTotal.value = res.total
  })
}
async function reloadList() {
  listPage.value = 1
  detailRequest.invalidate()
  current.value = null
  const { id: _id, ...query } = route.query
  await router.replace({ query })
  if (await fetchList() && reports.value[0]) await open(reports.value[0].id)
}
async function loadMore() {
  if (listLoading.value) return
  const previous = listPage.value
  listPage.value++
  try { await fetchList(true) } catch (error) {
    if (listPage.value === previous + 1) listPage.value = previous
    throw error
  }
}
const periodName = (r: ReportBrief) => reportPeriodName(r.title, REPORT_KIND[r.kind].label)
const itemBrief = (r: ReportBrief) => (r.kind === 'LEDGER'
  ? `资源 ${r.summary.ledger_total ?? '-'} · 未分配 ${r.summary.unassigned ?? '-'}`
  : `告警 ${r.summary.new_alarms ?? 0} · P1 ${r.summary.p1 ?? 0} · 成功率 ${r.summary.run_success_rate ?? '-'}%`)

// ---- 详情 ----
const current = ref<ReportDetail | null>(null)
const detailRequest = useLatestRequest()
const detailLoading = detailRequest.loading
const d = computed(() => current.value!.data as ReportData)
// 台账报表只有资源快照，不展示告警 / 任务 / 容量等巡检指标
const CHANGE_DEFAULT: Record<string, string> = { CREATED: '新资源纳入台账', DELETED: '资源已在 HCS 释放' }
const ov = computed(() => (current.value && current.value.kind !== 'LEDGER' ? current.value.data.overview : undefined))
async function open(id: number) {
  current.value = null
  return detailRequest.run(() => getReport(id), report => {
    current.value = report
    void router.replace({ query: { ...route.query, id: String(id) } })
  })
}
async function refresh() {
  listPage.value = 1
  if (!await fetchList()) return
  const id = current.value?.id ?? reports.value[0]?.id
  if (id) await open(id)
  else { detailRequest.invalidate(); current.value = null }
}
const kpis = computed(() => {
  const o = ov.value!
  const c = current.value?.data.compare ?? null
  const cmp = (k: string) => (c ? c[k] ?? null : null)
  return [
    { label: '新增告警', value: o.new_alarms, unit: '条', cmp: cmp('new_alarms'), better: 'down' },
    { label: 'P1 紧急', value: o.p1, unit: '条', cmp: cmp('p1'), better: 'down', color: o.p1 ? SEVERITY.P1.color : undefined },
    { label: 'P2 重要', value: o.p2, unit: '条', cmp: cmp('p2'), better: 'down', color: o.p2 ? SEVERITY.P2.color : undefined },
    { label: '平均认领 MTTA', value: o.mtta_min, unit: '分钟', cmp: cmp('mtta_min'), better: 'down' },
    { label: '平均恢复 MTTR', value: o.mttr_min, unit: '分钟', cmp: cmp('mttr_min'), better: 'down' },
    { label: 'P1/P2 认领率', value: o.ack_rate, unit: '%', sub: `已恢复 ${o.resolved} 条` },
    { label: '巡检成功率', value: o.run_success_rate, unit: '%', sub: `共执行 ${o.inspection_runs ?? 0} 次` },
    { label: '期末未恢复', value: o.active_at_end, unit: '条', sub: `当前健康分 ${o.health_score_now ?? '-'}`, color: o.active_at_end ? SEVERITY.P2.color : undefined },
  ] as { label: string; value: number | null | undefined; unit: string; cmp?: number | null; better?: string; sub?: string; color?: string }[]
})
const cmpClass = (v: number | null, better?: string) => (v === null || v === 0 ? 'flat' : (v < 0) === (better === 'down') ? 'good' : 'bad')
const maxSvc = computed(() => Math.max(1, ...(d.value.by_service ?? []).map((s) => s.count)))
const pct = (v: number, max: number) => `${(v / max) * 100}%`
const rtSpan = computed(() => ([d.value.ecs, d.value.pods, d.value.by_tenant?.length].filter(Boolean).length === 3 ? 8 : 12))
/** top_restart 的 workload 为 “集群/命名空间/工作负载” */
const wl = (k: string) => { const p = k.split('/'); return { name: p[p.length - 1], ns: p.slice(0, -1).join(' / ') } }
const goWorkload = (item: string | NonNullable<ReportData['pods']>['top_restart'][number]) => {
  const path = typeof item === 'string' ? item : item.workload
  const p = path.split('/')
  router.push({ path: '/pod-events', query: { namespace: p[p.length - 2], workload: p[p.length - 1],
    ...(typeof item === 'string' ? {} : { tenant_id: item.tenant_id ?? undefined, project_ref: item.project_ref ?? undefined,
      unknown_project: item.project_ref === null ? '1' : undefined,
      cluster_id: item.cluster_id || undefined, workload_kind: item.workload_kind || undefined }) } })
}
const trendOption = computed(() => {
  const t = d.value.trend
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { right: 0, top: 0, icon: 'circle', itemWidth: 8, data: SEVERITIES.map((s) => `${s} ${SEVERITY[s].label}`) },
    grid: { left: 8, right: 8, top: 34, bottom: 4, outerBoundsMode: 'same', outerBoundsContain: 'all' },
    xAxis: { type: 'category', data: t?.labels ?? [], axisTick: { show: false }, axisLine: { lineStyle: { color: '#e5e8ef' } }, axisLabel: { color: '#86909c' } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } }, axisLabel: { color: '#86909c' } },
    series: SEVERITIES.map((s) => ({
      name: `${s} ${SEVERITY[s].label}`, type: 'bar', stack: 'total', barMaxWidth: 18, data: t?.series[s] ?? [],
      itemStyle: { color: SEVERITY[s].color, borderRadius: s === 'P4' ? [3, 3, 0, 0] : 0 },
    })),
  }
})

// ---- 操作 ----
const regenLoading = ref(false)
async function regenerate() {
  if (regenLoading.value || !current.value) return
  const target = current.value
  const tenantId = app.tenantId
  let date: string | undefined
  try { date = reportRegenerationDate(target.kind, target.data.period.label) }
  catch (error) { ElMessage.warning((error as Error).message); return }
  regenLoading.value = true
  try {
    await ElMessageBox.confirm(target.kind === 'LEDGER' ? '按当前台账生成一份新的台账报表？' : '按最新数据重新生成该周期报表（覆盖当前内容）？', '重新生成', { type: 'info' })
    if (disposed || tenantId !== app.tenantId) return
    const res = await createReport(target.kind, date, target.tenant_id)
    if (disposed) return
    ElMessage.success('已重新生成')
    await fetchList()
    await open(res.id)
  } catch { /* 取消不执行，接口错误由请求层提示。 */ }
  finally { regenLoading.value = false }
}
async function onExport() {
  const name = await exportReport(current.value!.id)
  ElMessage.success(`已导出：${name}`)
}
const deleting = ref(false)
async function onDelete() {
  if (deleting.value || !current.value) return
  const target = current.value
  const tenantId = app.tenantId
  deleting.value = true
  try {
    await ElMessageBox.confirm(`删除「${target.title}」？`, '删除报表', { type: 'warning' })
    if (disposed || tenantId !== app.tenantId) return
    await deleteReport(target.id)
    if (disposed) return
    ElMessage.success('已删除')
    if (current.value?.id === target.id) { current.value = null; await reloadList() }
    else await fetchList()
  } catch { /* 取消不执行；接口错误由请求层提示。 */ }
  finally { deleting.value = false }
}

/** gen.tenant：0 = 全部租户合并报表 */
const gen = reactive({ visible: false, loading: false, kind: 'DAILY' as ReportKind, date: '' as string | null, tenant: 0 })
const genTenantOptions = computed(() => {
  const ts = app.currentTenant ? [app.currentTenant] : app.tenants
  return [...(fullScope.value ? [{ label: '全部租户（合并报表）', value: 0 }] : []), ...ts.map((t) => ({ label: `${t.name}（租户报表）`, value: t.id }))]
})
function openGen() {
  gen.tenant = app.currentTenant?.id ?? (fullScope.value ? 0 : app.tenants[0]?.id ?? 0)
  gen.visible = true
}
const pickerType = computed(() => (gen.kind === 'WEEKLY' ? 'week' : gen.kind === 'MONTHLY' ? 'month' : 'date'))
const pickerFormat = computed(() => (gen.kind === 'WEEKLY' ? 'YYYY 第 ww 周' : gen.kind === 'MONTHLY' ? 'YYYY-MM' : 'YYYY-MM-DD'))
async function doGenerate() {
  if (gen.loading) return
  gen.loading = true
  try {
    const res = await createReport(gen.kind, gen.kind === 'LEDGER' ? undefined : gen.date || undefined, gen.tenant || null)
    ElMessage.success(`已生成：${res.title}`)
    gen.visible = false
    kind.value = ''
    await fetchList()
    await open(res.id)
  } finally { gen.loading = false }
}

const send = reactive({ visible: false, loading: false, reportId: null as number | null, reportTitle: '', tenantId: null as number | null, reportTenantId: null as number | null, recipient_user_ids: [] as number[] })
async function openSend() {
  if (send.loading || !current.value) return
  const c = current.value
  send.reportId = c.id
  send.reportTitle = c.title
  send.tenantId = app.tenantId
  send.reportTenantId = c.tenant_id
  send.recipient_user_ids = [...(c.default_user_ids ?? [])]
  send.visible = true
}
async function doSend() {
  if (send.loading || !send.reportId || !send.recipient_user_ids.length || disposed || send.tenantId !== app.tenantId || !user.can('report:manage')) return
  const id = send.reportId
  const ids = [...send.recipient_user_ids]
  send.loading = true
  try {
    await sendReport(id, ids)
    if (disposed) return
    ElMessage.success('报表已发送')
    send.visible = false
    await Promise.all([current.value?.id === id ? open(id) : undefined, fetchList()])
  } finally { send.loading = false }
}

onMounted(async () => {
  await fetchList()
  const id = Number(route.query.id) || reports.value[0]?.id
  if (id) open(id)
})
</script>

<style scoped lang="scss">
.layout { display: flex; gap: 16px; align-items: flex-start; }
.list { width: 300px; flex: none; padding: 12px; position: sticky; top: 0;
  .items { margin-top: 10px; min-height: 120px; max-height: calc(100vh - 230px); overflow: auto; }
  .item { padding: 10px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; margin-bottom: 4px;
    &:hover { background: var(--hi-bg-soft); }
    &.active { background: var(--hi-sider-active-bg); border-color: var(--el-color-primary-light-7); b { color: var(--el-color-primary); } }
    .i1 { display: flex; align-items: center; gap: 8px; b { flex: 1; font-size: 13.5px; font-weight: 600; } }
    .k { width: 20px; height: 20px; flex: none; border-radius: 5px; color: #fff; font-size: 11px; font-style: normal; display: grid; place-items: center; font-weight: 700; }
    .send { color: var(--hi-ok); &.fail { color: var(--hi-p1); } }
    .i2 { display: flex; justify-content: space-between; margin: 4px 0 0 28px; } }
  .more { text-align: center; padding-top: 6px; }
  .tf { width: 100%; margin-top: 8px; }
  .merged { flex: none; font-size: 12px; color: var(--hi-text-3); padding: 0 6px; border-radius: 9px; background: var(--hi-bg-soft); line-height: 18px; } }
.detail { flex: 1; min-width: 0; min-height: 400px; }
.head { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px;
  .t { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; h3 { margin: 0; font-size: 18px; } }
  .kind-tag { border: none; }
  .h-right { display: flex; gap: 8px; flex: none; } }
.conclusion { margin-bottom: 16px; background: linear-gradient(180deg, #f5f8ff, #fff); border-color: var(--el-color-primary-light-8);
  ol { margin: 0; padding-left: 20px; line-height: 1.95; color: var(--hi-text-1); font-size: 13.5px; } }
.kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px;
  .kpi { display: flex; flex-direction: column; gap: 3px; padding: 12px 14px; background: #fff; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); min-width: 0;
    .k { font-size: 12px; color: var(--hi-text-2); white-space: nowrap; }
    b { font-size: 22px; line-height: 1.2; small { font-size: 11px; font-weight: 500; color: var(--hi-text-3); margin-left: 3px; } }
    .cmp { font-size: 12px; white-space: nowrap; &.good { color: var(--hi-ok); } &.bad { color: var(--hi-p1); } &.flat { color: var(--hi-text-3); } } } }
.fill { height: 100%; }
:deep(.el-row) > .el-col > .fill { height: 100%; }
.svc { padding: 8px 0; border-bottom: 1px dashed var(--hi-border-light); &:last-child { border-bottom: none; }
  .s1 { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 13px; }
  .bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--hi-bg-soft); margin-bottom: 4px; i { display: block; height: 100%; } } }
.cap { display: grid; grid-template-columns: 170px 1fr 52px; align-items: center; gap: 10px; padding: 6px 0; font-size: 13px;
  .n em { font-style: normal; margin-left: 6px; } b { text-align: right; } }
.ledger-sum { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 14px;
  > div { padding: 10px 12px; border-radius: 8px; background: var(--hi-bg-soft); display: flex; flex-direction: column; gap: 2px;
    span { font-size: 12px; color: var(--hi-text-3); } b { font-size: 18px; small { font-size: 11px; margin-left: 2px; color: var(--hi-text-3); } em { font-style: normal; } } } }
.gen-tip { margin: -4px 0 0 72px; line-height: 1.7; }
.rt-nums { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px;
  > div { padding: 8px 10px; border-radius: 8px; background: var(--hi-bg-soft); display: flex; flex-direction: column; gap: 2px; min-width: 0;
    span { font-size: 12px; color: var(--hi-text-3); white-space: nowrap; } b { font-size: 18px; small { font-size: 11px; margin-left: 2px; color: var(--hi-text-3); font-weight: 500; } } } }
.tl-title { font-size: 12px; margin-bottom: 4px; }
.tl { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 13px; border-bottom: 1px dashed var(--hi-border-light);
  &:last-child { border-bottom: none; }
  &.link { cursor: pointer; &:hover span { color: var(--el-color-primary); } }
  i { width: 18px; height: 18px; flex: none; border-radius: 4px; background: var(--hi-bg-soft); font-style: normal; font-size: 11px; display: grid; place-items: center; color: var(--hi-text-2); }
  span { flex: 1; min-width: 0; em { font-style: normal; margin-left: 6px; font-size: 12px; } }
  b { flex: none; } }
.tl-none { padding: 8px 0; font-size: 12.5px; }
.bt { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 0; border-bottom: 1px dashed var(--hi-border-light);
  &:last-child { border-bottom: none; }
  .bt-l { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; min-width: 0; font-size: 12.5px; b { color: var(--hi-text-1); } }
  .bt-s { font-size: 24px; flex: none; small { font-size: 11px; margin-left: 2px; color: var(--hi-text-3); font-weight: 500; } } }
@media (max-width: 767px) {
  .layout { flex-direction: column; }
  .list, .detail { width: 100%; box-sizing: border-box; }
  .list { position: static; }
  .list .items { max-height: 240px; }
  .head .h-left { min-width: 0; max-width: 100%; }
  .head .t { flex-wrap: wrap; }
  .head h3 { overflow-wrap: anywhere; }
  .head .h-right { flex: 1 1 100%; flex-wrap: wrap; justify-content: flex-start; }
  .kpis, .ledger-sum { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .cap { grid-template-columns: minmax(0, 1fr) 72px 46px; }
  .cap .n { overflow-wrap: anywhere; }
}
@media (max-width: 1199px) {
  :deep(.el-row) { row-gap: 16px; }
}
</style>
