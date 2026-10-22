<template>
  <el-drawer :model-value="modelValue !== null" size="780px" :with-header="false" class="alarm-drawer" @close="close">
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
            <span>{{ SERVICES[a.service] }} · {{ RESOURCE_TYPES[a.resource_type] }}</span>
            <span>持续 <b>{{ durationOf(a.first_seen, a.resolved_at) }}</b></span>
            <span>累计触发 <b class="num">{{ a.count }}</b> 次</span>
          </div>
          <div class="actions">
            <el-button v-if="a.status === 'FIRING'" type="primary" :icon="Check" :disabled="!canAck" :loading="acting === 'ack'" @click="doAck">认领</el-button>
            <el-button :icon="CircleClose" :disabled="!canAck || ['RESOLVED', 'CLOSED'].includes(a.status)" :loading="acting === 'close'" @click="doClose">关闭</el-button>
            <el-tooltip content="静默能力随“维护与静默”模块提供（规划 v1.1）" placement="top">
              <span><el-button :icon="MuteNotification" disabled>静默 2 小时</el-button></span>
            </el-tooltip>
            <el-button :icon="Message" @click="openPreview">邮件预览</el-button>
            <el-button :icon="Link" @click="copyLink">复制链接</el-button>
          </div>
        </div>

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
            <el-table :data="a.notifications" size="small">
              <el-table-column label="类型" width="90"><template #default="{ row }">{{ NOTIFY_KIND[row.kind] ?? row.kind }}</template></el-table-column>
              <el-table-column label="主题" min-width="260" show-overflow-tooltip prop="subject" />
              <el-table-column label="收件人" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ row.recipients.join('，') }}</template></el-table-column>
              <el-table-column label="状态" width="90"><template #default="{ row }">
                <el-tag :type="NOTIFY_STATUS[row.status]?.type ?? 'info'" size="small">{{ NOTIFY_STATUS[row.status]?.label ?? row.status }}</el-tag></template></el-table-column>
              <el-table-column label="发送时间" width="140"><template #default="{ row }">{{ fmtTime(row.sent_at) }}</template></el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>

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
import { ackAlarm, closeAlarm, getAlarm, previewMail } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useUserStore } from '@/stores/user'
import type { AlarmDetail } from '@/types'
import { ALARM_STATUS, LOG_ACTION, NOTIFY_KIND, NOTIFY_STATUS, RESOURCE_TYPES, SERVICES, SEVERITY } from '@/utils/dict'
import { durationOf, fmtTime } from '@/utils/format'

const props = defineProps<{ modelValue: number | null }>()
const emit = defineEmits<{ 'update:modelValue': [v: number | null]; changed: [] }>()

const canAck = computed(() => useUserStore().can('alarm:handle'))
const a = ref<AlarmDetail>()
const loading = ref(false)
const acting = ref<'' | 'ack' | 'close'>('')
const tab = ref('timeline')
const preview = reactive({ visible: false, kind: 'FIRING', data: null as Awaited<ReturnType<typeof previewMail>> | null })

const active = computed(() => !!a.value && ['FIRING', 'ACKED', 'SUPPRESSED'].includes(a.value.status))
const logs = computed(() => [...(a.value?.logs ?? [])].reverse()) // 最新在上
const facts = computed(() => Object.entries((a.value?.detail.facts ?? {}) as Record<string, unknown>)
  .map(([key, v]) => ({ key, value: typeof v === 'object' ? JSON.stringify(v) : String(v) })))
const statusLabel = (s: string | null) => (s ? ALARM_STATUS[s]?.label ?? s : '-')

async function load(id: number) {
  loading.value = true
  try { a.value = await getAlarm(id) } finally { loading.value = false }
}
watch(() => props.modelValue, (id) => { if (id !== null) { tab.value = 'timeline'; load(id) } }, { immediate: true })

const close = () => emit('update:modelValue', null)

async function doAck() {
  acting.value = 'ack'
  try {
    await ackAlarm(a.value!.id)
    ElMessage.success('认领成功，已停止重复通知；升级计时暂停')
    await load(a.value!.id)
    emit('changed')
  } finally { acting.value = '' }
}
async function doClose() {
  const { value } = await ElMessageBox.prompt('关闭后告警不再参与去重与升级；若问题未解决，下次巡检会产生新告警。', '关闭告警', {
    inputPlaceholder: '关闭原因（必填），例如：误报 / 资源已下线', inputValidator: (v) => !!v?.trim() || '请填写关闭原因',
    confirmButtonText: '确认关闭', cancelButtonText: '取消', type: 'warning',
  })
  acting.value = 'close'
  try {
    await closeAlarm(a.value!.id, value)
    ElMessage.success('告警已关闭')
    await load(a.value!.id)
    emit('changed')
  } finally { acting.value = '' }
}
async function loadPreview() {
  preview.data = await previewMail(a.value!.id, preview.kind)
}
async function openPreview() {
  preview.kind = a.value?.status === 'RESOLVED' ? 'RESOLVED' : 'FIRING'
  preview.visible = true
  await loadPreview()
}
async function copyLink() {
  const url = `${location.origin}/alarms?id=${a.value!.id}`
  try {
    await navigator.clipboard.writeText(url)
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
.timeline { padding: 4px 4px 0; }
.log { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  .flow { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--hi-text-2); padding: 0 8px; border-radius: 10px; background: var(--hi-bg-soft); } }
.note { margin-top: 6px; padding: 8px 12px; font-size: 13px; color: var(--hi-text-2); background: var(--hi-bg-soft); border-radius: 6px; }
.tip { margin: 0 0 10px; }
.runbook { white-space: pre-wrap; line-height: 1.8; padding: 14px 16px; border-left: 3px solid var(--el-color-primary); background: var(--el-color-primary-light-9); border-radius: 0 6px 6px 0; }
.mail-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.mail-meta { padding: 10px 14px; margin-bottom: 12px; border-radius: 6px; background: var(--hi-bg-soft); font-size: 13px; display: grid; gap: 6px;
  .k { display: inline-block; width: 64px; color: var(--hi-text-3); } .el-tag { margin-right: 6px; } }
.mail-frame { width: 100%; height: 62vh; border: 1px solid var(--hi-border); border-radius: 6px; background: #f3f5f9; }
</style>
