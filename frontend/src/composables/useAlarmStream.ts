import { onBeforeUnmount, onMounted, ref } from 'vue'
import { TENANT_KEY, TOKEN_KEY } from '@/api/request'
import type { AlarmPushEvent } from '@/types'

/**
 * 订阅告警实时推送（SSE）。EventSource 自带断线重连（服务端 retry: 5000）。
 * EventSource 不能设置请求头，Token 与当前租户走 Query；切换租户后调用 reopen()。
 */
export function useAlarmStream(onEvent: (e: AlarmPushEvent) => void) {
  const connected = ref(false)
  let es: EventSource | null = null

  function open() {
    const token = localStorage.getItem(TOKEN_KEY)
    if (!token || es) return
    const tenant = localStorage.getItem(TENANT_KEY)
    const qs = new URLSearchParams({ token, ...(tenant ? { tenant } : {}) })
    es = new EventSource(`/api/v1/stream/alarms?${qs}`)
    es.onopen = () => (connected.value = true)
    es.onerror = () => (connected.value = false)
    es.addEventListener('alarm', (m) => onEvent(JSON.parse((m as MessageEvent).data)))
  }
  function close() {
    es?.close()
    es = null
    connected.value = false
  }
  const reopen = () => { close(); open() }
  onMounted(open)
  onBeforeUnmount(close)
  return { connected, open, close, reopen }
}
