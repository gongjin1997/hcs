<template>
  <div v-loading="loading" class="settings-panel">
    <el-alert type="info" :closable="false" show-icon class="mb-16"
              title="保存后生效，无需重启。未设置时使用部署默认值；修改记录可在操作审计中查看。" />
    <el-row v-if="canSettings" :gutter="16">
      <!-- 数据保留 -->
      <el-col :xs="24" :lg="12">
        <div v-if="retention" class="box">
          <div class="box-title">
            <el-icon><Delete /></el-icon>{{ retention.label }}
            <span class="text-3">“数据清理”任务每天删除过期数据</span>
          </div>
          <el-form label-width="250px" label-position="left" size="default" class="form" :disabled="!retention.editable || busy" @submit.prevent>
            <el-form-item v-for="f in retention.fields" :key="f.key" :label="f.label">
              <el-input-number v-model="ret[f.key]" :min="f.ge" :max="f.le" :step="f.key === 'pod_event_days' ? 1 : 30" controls-position="right"
                               :disabled="!retention.editable" style="width: 150px" />
              <span class="unit">天</span>
              <span v-if="ret[f.key] !== f.default" class="text-3 def">默认 {{ f.default }}</span>
            </el-form-item>
          </el-form>
          <div class="foot">
            <span class="text-3">{{ metaText(retention) }}</span>
            <el-button v-if="retention.editable" :disabled="!retDirty" @click="resetRet">恢复</el-button>
            <el-button v-if="retention.editable" type="primary" :disabled="!retDirty" :loading="saving === 'retention'" @click="saveRet">保存</el-button>
          </div>
        </div>
      </el-col>

      <!-- 平台自身告警 -->
      <el-col :xs="24" :lg="12">
        <div v-if="platform" class="box">
          <div class="box-title">
            <el-icon><WarningFilled /></el-icon>{{ platform.label }}
            <span class="text-3">监测任务失败、认证失败、通知失败和进程停止</span>
          </div>
          <el-form label-width="250px" label-position="left" class="form" :disabled="!platform.editable || busy" @submit.prevent>
            <el-form-item :label="label(platform, 'enabled')">
              <el-switch v-model="pa.enabled" :disabled="!platform.editable" />
            </el-form-item>
            <template v-if="pa.enabled">
              <el-form-item :label="label(platform, 'task_fail_threshold')">
                <el-input-number v-model="pa.task_fail_threshold" :min="1" :max="20" controls-position="right" :disabled="!platform.editable" style="width: 150px" />
                <span class="unit">次</span>
              </el-form-item>
              <el-form-item :label="label(platform, 'heartbeat_timeout_min')">
                <el-input-number v-model="pa.heartbeat_timeout_min" :min="1" :max="60" controls-position="right" :disabled="!platform.editable" style="width: 150px" />
                <span class="unit">分钟</span>
              </el-form-item>
              <el-form-item :label="label(platform, 'auth_fail_alarm')"><el-switch v-model="pa.auth_fail_alarm" :disabled="!platform.editable" /></el-form-item>
              <el-form-item :label="label(platform, 'notify_fail_alarm')"><el-switch v-model="pa.notify_fail_alarm" :disabled="!platform.editable" /></el-form-item>
              <el-form-item :label="label(platform, 'severity')">
                <el-radio-group v-model="pa.severity" :disabled="!platform.editable">
                  <el-radio-button v-for="s in SEV3" :key="s" :value="s">{{ s }} {{ SEVERITY[s].label }}</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="兜底收件人">
                <RecipientSelect v-model="pa.recipient_user_ids" global-only :disabled="!platform.editable || busy" :active="pa.enabled" />
                <div class="text-3 hint">未命中路由时通知所选人员；未选择时通知可接收邮件的全租户平台管理员。</div>
              </el-form-item>
            </template>
          </el-form>
          <div class="foot">
            <span class="text-3">{{ metaText(platform) }}</span>
            <el-button v-if="platform.editable" :disabled="!paDirty" @click="resetPa">恢复</el-button>
            <el-button v-if="platform.editable" type="primary" :disabled="!paDirty" :loading="saving === 'platform_alarm'" @click="savePa">保存</el-button>
          </div>
        </div>
      </el-col>
    </el-row>

    <CollectionPanel v-if="collection" ref="collectionRef" :setting="collection" :disabled="loading || !!saving || confirmingReload" class="mt-16" @saved="load('collect')" />
    <!-- 升级短信：总开关 + 各租户独立网关（原“租户接入”卡片上的短信配置移到这里） -->
    <div class="sms-section mt-16" aria-label="升级短信">
      <SmsGlobalPanel ref="smsRef" :disabled="loading || !!saving || confirmingReload" @saved="smsTenantRef?.reload()" />
      <SmsTenantPanel v-if="canTenantSms" ref="smsTenantRef" :disabled="loading || !!saving || confirmingReload" class="mt-12" />
    </div>

    <!-- 其他设置的入口 -->
    <div class="box mt-16 links">
      <div class="box-title"><el-icon><Link /></el-icon>其他设置</div>
      <div class="link-row">
        <span><b>定时报表</b>（发送时间 / 收件人）：系统管理 → 巡检任务 → “修改发送时间 / 收件人”</span>
        <span><b>台账字典</b>（业务系统）：资源台账 → 表格标题栏“字典”</span>
        <span><b>人员 / 负责人 / 通知联系人</b>：系统管理 → 用户管理（可仅登记人员，不开通登录）</span>
        <span><b>采集开关</b>（按资源类型启停）：系统管理 → 采集开关</span>
        <span><b>ECS 采集策略</b>（各租户频率 / 范围 / 指标）：系统管理 → 巡检任务 → ECS 任务行的“租户策略”</span>
        <span><b>SMTP / IMAP 邮件服务</b>：通知管理 → 邮件服务与测试（页面保存，无需重启）</span>
        <span><b>升级短信</b>：在本页“租户短信”中为租户配置网关并开启，再开启总开关。</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Delete, Link, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { getSettings, saveSetting } from '@/api'
