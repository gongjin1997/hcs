<template>
  <div v-loading="loading" class="smtp-panel">
    <el-row :gutter="20">
      <el-col :span="12">
        <div class="box">
          <div class="box-title"><el-icon><Setting /></el-icon>当前 SMTP 配置<span class="text-3">只读；在部署目录 .env 中修改后执行 make restart</span></div>
          <el-descriptions v-if="info" :column="1" border size="default">
            <el-descriptions-item label="服务器"><span class="mono">{{ info.host }}:{{ info.port }}</span></el-descriptions-item>
            <el-descriptions-item label="加密">{{ info.starttls ? 'STARTTLS' : '不加密（内网中继）' }}</el-descriptions-item>
            <el-descriptions-item label="认证">{{ info.auth ? '账号密码（口令不在页面显示）' : '匿名（内网中继白名单）' }}</el-descriptions-item>
            <el-descriptions-item label="发件人"><span class="mono">{{ info.sender }}</span></el-descriptions-item>
            <el-descriptions-item label="回复地址"><span class="mono">{{ info.reply_to || '-' }}</span></el-descriptions-item>
            <el-descriptions-item label="环境标识">【{{ info.env_tag }}】<span class="text-3">（邮件主题前缀）</span></el-descriptions-item>
            <el-descriptions-item label="平台访问地址"><span class="mono">{{ info.base_url }}</span><div class="text-3">邮件里“查看详情 / 认领”链接使用该地址，需要是运维同事能打开的内网地址</div></el-descriptions-item>
          </el-descriptions>
          <div class="env text-3">
            对应环境变量：<span class="mono">INSPECTOR_SMTP_HOST / _PORT / _STARTTLS / _USER / _PASSWORD / _SENDER / _REPLY_TO、INSPECTOR_ENV_TAG、INSPECTOR_BASE_URL</span>
          </div>
        </div>
      </el-col>
      <el-col :span="12">
        <div class="box">
          <div class="box-title"><el-icon><Promotion /></el-icon>发送测试邮件<span class="text-3">同步发送，立即返回结果</span></div>
          <el-select v-model="to" multiple filterable allow-create default-first-option :reserve-keyword="false" class="w-full" placeholder="输入收件邮箱后回车，可多个">
            <el-option v-for="c in contacts" :key="c.id" :value="c.email" :label="`${c.name} <${c.email}>`" />
          </el-select>
          <div class="ops">
            <el-button type="primary" :icon="Promotion" :loading="sending" :disabled="!canManage || !to.length" @click="send">发送测试邮件</el-button>
            <span v-if="!canManage" class="text-3">需要“编辑通知路由 / 联系组 / 联系人”权限</span>
          </div>
          <div v-if="history.length" class="history">
            <div v-for="(h, i) in history" :key="i" class="h" :class="h.ok ? 'ok' : 'bad'">
              <el-icon><component :is="h.ok ? CircleCheckFilled : CircleCloseFilled" /></el-icon>
              <span class="num">{{ h.at }}</span><span>{{ h.text }}</span>
            </div>
          </div>
          <div class="text-3 tips">
            <div>常见问题：</div>
            <div>· 连接超时 / 拒绝：检查 API 容器到 SMTP 服务器的网络与端口（25 / 465 / 587）</div>
            <div>· 535 认证失败：账号口令错误，或邮件服务器要求使用授权码</div>
            <div>· 550 / 553 拒收：发件人地址不被允许，或收件域不在中继白名单</div>
          </div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { CircleCheckFilled, CircleCloseFilled, Promotion, Setting } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { sendTestMail, smtpInfo } from '@/api'
import { useUserStore } from '@/stores/user'
import type { Contact, SmtpInfo } from '@/types'

defineProps<{ contacts: Contact[]; canManage: boolean }>()
const user = useUserStore()
const loading = ref(false)
const sending = ref(false)
const info = ref<SmtpInfo | null>(null)
const to = ref<string[]>(user.user?.email ? [user.user.email] : [])
const history = ref<{ at: string; ok: boolean; text: string }[]>([])

onMounted(async () => {
  loading.value = true
  try { info.value = await smtpInfo() } finally { loading.value = false }
})
async function send() {
  sending.value = true
  const at = dayjs().format('HH:mm:ss')
  try {
    const r = await sendTestMail(to.value)
    history.value.unshift({ at, ok: true, text: `已发送到 ${r.sent_to.join('、')}，耗时 ${r.elapsed_ms} ms` })
    ElMessage.success('测试邮件已发送，请到邮箱确认是否收到')
  } catch (e) {
    history.value.unshift({ at, ok: false, text: (e as Error).message })
  } finally { sending.value = false }
}
</script>

<style scoped lang="scss">
.box { height: 100%; padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
  .box-title { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-weight: 600;
    .el-icon { color: var(--el-color-primary); } .text-3 { font-weight: 400; font-size: 12px; } } }
.env { margin-top: 10px; font-size: 12px; line-height: 1.7; }
.ops { display: flex; align-items: center; gap: 10px; margin: 12px 0; }
.history { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;
  .h { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; font-size: 13px;
    &.ok { color: var(--hi-ok); background: var(--hi-ok-bg); } &.bad { color: var(--hi-p1); background: var(--hi-p1-bg); } } }
.tips { font-size: 12px; line-height: 1.8; }
</style>
