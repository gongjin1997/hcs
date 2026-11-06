<template>
  <div v-loading="loading" class="smtp-panel">
    <el-alert v-if="loadFailed" type="error" :closable="false" show-icon class="mb-16" title="邮件设置加载失败。">
      <el-button link type="primary" :disabled="busy" @click="reload">重试</el-button>
    </el-alert>
    <el-alert type="info" :closable="false" show-icon class="mb-16"
              title="SMTP 用于发信，IMAP 用于连接收件箱。保存后生效，无需重启。" />
    <p v-if="info" class="text-3 status">
      {{ info.source === 'database' ? '使用已保存配置' : '尚未保存，使用部署默认值' }}
      <span v-if="!canManage || !info.editable"> · 只读。修改和测试需要通知管理及全部租户权限</span>
    </p>
    <el-row :gutter="20">
      <el-col :xs="24" :lg="12">
        <div class="box">
          <div class="box-title"><el-icon><Setting /></el-icon>SMTP 发信服务</div>
          <el-form label-width="100px" :disabled="!editable || busy" @submit.prevent>
            <el-form-item label="服务器"><el-input v-model="smtp.host" placeholder="smtp.example.com 或 IP" :maxlength="253" /></el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="smtp.port" :min="1" :max="65535" controls-position="right" />
              <span class="hint">25：SMTP 中继；465：SSL；587：STARTTLS</span>
            </el-form-item>
            <el-form-item label="加密方式">
              <el-select v-model="smtp.security">
                <el-option label="NONE（不加密）" value="NONE" /><el-option label="STARTTLS" value="STARTTLS" /><el-option label="SSL / TLS" value="SSL" />
              </el-select>
            </el-form-item>
            <el-form-item label="账号认证"><el-switch v-model="smtp.auth" /><span class="hint">内网白名单中继可关闭</span></el-form-item>
            <el-form-item v-if="smtp.auth" label="账号"><el-input v-model="smtp.username" autocomplete="off" :maxlength="256" /></el-form-item>
            <el-form-item label="密码">
              <el-input v-model="smtp.password" type="password" autocomplete="new-password" :maxlength="1024" :disabled="smtp.clear_password"
                        :placeholder="info?.smtp.password_set ? '已设置，留空保留原密码' : '未设置密码'" />
              <el-checkbox v-model="smtp.clear_password" @change="smtp.password = ''">清除已保存密码</el-checkbox>
            </el-form-item>
            <el-form-item label="发件地址"><el-input v-model="smtp.sender" placeholder="inspector@example.com" :maxlength="254" /></el-form-item>
            <el-form-item label="回复地址"><el-input v-model="smtp.reply_to" placeholder="可留空" :maxlength="254" /></el-form-item>
          </el-form>
        </div>
      </el-col>
      <el-col :xs="24" :lg="12">
        <div class="box">
          <div class="box-title"><el-icon><Message /></el-icon>IMAP 收件箱连接</div>
          <el-form label-width="100px" :disabled="!editable || busy" @submit.prevent>
            <el-form-item label="启用连接"><el-switch v-model="imap.enabled" /></el-form-item>
            <el-form-item label="服务器"><el-input v-model="imap.host" placeholder="imap.example.com 或 IP" :maxlength="253" /></el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="imap.port" :min="1" :max="65535" controls-position="right" />
              <span class="hint">143：IMAP / STARTTLS；993：IMAP SSL</span>
            </el-form-item>
            <el-form-item label="加密方式">
              <el-select v-model="imap.security">
                <el-option label="NONE（不加密）" value="NONE" /><el-option label="STARTTLS" value="STARTTLS" /><el-option label="SSL / TLS" value="SSL" />
              </el-select>
            </el-form-item>
            <el-form-item label="账号"><el-input v-model="imap.username" autocomplete="off" :maxlength="256" /></el-form-item>
            <el-form-item label="密码">
              <el-input v-model="imap.password" type="password" autocomplete="new-password" :maxlength="1024" :disabled="imap.clear_password"
                        :placeholder="info?.imap.password_set ? '已设置，留空保留原密码' : '未设置密码'" />
              <el-checkbox v-model="imap.clear_password" @change="imap.password = ''">清除已保存密码</el-checkbox>
            </el-form-item>
          </el-form>
          <p class="text-3 tips">IMAP 仅测试连接，不收取、移动或删除邮件，也不能用于发信。</p>
        </div>
      </el-col>
    </el-row>
    <div class="ops">
      <el-button v-if="editable" type="primary" :loading="saving" :disabled="busy || (!dirty && info?.source === 'database')" @click="save">保存邮件设置</el-button>
      <span class="text-3">密码留空不变；勾选清除并保存后删除。</span>
    </div>
    <div class="box tests">
      <div class="box-title">连接与发信测试<span class="text-3">修改设置后请先保存</span></div>
      <div class="ops">
        <el-button :loading="testing === 'SMTP'" :disabled="!editable || busy || dirty" @click="testConnection('SMTP')">测试 SMTP 连接（不发信）</el-button>
        <el-button :loading="testing === 'IMAP'" :disabled="!editable || busy || dirty || !info?.imap.enabled" @click="testConnection('IMAP')">测试 IMAP 连接与认证</el-button>
      </div>
      <RecipientSelect v-model="userIds" global-only :disabled="!editable || busy" :active="editable" />
      <div class="ops">
        <el-button type="primary" :icon="Promotion" :loading="sending" :disabled="!editable || busy || dirty || !userIds.length" @click="sendTest">发送测试邮件</el-button>
        <span class="text-3">将向所选收件人实际发信。</span>
      </div>
      <div v-if="history.length" class="history" aria-live="polite">
        <div v-for="(h, i) in history" :key="i" class="h" :class="h.ok ? 'ok' : 'bad'">
          <el-icon><component :is="h.ok ? CircleCheckFilled : CircleCloseFilled" /></el-icon>
          <span>{{ h.at }}</span><span>{{ h.text }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { CircleCheckFilled, CircleCloseFilled, Message, Promotion, Setting } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, shallowRef } from 'vue'