import { useUserStore } from '@/stores/user'
import type { PlatformAlarmSetting, Retention, SettingItem } from '@/types'
import { SEVERITY } from '@/utils/dict'
import { fmtTime } from '@/utils/format'
import CollectionPanel from './CollectionPanel.vue'
import RecipientSelect from '@/components/RecipientSelect.vue'
import SmsGlobalPanel from './SmsGlobalPanel.vue'
import SmsTenantPanel from './SmsTenantPanel.vue'

const SEV3 = ['P1', 'P2', 'P3'] as const
const user = useUserStore()
/** 数据保留 / 平台告警 / 采集并发需要“系统设置”权限；租户短信需要“管理租户接入”权限（两者可以只具备其一）。 */
const canSettings = computed(() => user.can('setting:manage'))
const canTenantSms = computed(() => user.can('tenant:manage'))
const smsTenantRef = ref<{ reload: () => Promise<void> } | null>(null)
const loading = ref(false)
const saving = ref('')
const confirmingReload = ref(false)
const collectionRef = ref<{ busy: boolean; dirty: boolean } | null>(null)
const smsRef = ref<{ reload: () => Promise<void>; busy: boolean; dirty: boolean } | null>(null)
const busy = computed(() => loading.value || !!saving.value || confirmingReload.value || !!collectionRef.value?.busy || !!smsRef.value?.busy)
const retention = ref<SettingItem<Retention> | null>(null)
const platform = ref<SettingItem<PlatformAlarmSetting> | null>(null)
const collection = ref<SettingItem<{ hcs_concurrency: number; hcs_qps: Record<string, number> }> | null>(null)
const ret = reactive<Record<string, number>>({})
const pa = reactive<PlatformAlarmSetting & { recipient_user_ids: number[] }>({ enabled: true, task_fail_threshold: 3, auth_fail_alarm: true, notify_fail_alarm: true, heartbeat_timeout_min: 3, severity: 'P2', mail_to: [], recipient_user_ids: [] })

