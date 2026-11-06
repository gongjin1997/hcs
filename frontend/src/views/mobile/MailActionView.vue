<template>
  <!-- 邮件“一键认领”H5 落地页：签名链接免登录，适配手机邮箱客户端内置浏览器 -->
  <div class="m-page">
    <div class="m-bar"><svg viewBox="0 0 32 32" width="22" height="22"><rect width="32" height="32" rx="8" fill="#1d63f0" />
      <path d="M16 6l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V9z" fill="none" stroke="#fff" stroke-width="2.4" /></svg><b>HCS 巡检平台</b><span>告警处理</span></div>

    <div v-if="loading" class="m-state"><el-icon class="spin" :size="32"><Loading /></el-icon><p>正在校验链接…</p></div>

    <div v-else-if="error" class="m-state">
      <div class="icon err"><el-icon :size="34"><WarningFilled /></el-icon></div>
      <h3>{{ error }}</h3>
      <p>链接有效期 24 小时，且仅限收件人本人使用。<br />请登录控制台处理该告警。</p>
      <a class="m-btn ghost" href="/alarms">登录控制台</a>
    </div>

    <template v-else-if="info">
      <div class="m-card" :style="{ '--c': color }">
        <div class="m-head">
          <span class="sev">{{ a.severity }} {{ SEVERITY[a.severity].label }}</span>
          <span class="st">{{ ALARM_STATUS[a.status]?.label }}</span>
        </div>
        <h2>{{ a.title }}</h2>
        <div class="res"><b>{{ a.resource_name }}</b><template v-if="a.sub_object"> / {{ a.sub_object }}</template></div>
        <div class="kv">
          <div><span>当前值</span><b class="num">{{ a.current_value ?? '-' }}</b></div>
          <div><span>阈值</span><b>{{ a.threshold_text ?? '-' }}</b></div>
          <div><span>首次发生</span><b class="num">{{ fmtTime(a.first_seen, 'MM-DD HH:mm') }}</b></div>
          <div><span>已持续</span><b>{{ durationOf(a.first_seen, a.resolved_at) }}</b></div>
        </div>
        <div class="rows">
          <div><span>服务</span>{{ SERVICES[a.service] }} · {{ RESOURCE_TYPES[a.resource_type] }}</div>
          <div><span>负责人</span>{{ a.owner ?? '-' }}</div>
          <div><span>规则</span><em class="mono">{{ a.rule_id }}</em></div>
          <div v-if="a.ack_by"><span>认领人</span>{{ a.ack_by }} · {{ fmtTime(a.ack_at, 'MM-DD HH:mm') }}</div>
        </div>
      </div>

      <div v-if="done" class="m-result"><el-icon :size="22"><CircleCheckFilled /></el-icon>已认领，后续不再重复通知和升级</div>

      <div class="m-actions">
        <template v-if="info.action === 'ack' && a.status === 'FIRING'">
          <button class="m-btn primary" :disabled="acting" @click="doAck">{{ acting ? '处理中…' : '认领告警，我来处理' }}</button>
          <div class="m-who">认领人：{{ info.recipient || '邮件收件人' }}</div>
        </template>
        <div v-else-if="a.status !== 'FIRING' && !done" class="m-tip">该告警当前为“{{ ALARM_STATUS[a.status]?.label }}”，无需重复认领</div>
        <div v-else-if="info.action !== 'ack'" class="m-tip">“静默”操作需登录控制台填写原因后完成</div>
        <a class="m-btn ghost" :href="`/alarms?id=${a.id}`">在控制台查看详情</a>
      </div>
      <p class="m-foot">此链接只能用于处理本条告警</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { CircleCheckFilled, Loading, WarningFilled } from '@element-plus/icons-vue'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { mailActionDo, mailActionInfo } from '@/api'
import { ApiError } from '@/api/request'
import { useLatestRequest } from '@/composables/useLatestRequest'
import type { Alarm } from '@/types'
import { ALARM_STATUS, RESOURCE_TYPES, SERVICES, SEVERITY } from '@/utils/dict'
import { durationOf, fmtTime } from '@/utils/format'

const route = useRoute()
const info = ref<{ alarm: Alarm; action: string; recipient: string }>()
const infoRequest = useLatestRequest()
const ackRequest = useLatestRequest()
const loading = infoRequest.loading
const error = ref('')
const acting = ackRequest.loading
const done = ref(false)
const a = computed(() => info.value!.alarm)
const color = computed(() => (info.value ? (a.value.status === 'RESOLVED' ? '#2E7D32' : SEVERITY[a.value.severity].color) : '#1d63f0'))

