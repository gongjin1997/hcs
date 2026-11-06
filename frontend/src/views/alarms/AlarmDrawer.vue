<template>
  <el-drawer :model-value="modelValue !== null" size="min(780px, 100vw)" :with-header="false" class="alarm-drawer" @close="close">
    <div v-loading="loading" class="wrap">
      <template v-if="a">
        <!-- 头部：级别色带 + 标题 + 主操作 -->
        <div class="head" :style="{ '--c': SEVERITY[a.severity].color, '--bg': SEVERITY[a.severity].bg }">
          <div class="row1">
            <SeverityTag :level="a.severity" solid />
            <StatusTag :status="a.status" />
            <TenantTag v-if="a.tenant_id" :id="a.tenant_id" />
            <span class="mono text-3">#{{ a.id }} · {{ a.rule_id }}</span>
            <el-button class="x" text circle :icon="Close" @click="close" />
          </div>
          <h3>{{ a.title }}</h3>
          <div class="meta">
            <span>资源 <b>{{ a.resource_name }}</b><template v-if="a.sub_object"> / {{ a.sub_object }}</template></span>
            <router-link v-if="monitoringLink" :to="monitoringLink">查看监控</router-link>
            <span v-if="monitoringLink" class="text-3">当前查询，非告警时刻快照</span>
            <span>{{ SERVICES[a.service] }} · {{ RESOURCE_TYPES[a.resource_type] }}</span>
            <span>持续 <b>{{ durationOf(a.first_seen, a.resolved_at) }}</b></span>
            <span>累计触发 <b class="num">{{ a.count }}</b> 次</span>
          </div>
          <div class="actions">
            <el-button v-if="a.status === 'FIRING'" type="primary" :icon="Check" :disabled="!canAck" :loading="acting === 'ack'" @click="doAck">认领</el-button>
            <el-button :icon="CircleClose" :disabled="!canAck || ['RESOLVED', 'CLOSED'].includes(a.status)" :loading="acting === 'close'" @click="doClose">关闭</el-button>
            <el-button v-if="a.silence" :icon="MuteNotification" @click="goSilence(a.silence.id)">查看静默</el-button>
            <el-tooltip v-else-if="!canSilence" content="需要“创建 / 取消静默与维护窗口”权限" placement="top">
              <span><el-button :icon="MuteNotification" disabled>静默</el-button></span>
            </el-tooltip>
            <el-button v-else :icon="MuteNotification" :disabled="!active" @click="openQuick">静默</el-button>
            <el-button :icon="Message" @click="openPreview">邮件预览</el-button>
            <el-button :icon="Link" @click="copyLink">复制链接</el-button>
          </div>
        </div>

        <!-- 被静默 / 维护窗口抑制：说明来源，结束后仍未恢复会补发通知 -->
        <el-alert v-if="a.silence" class="silence-alert" type="info" :closable="false" show-icon>
          <template #title>
            已被{{ SILENCE_KIND[a.silence.kind]?.label ?? '静默' }}“{{ a.silence.name }}”抑制
            <el-tag size="small" :type="SILENCE_STATUS[a.silence.status]?.type" effect="plain">{{ SILENCE_STATUS[a.silence.status]?.label }}</el-tag>
          </template>
          <div class="s-body">
            {{ fmtTime(a.silence.starts_at, 'MM-DD HH:mm') }} ~ {{ fmtTime(a.silence.ends_at, 'MM-DD HH:mm') }} · {{ a.silence.created_by }} 创建
            <template v-if="a.silence.reason"> · {{ a.silence.reason }}</template>
            <div class="text-3">窗口内不发通知、不升级；结束后告警仍未恢复会自动补发通知。</div>
          </div>
        </el-alert>

        <!-- 关键数值 -->
        <div class="kv">
          <div><span>当前值</span><b class="num" :style="{ color: active ? SEVERITY[a.severity].color : undefined }">{{ a.current_value ?? '-' }}</b></div>
          <div><span>阈值 / 条件</span><b>{{ a.threshold_text ?? '-' }}</b></div>
          <div><span>首次发生</span><b class="num">{{ fmtTime(a.first_seen) }}</b></div>
          <div><span>{{ a.resolved_at ? '恢复时间' : '最近一次检测' }}</span><b class="num">{{ fmtTime(a.resolved_at || a.last_seen) }}</b></div>
        </div>

        <el-descriptions :column="2" border size="small" class="desc">
          <el-descriptions-item label="资源 ID"><span class="mono">{{ a.detail.resource_id ?? '-' }}</span></el-descriptions-item>
          <el-descriptions-item label="核心资源"><el-tag v-if="a.detail.critical" type="danger" size="small" effect="plain">核心</el-tag><span v-else>否</span></el-descriptions-item>
          <el-descriptions-item label="项目 / 区域">{{ a.detail.project ?? '-' }} / {{ a.detail.region ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="工作空间">{{ a.detail.workspace ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="负责人">{{ a.owner ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="认领">{{ a.ack_by ? `${a.ack_by} · ${fmtTime(a.ack_at)}` : '未认领' }}</el-descriptions-item>
          <el-descriptions-item v-if="a.suppress_reason" label="抑制原因" :span="2">{{ a.suppress_reason }}</el-descriptions-item>
          <el-descriptions-item label="告警指纹" :span="2"><span class="mono">{{ a.fingerprint }}</span></el-descriptions-item>
        </el-descriptions>

        <el-tabs v-model="tab" class="tabs">
          <el-tab-pane label="处理时间线" name="timeline">
            <el-timeline class="timeline">
              <el-timeline-item v-for="l in logs" :key="l.id" :timestamp="fmtTime(l.at, 'YYYY-MM-DD HH:mm:ss')" placement="top"
                                :color="LOG_ACTION[l.action]?.color" :hollow="l.action === 'NOTIFY'">
                <div class="log">
                  <b :style="{ color: LOG_ACTION[l.action]?.color }">{{ LOG_ACTION[l.action]?.label ?? l.action }}</b>
                  <span class="text-3">{{ l.operator === 'system' ? '系统' : l.operator }}</span>
                  <span v-if="l.from_status || l.to_status" class="flow">
                    {{ statusLabel(l.from_status) }} <el-icon><Right /></el-icon> {{ statusLabel(l.to_status) }}
                  </span>
                </div>
                <div v-if="l.note" class="note">{{ l.note }}</div>
              </el-timeline-item>
            </el-timeline>
          </el-tab-pane>
          <el-tab-pane label="现场数据" name="facts">
            <p class="text-3 tip">规则判定时采集到的事实（facts），用于复盘与规则调优；完整原始报文见巡检运行日志。</p>
            <el-table :data="facts" size="small" border>
              <el-table-column label="字段" prop="key" width="220"><template #default="{ row }"><span class="mono">{{ row.key }}</span></template></el-table-column>
              <el-table-column label="值"><template #default="{ row }"><span class="mono">{{ row.value }}</span></template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="处置手册" name="runbook">
            <div v-if="a.runbook" class="runbook">{{ a.runbook }}</div>
            <el-empty v-else description="该规则暂未配置处置手册，可在“规则管理”中补充" :image-size="80" />
          </el-tab-pane>
          <el-tab-pane :label="`通知记录（${a.notifications.length}）`" name="notify">
            <p class="text-3 tip">已发送表示邮件服务器已受理，不代表收件人已收到。失败与跳过原因见记录；每次重试见处理时间线。</p>
            <el-table :data="a.notifications" size="small">
              <el-table-column label="类型" width="90"><template #default="{ row }">{{ NOTIFY_KIND[row.kind] ?? row.kind }}</template></el-table-column>
              <el-table-column label="主题" min-width="260" show-overflow-tooltip prop="subject" />
              <el-table-column label="收件人" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ row.recipients.join('，') }}</template></el-table-column>
              <el-table-column label="投递结果" min-width="200"><template #default="{ row }">
                <el-tag :type="NOTIFY_STATUS[row.status]?.type ?? 'info'" size="small">{{ NOTIFY_STATUS[row.status]?.label ?? row.status }}</el-tag>
                <div v-if="row.error" class="notification-error">{{ row.error }}</div>
                <div v-if="row.retry" class="text-3">失败 {{ row.retry }} 次</div>
                <div v-if="row.next_retry_at" class="text-3">下次重试 {{ fmtTime(row.next_retry_at) }}</div>
              </template></el-table-column>
              <el-table-column label="提交服务器" width="140"><template #default="{ row }">{{ fmtTime(row.sent_at) }}</template></el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>

    <el-dialog v-model="quick.visible" title="静默告警" width="560px" append-to-body class="quick-dialog">
      <el-form label-width="80px">
        <el-form-item label="范围">
          <el-radio-group v-model="quick.target" class="targets">
            <el-radio value="alarm">只这条告警<span class="text-3">（{{ a?.title }}）</span></el-radio>
            <el-radio value="resource" :disabled="!a?.resource_ref">该资源的全部告警<span class="text-3">（{{ a?.resource_name }}）</span></el-radio>
            <el-radio value="rule">这条规则的全部告警<span class="text-3">（{{ a?.rule_id }}{{ a?.tenant_id ? '，仅当前租户' : '' }}）</span></el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="时长">
          <el-radio-group v-model="quick.minutes" size="small">
            <el-radio-button v-for="d in DURATIONS" :key="d.v" :value="d.v">{{ d.label }}</el-radio-button>
          </el-radio-group>
          <div class="text-3 until">到 {{ quickUntil }} 自动结束，可在“维护与静默”中提前结束</div>
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="quick.reason" type="textarea" :rows="2" maxlength="500" show-word-limit placeholder="例如：已知问题处理中 / 变更单号（会写入告警时间线和审计）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-link type="primary" class="adv" underline="never" @click="openAdvanced">更多设置（级别、每周重复…）</el-link>
        <el-button @click="quick.visible = false">取消</el-button>
        <el-button type="primary" :loading="quick.saving" @click="doQuick">静默 {{ DURATIONS.find((d) => d.v === quick.minutes)?.label }}</el-button>
      </template>
    </el-dialog>
    <SilenceDialog v-model:visible="advanced.visible" :preset="advanced.preset" @saved="onAdvancedSaved" />

    <el-dialog v-model="preview.visible" title="邮件预览" width="820px" append-to-body top="5vh" class="mail-dialog">
      <div class="mail-head">
        <el-radio-group v-model="preview.kind" size="small" @change="loadPreview">
          <el-radio-button value="FIRING">告警通知</el-radio-button>
          <el-radio-button value="UPGRADE">升级通知</el-radio-button>
          <el-radio-button value="RESOLVED">恢复通知</el-radio-button>
        </el-radio-group>
        <span class="text-3">使用真实模板渲染，不会发送邮件</span>
      </div>
      <div v-if="preview.data" class="mail-meta">
        <div><span class="k">主题</span><b>{{ preview.data.subject }}</b></div>
        <div><span class="k">收件人</span>{{ preview.data.recipients.join('；') || '-' }}</div>
        <div><span class="k">命中路由</span><el-tag v-for="r in preview.data.routes" :key="r" size="small" effect="plain">{{ r }}</el-tag></div>
      </div>
      <iframe v-if="preview.data" class="mail-frame" :srcdoc="preview.data.html" sandbox="" title="邮件预览" />
    </el-dialog>
  </el-drawer>
</template>

<script setup lang="ts">
import { Check, CircleClose, Close, Link, Message, MuteNotification, Right } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import dayjs from 'dayjs'
import { useRouter } from 'vue-router'
import { ackAlarm, closeAlarm, getAlarm, previewMail, quickSilence } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import { resourceMonitoringLocation } from '@/config/resource-navigation'
import type { AlarmDetail, Silence, SilenceIn } from '@/types'
import { ALARM_STATUS, LOG_ACTION, NOTIFY_KIND, NOTIFY_STATUS, RESOURCE_TYPES, SERVICES, SEVERITY, SILENCE_KIND, SILENCE_STATUS } from '@/utils/dict'
import { durationOf, fmtTime } from '@/utils/format'
import { copyText } from '@/utils/clipboard'
import SilenceDialog from '@/views/silences/SilenceDialog.vue'

const props = defineProps<{ modelValue: number | null }>()
const emit = defineEmits<{ 'update:modelValue': [v: number | null]; changed: [] }>()

const router = useRouter()
const canAck = computed(() => useUserStore().can('alarm:handle'))
const canSilence = computed(() => useUserStore().can('silence:manage'))
const a = ref<AlarmDetail>()
const monitoringLink = computed(() => resourceMonitoringLocation(a.value && {
  id: a.value.resource_ref, tenant_id: a.value.tenant_id, resource_type: a.value.resource_type,
}, useUserStore()))
const detailRequest = useLatestRequest()
const loading = detailRequest.loading
const acting = ref<'' | 'ack' | 'close'>('')
const tab = ref('timeline')
const preview = reactive({ visible: false, kind: 'FIRING', data: null as Awaited<ReturnType<typeof previewMail>> | null })
const previewRequest = useLatestRequest()

const active = computed(() => !!a.value && ['FIRING', 'ACKED', 'SUPPRESSED'].includes(a.value.status))
const logs = computed(() => [...(a.value?.logs ?? [])].reverse()) // 最新在上
const facts = computed(() => Object.entries((a.value?.detail.facts ?? {}) as Record<string, unknown>)
  .map(([key, v]) => ({ key, value: typeof v === 'object' ? JSON.stringify(v) : String(v) })))
const statusLabel = (s: string | null) => (s ? ALARM_STATUS[s]?.label ?? s : '-')

async function load(id: number) {
  return detailRequest.run(() => getAlarm(id), detail => { a.value = detail })
}
watch(() => props.modelValue, id => {
  detailRequest.invalidate()
  a.value = undefined
  preview.visible = false
  previewRequest.invalidate()
  preview.data = null
  if (id !== null) { tab.value = 'timeline'; void load(id).catch(() => undefined) }
}, { immediate: true })

const close = () => emit('update:modelValue', null)

async function doAck() {
  const id = a.value?.id
  if (!id || acting.value) return
  acting.value = 'ack'
  try {
    await ackAlarm(id)
    ElMessage.success('已认领，不再重复通知和升级')
    if (props.modelValue === id) await load(id)
    emit('changed')
  } finally { acting.value = '' }
}
async function doClose() {
  const id = a.value?.id
  if (!id || acting.value || !canAck.value) return
  acting.value = 'close'
  try {
  const { value } = await ElMessageBox.prompt('关闭后告警不再参与去重与升级；若问题未解决，下次巡检会产生新告警。', '关闭告警', {
    inputPlaceholder: '关闭原因（必填），例如：误报 / 资源已下线', inputValidator: (v) => !!v?.trim() || '请填写关闭原因',
    confirmButtonText: '确认关闭', cancelButtonText: '取消', type: 'warning',
  })
    if (props.modelValue !== id) return
    await closeAlarm(id, value)
    ElMessage.success('告警已关闭')
    if (props.modelValue === id) await load(id)
    emit('changed')
  } catch { /* 取消不执行，接口错误由请求层显示。 */ }
  finally { acting.value = '' }
}
async function loadPreview() {
  const id = a.value?.id, kind = preview.kind
  if (!id || !preview.visible) return false
  return previewRequest.run(() => previewMail(id, kind), value => { preview.data = value })
}
async function openPreview() {
  preview.kind = a.value?.status === 'RESOLVED' ? 'RESOLVED' : 'FIRING'
  preview.visible = true
  await loadPreview()
}
// ---- 静默：快速静默（只这条 / 该资源 / 该规则）或打开完整表单 ----
const DURATIONS = [
  { v: 30, label: '30 分钟' }, { v: 60, label: '1 小时' }, { v: 120, label: '2 小时' }, { v: 240, label: '4 小时' },
  { v: 480, label: '8 小时' }, { v: 1440, label: '1 天' }, { v: 4320, label: '3 天' },
]
const quick = reactive({ visible: false, target: 'alarm' as 'alarm' | 'resource' | 'rule', minutes: 120, reason: '', saving: false })
const quickUntil = computed(() => dayjs().add(quick.minutes, 'minute').format('MM-DD HH:mm'))
function openQuick() {
  if (!canSilence.value || quick.saving || !a.value) return
  Object.assign(quick, { visible: true, target: 'alarm', minutes: 120, reason: '' })
}
async function doQuick() {
  const id = a.value?.id
  if (!id || quick.saving || !canSilence.value) return
  quick.saving = true
  try {
    const res = await quickSilence(id, { minutes: quick.minutes, target: quick.target, reason: quick.reason.trim() })
    ElMessage.success(`已静默至 ${fmtTime(res.silence.ends_at, 'MM-DD HH:mm')}${res.suppressed > 1 ? `，共抑制 ${res.suppressed} 条告警` : ''}`)
    quick.visible = false
    if (props.modelValue === id) await load(id)
    emit('changed')
  } finally { quick.saving = false }
}
const advanced = reactive({ visible: false, alarmId: null as number | null, preset: null as Partial<SilenceIn> | null })
function openAdvanced() {
  const x = a.value
  if (!x || !canSilence.value || quick.saving) return
  advanced.alarmId = x.id
  const tenant = x.tenant_id ? { tenant_ids: [x.tenant_id] } : {}
  const matchers = quick.target === 'resource' && x.resource_ref ? { ...tenant, resource_refs: [x.resource_ref] }
    : quick.target === 'rule' ? { ...tenant, rule_ids: [x.rule_id] } : { ...tenant, fingerprints: [x.fingerprint] }
  advanced.preset = { kind: 'SILENCE', name: `静默：${x.resource_name} · ${x.title}`.slice(0, 128), matchers, reason: quick.reason }
  quick.visible = false
  advanced.visible = true
}
watch(() => props.modelValue, () => { quick.visible = false; advanced.visible = false }, { flush: 'sync' })
async function onAdvancedSaved(_: Silence) {
  if (advanced.alarmId != null && props.modelValue === advanced.alarmId) await load(advanced.alarmId)
  emit('changed')
}
function goSilence(id: number) {
  close()
  router.push({ path: '/silences', query: { id: String(id) } })
}

async function copyLink() {
  const url = `${location.origin}/alarms?id=${a.value!.id}`
  try {
    if (!await copyText(url)) throw new Error('copy unavailable')
    ElMessage.success('告警链接已复制')
  } catch {
    ElMessage.info(url)
  }
}
</script>

<style scoped lang="scss">
.wrap { min-height: 100%; }
.head {
  margin: -16px -20px 0; padding: 18px 24px 16px; border-top: 4px solid var(--c);
  background: linear-gradient(180deg, var(--bg) 0%, #fff 100%);
  .row1 { display: flex; align-items: center; gap: 8px; .x { margin-left: auto; } }
  h3 { margin: 12px 0 8px; font-size: 19px; line-height: 1.4; color: var(--hi-text-1); }
  .meta { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--hi-text-2); b { color: var(--hi-text-1); } }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; .el-button + .el-button { margin-left: 0; } }
}
.kv {
  display: grid; grid-template-columns: repeat(4, 1fr); margin: 16px 0; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
  > div { padding: 10px 14px; display: flex; flex-direction: column; gap: 4px; & + div { border-left: 1px solid var(--hi-border); } }
  span { font-size: 12px; color: var(--hi-text-3); }
  b { font-size: 15px; color: var(--hi-text-1); }
}
.tabs { margin-top: 12px; }
.silence-alert { margin-top: 14px; :deep(.el-alert__title) { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }
  .s-body { margin-top: 4px; font-size: 13px; line-height: 1.7; color: var(--hi-text-2); } }
.targets { display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  .el-radio { height: 28px; margin-right: 0; } .text-3 { margin-left: 4px; font-size: 12px; } }
.until { width: 100%; margin-top: 6px; font-size: 12px; }
.adv { float: left; line-height: 32px; }
.timeline { padding: 4px 4px 0; }
.log { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  .flow { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--hi-text-2); padding: 0 8px; border-radius: 10px; background: var(--hi-bg-soft); } }
.note { margin-top: 6px; padding: 8px 12px; font-size: 13px; color: var(--hi-text-2); background: var(--hi-bg-soft); border-radius: 6px; }
.tip { margin: 0 0 10px; }
.notification-error { margin-top: 6px; color: var(--hi-text-2); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.runbook { white-space: pre-wrap; line-height: 1.8; padding: 14px 16px; border-left: 3px solid var(--el-color-primary); background: var(--el-color-primary-light-9); border-radius: 0 6px 6px 0; }
.mail-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.mail-meta { padding: 10px 14px; margin-bottom: 12px; border-radius: 6px; background: var(--hi-bg-soft); font-size: 13px; display: grid; gap: 6px;
  .k { display: inline-block; width: 64px; color: var(--hi-text-3); } .el-tag { margin-right: 6px; } }
.mail-frame { width: 100%; height: 62vh; border: 1px solid var(--hi-border); border-radius: 6px; background: #f3f5f9; }
</style>
