<template>
  <div v-loading="loading" class="settings-panel">
    <el-alert type="info" :closable="false" show-icon class="mb-16"
              title="设置保存在数据库中，保存后各进程下次使用时生效，无需重启；每次修改写入操作审计。环境变量（.env）只作为首次部署的默认值。" />
    <el-row :gutter="16">
      <!-- 数据保留 -->
      <el-col :span="12">
        <div v-if="retention" class="box">
          <div class="box-title">
            <el-icon><Delete /></el-icon>{{ retention.label }}
            <span class="text-3">每天低峰由“数据清理”任务删除超过保留期的数据</span>
          </div>
          <el-form label-width="250px" label-position="left" size="default" class="form">
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
      <el-col :span="12">
        <div v-if="platform" class="box">
          <div class="box-title">
            <el-icon><WarningFilled /></el-icon>{{ platform.label }}
            <span class="text-3">巡检平台自己出问题时（任务失败、认证失败、通知失败、进程停止）主动告警</span>
          </div>
          <el-form label-width="250px" label-position="left" class="form">
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
                <el-select v-model="pa.mail_to" multiple filterable allow-create default-first-option :reserve-keyword="false" class="w-full"
                           :disabled="!platform.editable" placeholder="输入邮箱后回车" />
                <div class="text-3 hint">{{ label(platform, 'mail_to') }}。平台告警优先按“通知管理”的路由（服务 = 巡检平台）发送</div>
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

    <!-- 其他设置的入口 -->
    <div class="box mt-16 links">
      <div class="box-title"><el-icon><Link /></el-icon>其他设置</div>
      <div class="link-row">
        <span><b>定时报表</b>（发送时间 / 收件人）：系统管理 → 巡检任务 → “修改发送时间 / 收件人”</span>
        <span><b>台账字典</b>（业务系统 / 负责人）：资源台账 → 右上角“字典”</span>
        <span><b>采集开关</b>（按资源类型启停）：系统管理 → 采集开关</span>
        <span><b>SMTP 与测试邮件</b>：通知管理 → SMTP 与测试邮件（SMTP 账号在部署目录 .env 中配置）</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Delete, Link, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { getSettings, saveSetting } from '@/api'
import type { PlatformAlarmSetting, Retention, SettingItem } from '@/types'
import { SEVERITY } from '@/utils/dict'
import { fmtTime } from '@/utils/format'

const SEV3 = ['P1', 'P2', 'P3'] as const
const loading = ref(false)
const saving = ref('')
const retention = ref<SettingItem<Retention> | null>(null)
const platform = ref<SettingItem<PlatformAlarmSetting> | null>(null)
const ret = reactive<Record<string, number>>({})
const pa = reactive<PlatformAlarmSetting>({ enabled: true, task_fail_threshold: 3, auth_fail_alarm: true, notify_fail_alarm: true, heartbeat_timeout_min: 3, severity: 'P2', mail_to: [] })

async function load() {
  loading.value = true
  try {
    const s = await getSettings()
    retention.value = s.retention ?? null
    platform.value = s.platform_alarm ?? null
    resetRet()
    resetPa()
  } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })

const label = (s: SettingItem, key: string) => s.fields.find((f) => f.key === key)?.label ?? key
const metaText = (s: SettingItem) => (s.customized && s.updated_by ? `${s.updated_by} 于 ${fmtTime(s.updated_at, 'YYYY-MM-DD HH:mm')} 修改` : '使用默认值')

function resetRet() { if (retention.value) Object.assign(ret, retention.value.value) }
function resetPa() { if (platform.value) Object.assign(pa, { ...platform.value.value, mail_to: [...(platform.value.value.mail_to ?? [])] }) }
const retDirty = computed(() => !!retention.value && Object.entries(retention.value.value).some(([k, v]) => ret[k] !== v))
const paDirty = computed(() => !!platform.value && JSON.stringify({ ...platform.value.value, mail_to: platform.value.value.mail_to ?? [] }) !== JSON.stringify({ ...pa }))

async function saveRet() {
  saving.value = 'retention'
  try {
    const r = await saveSetting<Retention>('retention', { ...ret } as Partial<Retention>)
    ElMessage.success(r.changed.length ? `已保存（${r.changed.length} 项），下次数据清理按新保留期执行` : '没有变化')
    await load()
  } finally { saving.value = '' }
}
async function savePa() {
  const bad = pa.mail_to.find((m) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(m))
  if (bad) return ElMessage.warning(`邮箱格式不正确：${bad}`)
  saving.value = 'platform_alarm'
  try {
    const r = await saveSetting<PlatformAlarmSetting>('platform_alarm', { ...pa })
    ElMessage.success(r.changed.length ? '已保存，调度进程下一轮检查时生效' : '没有变化')
    await load()
  } finally { saving.value = '' }
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
</style>