async function load(group?: 'retention' | 'platform_alarm' | 'collect') {
  if (loading.value || !canSettings.value) return
  loading.value = true
  try {
    const s = await getSettings()
    retention.value = s.retention ?? null
    platform.value = s.platform_alarm ?? null
    if (!group || group === 'collect' || !collectionRef.value?.dirty) collection.value = s.collect ?? null
    if (!group || group === 'retention') resetRet()
    if (!group || group === 'platform_alarm') resetPa()
  } catch {
    // 请求层提示错误，保留未保存的表单。
  } finally { loading.value = false }
}
onMounted(load)
async function reload() {
  if (busy.value) return
  if (retDirty.value || paDirty.value || collectionRef.value?.dirty || smsRef.value?.dirty) {
    confirmingReload.value = true
    try {
      await ElMessageBox.confirm('未保存的设置将丢失，是否刷新？', '刷新', {
        type: 'warning', confirmButtonText: '刷新', cancelButtonText: '取消',
      })
    } catch { return }
    finally { confirmingReload.value = false }
  }
  await Promise.all([load(), smsRef.value?.reload(), smsTenantRef.value?.reload()])
}
defineExpose({ reload, busy })

const label = (s: SettingItem, key: string) => s.fields.find((f) => f.key === key)?.label ?? key
const metaText = (s: SettingItem) => (s.customized && s.updated_by ? `${s.updated_by} 于 ${fmtTime(s.updated_at, 'YYYY-MM-DD HH:mm')} 修改` : '使用默认值')

function resetRet() { if (retention.value) Object.assign(ret, retention.value.value) }
function resetPa() { if (platform.value) Object.assign(pa, { ...platform.value.value, mail_to: [...(platform.value.value.mail_to ?? [])], recipient_user_ids: [...(platform.value.value.recipient_user_ids ?? [])] }) }
const retDirty = computed(() => !!retention.value && Object.entries(retention.value.value).some(([k, v]) => ret[k] !== v))
const paDirty = computed(() => !!platform.value && Object.entries({ ...platform.value.value, recipient_user_ids: platform.value.value.recipient_user_ids ?? [] })
  .some(([key, value]) => key !== 'mail_to' && JSON.stringify(pa[key as keyof typeof pa]) !== JSON.stringify(value)))

async function saveRet() {
  if (busy.value) return
  saving.value = 'retention'
  try {
    const r = await saveSetting<Retention>('retention', { ...ret } as Partial<Retention>)
    ElMessage.success(r.changed.length ? `已保存（${r.changed.length} 项），下次数据清理按新保留期执行` : '没有变化')
    await load('retention')
  } catch { /* 请求层提示错误，保留草稿。 */ }
  finally { saving.value = '' }
}
async function savePa() {
  if (busy.value) return
  saving.value = 'platform_alarm'
  try {
    const { mail_to: _resolved, ...body } = pa
    const r = await saveSetting<PlatformAlarmSetting>('platform_alarm', body)
    ElMessage.success(r.changed.length ? '已保存，调度进程下一轮检查时生效' : '没有变化')
    await load('platform_alarm')
  } catch { /* 请求层提示错误，保留草稿。 */ }
  finally { saving.value = '' }
}
</script>

<style scoped lang="scss">
.box { height: 100%; padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff; display: flex; flex-direction: column;
  .box-title { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-weight: 600;
    .el-icon { color: var(--el-color-primary); } .text-3 { font-weight: 400; font-size: 12px; } } }
.form { flex: 1; :deep(.el-form-item) { margin-bottom: 12px; }
  // 标签过长时换行显示（默认固定 32px 行高会与下一行重叠）
  :deep(.el-form-item__label) { height: auto; min-height: 32px; align-items: center; line-height: 1.4; color: var(--hi-text-2); } }
.unit { margin-left: 8px; color: var(--hi-text-3); }
.def { margin-left: 10px; font-size: 12px; }
.hint { width: 100%; margin-top: 4px; font-size: 12px; line-height: 1.6; }
.foot { display: flex; align-items: center; gap: 8px; padding-top: 10px; border-top: 1px dashed var(--hi-border-light);
  .text-3 { margin-right: auto; font-size: 12px; } }
.links .link-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; font-size: 13px; color: var(--hi-text-2); }
.mt-12 { margin-top: 12px; }
@media (max-width: 767px) {
  :deep(.el-row) { row-gap: 16px; }
}
</style>
