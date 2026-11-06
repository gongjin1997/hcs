import { defineStore } from 'pinia'
import * as api from '@/api'
import { TENANT_KEY } from '@/api/request'
import type { AlarmStats, TenantBrief } from '@/types'
import { useUserStore } from './user'

export const useAppStore = defineStore('app', {
  state: () => ({
    collapsed: localStorage.getItem('hi_collapsed') === '1',
    /** 顶部租户切换：null = 全部租户（用户可见范围内）；切换后各页面按 tenantSeq 重新挂载 */
    tenantId: (Number(localStorage.getItem(TENANT_KEY)) || null) as number | null,
    tenantSeq: 0,
    stats: null as AlarmStats | null,
    statsRequestSeq: 0,
    streamConnected: false,
    eventSeq: 0, // 每收到一条 SSE 推送 +1，列表页 watch 它做增量刷新（避免每个页面各开一条 SSE 连接）
  }),
  getters: {
    /** 当前用户可切换的租户 */
    tenants: (): TenantBrief[] => useUserStore().user?.tenants ?? [],
    currentTenant(): TenantBrief | null { return this.tenants.find((t) => t.id === this.tenantId) ?? null },
    tenantLabel(): string {
      if (this.currentTenant) return this.currentTenant.name
      return this.tenants.length === 1 ? this.tenants[0].name : '全部租户'
    },
    /** 列表是否需要展示“租户”列：可见多个租户且未选定某个租户 */
    multiTenant(): boolean { return !this.currentTenant && this.tenants.length > 1 },
  },
  actions: {
    toggle() {
      this.collapsed = !this.collapsed
      localStorage.setItem('hi_collapsed', this.collapsed ? '1' : '0')
    },
    setTenant(id: number | null) {
      if (id === this.tenantId) return
      if (id !== null && !this.tenants.some(t => t.id === id)) return
      this.tenantId = id
      if (id) localStorage.setItem(TENANT_KEY, String(id))
      else localStorage.removeItem(TENANT_KEY)
      this.invalidateStats()
      this.streamConnected = false
      this.tenantSeq++
      void this.refreshStats()
    },
    /** 登录 / 拉取用户后校验：已选租户不在范围内则回到全部 */
    ensureTenant() {
      if (this.tenantId && !this.tenants.some((t) => t.id === this.tenantId)) {
        this.setTenant(null)
      }
    },
    invalidateStats() {
      this.statsRequestSeq++
      this.stats = null
    },
    resetSession() {
      this.invalidateStats()
      this.tenantId = null
      this.streamConnected = false
      this.eventSeq = 0
      this.tenantSeq++
      localStorage.removeItem(TENANT_KEY)
    },
    tenantOf(id?: number | null): TenantBrief | undefined {
      return id == null ? undefined : this.tenants.find((t) => t.id === id)
    },
    async refreshStats() {
      const user = useUserStore()
      const current = ++this.statsRequestSeq
      const scope = this.tenantSeq
      const token = user.token
      if (!user.can('alarm:view')) { this.stats = null; return }
      try {
        const stats = await api.alarmStats()
        if (current === this.statsRequestSeq && scope === this.tenantSeq && token === user.token) this.stats = stats
      } catch { /* 自动刷新错误由请求层提示；下次刷新可重试。 */ }
    },
  },
})
