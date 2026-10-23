import { onBeforeUnmount, onMounted, ref } from 'vue'
import { streamTicket } from '@/api'
import { TENANT_KEY, TOKEN_KEY } from '@/api/request'
import type { AlarmPushEvent } from '@/types'

/**
 * 订阅告警实时推送（SSE）。
 * EventSource 不能设置请求头：先用登录令牌换一张 60 秒有效、只能用一次的票据（POST /stream/ticket），再用票据建立连接，
 * 登录令牌不会出现在 URL / Nginx 访问日志里。票据只能用一次，所以断线后不依赖 EventSource 自带的重连，
 * 而是关闭连接、退避（5s → 10s → … 最长 60s）后重新换票据再连；切换租户后调用 reopen()。
 */
export function useAlarmStream(onEvent: (e: AlarmPushEvent) => void) {
  const connected = ref(false)
  let es: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let retry = 0
  let stopped = false

  async function open() {
    if (es || stopped || !localStorage.getItem(TOKEN_KEY)) return
    let ticket: string
    try {
      ticket = (await streamTicket()).ticket
    } catch {
      return schedule()                       // 未登录 / 无权限 / 网络异常：稍后再试（401 时请求层会跳转登录）
    }
    if (stopped || es) return
    const tenant = localStorage.getItem(TENANT_KEY)
    const qs = new URLSearchParams({ ticket, ...(tenant ? { tenant } : {}) })
    const source = new EventSource(`/api/v1/stream/alarms?${qs}`)
    es = source
    source.onopen = () => { connected.value = true; retry = 0 }
    source.onerror = () => {                  // 票据已用过，不能让 EventSource 用同一个 URL 重连
      if (es !== source) return
      source.close()
      es = null
      connected.value = false
      schedule()
    }
    source.addEventListener('alarm', (m) => onEvent(JSON.parse((m as MessageEvent).data)))
  }
  function schedule() {
    if (stopped || timer) return
    const delay = Math.min(60, 5 * 2 ** retry) * 1000
    retry = Math.min(retry + 1, 4)
    timer = setTimeout(() => { timer = null; open() }, delay)
  }
  function close() {
    if (timer) { clearTimeout(timer); timer = null }
    es?.close()
    es = null
    connected.value = false
  }
  const reopen = () => { close(); retry = 0; open() }
  onMounted(() => { stopped = false; open() })
  onBeforeUnmount(() => { stopped = true; close() })
  return { connected, open, close, reopen }
}