watch(() => String(route.params.token), async token => {
  infoRequest.invalidate()
  ackRequest.invalidate()
  info.value = undefined
  done.value = false
  error.value = ''
  try { await infoRequest.run(() => mailActionInfo(token), value => { info.value = value }) } catch (e) {
    error.value = e instanceof ApiError ? e.message : '链接无效或已过期'
  }
}, { immediate: true, flush: 'sync' })
async function doAck() {
  if (acting.value || !info.value || info.value.action !== 'ack' || a.value.status !== 'FIRING') return
  const token = String(route.params.token)
  try {
    await ackRequest.run(() => mailActionDo(token), alarm => {
      info.value = { ...info.value!, alarm }
      done.value = true
    })
  } catch { /* 请求层显示失败原因，保留按钮以便重试。 */ }
}
</script>

<style scoped lang="scss">
.m-page { min-height: 100vh; max-width: 480px; margin: 0 auto; padding: 0 16px 32px; background: #f3f5f9; font-size: 15px; }
.m-bar { display: flex; align-items: center; gap: 8px; height: 52px; b { font-size: 16px; } span { margin-left: auto; font-size: 13px; color: var(--hi-text-3); } }
.m-card { position: relative; overflow: hidden; padding-top: 5px; border-radius: 12px; background: #fff; box-shadow: 0 4px 16px rgba(16, 24, 40, 0.08);
  &::before { content: ''; position: absolute; inset: 0 0 auto; height: 5px; background: var(--c); }
  .m-head { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px 0;
    .sev { padding: 3px 10px; border-radius: 12px; font-size: 13px; font-weight: 700; color: #fff; background: var(--c); }
    .st { font-size: 13px; color: var(--c); font-weight: 600; } }
  h2 { margin: 12px 16px 6px; font-size: 20px; line-height: 1.4; }
  .res { margin: 0 16px 14px; color: var(--hi-text-2); }
  .kv { display: grid; grid-template-columns: 1fr 1fr; margin: 0 16px; border-radius: 8px; background: color-mix(in srgb, var(--c) 6%, #fff); border: 1px solid color-mix(in srgb, var(--c) 18%, #fff);
    > div { padding: 10px 12px; display: flex; flex-direction: column; gap: 2px; }
    > div:nth-child(odd) { border-right: 1px solid color-mix(in srgb, var(--c) 14%, #fff); }
    > div:nth-child(-n + 2) { border-bottom: 1px solid color-mix(in srgb, var(--c) 14%, #fff); }
    span { font-size: 12px; color: var(--hi-text-3); } b { font-size: 17px; } }
  .rows { padding: 10px 16px 14px; font-size: 14px; > div { display: flex; padding: 6px 0; } span { width: 64px; flex: none; color: var(--hi-text-3); } em { font-style: normal; } } }
.m-result { display: flex; align-items: center; gap: 8px; margin-top: 14px; padding: 12px 14px; border-radius: 10px; color: var(--hi-ok); background: var(--hi-ok-bg); font-weight: 600; }
.m-actions { display: flex; flex-direction: column; gap: 12px; margin-top: 18px; }
.m-btn { display: block; width: 100%; height: 48px; line-height: 48px; border-radius: 10px; font-size: 16px; font-weight: 600; text-align: center; border: none; cursor: pointer;
  &.primary { color: #fff; background: linear-gradient(90deg, #1d63f0, #3a7af5); box-shadow: 0 6px 16px rgba(29, 99, 240, 0.3); &:disabled { opacity: 0.6; } }
  &.ghost { color: var(--el-color-primary); background: #fff; border: 1px solid var(--el-color-primary-light-5); } }
.m-who { margin-top: -4px; text-align: center; font-size: 13px; color: var(--hi-text-3); }
.m-tip { padding: 12px; text-align: center; border-radius: 10px; color: var(--hi-text-2); background: #fff; font-size: 14px; }
.m-foot { margin-top: 18px; text-align: center; font-size: 12px; color: var(--hi-text-3); }
.m-state { padding: 80px 12px; text-align: center; color: var(--hi-text-2);
  .icon { width: 64px; height: 64px; margin: 0 auto 12px; border-radius: 50%; display: grid; place-items: center; &.err { color: var(--hi-p2); background: var(--hi-p2-bg); } }
  h3 { margin: 0 0 8px; color: var(--hi-text-1); } p { line-height: 1.7; font-size: 14px; margin-bottom: 24px; } }
.spin { animation: spin 1s linear infinite; color: var(--el-color-primary); }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