import { getMailSettings, saveMailSettings, sendMailTest, testImapConnection, testSmtpConnection } from '@/api/mail'
import { useUserStore } from '@/stores/user'
import RecipientSelect from '@/components/RecipientSelect.vue'
import type { ImapConfig, MailSettings, PasswordUpdate, SmtpConfig } from '@/types/mail'

const props = defineProps<{ canManage: boolean }>()
const user = useUserStore()
const loading = shallowRef(false)
const loadFailed = shallowRef(false)
const confirmingReload = shallowRef(false)
const saving = shallowRef(false)
const sending = shallowRef(false)
const testing = shallowRef<'SMTP' | 'IMAP' | ''>('')
const info = ref<MailSettings | null>(null)
const smtp = reactive<SmtpConfig & Required<PasswordUpdate>>({ host: '', port: 25, security: 'NONE', auth: false, username: '', sender: '', reply_to: '', password: '', clear_password: false })
const imap = reactive<ImapConfig & Required<PasswordUpdate>>({ enabled: false, host: '', port: 993, security: 'SSL', username: '', password: '', clear_password: false })
const userIds = ref<number[]>(user.user?.email && user.user.id ? [user.user.id] : [])
const history = ref<{ at: string; ok: boolean; text: string }[]>([])
const editable = computed(() => props.canManage && !!info.value?.editable && !loadFailed.value)
const busy = computed(() => loading.value || saving.value || sending.value || !!testing.value || confirmingReload.value)
const dirty = computed(() => {
  if (!info.value) return false
  return (['smtp', 'imap'] as const).some(protocol => {
    const form = protocol === 'smtp' ? smtp : imap
    const { password_set: _set, ...saved } = info.value![protocol]
    const { password, clear_password, ...current } = form
    return !!password || clear_password || JSON.stringify(saved) !== JSON.stringify(current)
  })
})

function apply(value: MailSettings) {
  info.value = value
  const { password_set: _smtpSet, ...smtpValue } = value.smtp
  const { password_set: _imapSet, ...imapValue } = value.imap
  Object.assign(smtp, smtpValue, { password: '', clear_password: false })
  Object.assign(imap, imapValue, { password: '', clear_password: false })
}
async function load() {
  if (busy.value) return
  loading.value = true
  try {
    apply(await getMailSettings())
    loadFailed.value = false
  } catch {
    loadFailed.value = true
  } finally { loading.value = false }
}
async function reload() {
  if (busy.value) return
  if (dirty.value) {
    confirmingReload.value = true
    try {
      await ElMessageBox.confirm('未保存的修改将丢失，是否重新加载？', '重新加载', {
        type: 'warning', confirmButtonText: '重新加载', cancelButtonText: '取消',
      })
    } catch {
      return
    } finally { confirmingReload.value = false }
  }
  await load()
}
onMounted(load)
defineExpose({ reload, busy })

async function save() {
  if (!editable.value || busy.value) return
  saving.value = true
  try {
    apply(await saveMailSettings({ smtp: { ...smtp }, imap: { ...imap } }))
    history.value = []
    ElMessage.success('邮件设置已保存')
  } catch {
    // 请求层统一提示错误，保留草稿供重试。
  } finally { saving.value = false }
}
function record(ok: boolean, text: string) {
  history.value.unshift({ at: dayjs().format('HH:mm:ss'), ok, text })
  history.value = history.value.slice(0, 8)
}
async function testConnection(protocol: 'SMTP' | 'IMAP') {
  if (!editable.value || busy.value || dirty.value) return
  testing.value = protocol
  try {
    const result = await (protocol === 'SMTP' ? testSmtpConnection() : testImapConnection())
    record(true, `${protocol}：${result.message}，耗时 ${result.elapsed_ms} ms`)
  } catch (e) {
    record(false, (e as Error).message)
  } finally { testing.value = '' }
}
async function sendTest() {
  if (!editable.value || busy.value || dirty.value || !userIds.value.length) return
  sending.value = true
  try {
    const result = await sendMailTest([...userIds.value])
    record(true, `已发送到 ${result.sent_to.join('、')}，耗时 ${result.elapsed_ms} ms`)
    ElMessage.success('测试邮件已发送，请到邮箱确认是否收到')
  } catch (e) {
    record(false, (e as Error).message)
  } finally { sending.value = false }
}
</script>

<style scoped lang="scss">
.box { height: 100%; padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
  .box-title { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-weight: 600;
    .el-icon { color: var(--el-color-primary); } .text-3 { font-weight: 400; font-size: 12px; } } }
.status, .tips { font-size: 12px; line-height: 1.7; }
.hint { margin-left: 10px; font-size: 12px; color: var(--hi-text-3); }
.ops { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.tests { margin-top: 16px; }
.w-full { width: 100%; }
.history { display: flex; flex-direction: column; gap: 6px;
  .h { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; font-size: 13px;
    &.ok { color: var(--hi-ok); background: var(--hi-ok-bg); } &.bad { color: var(--hi-p1); background: var(--hi-p1-bg); } } }
</style>
