import { defineStore } from 'pinia'
import * as api from '@/api'
import { TENANT_KEY, TOKEN_KEY } from '@/api/request'
import type { UserInfo } from '@/types'
import { useAppStore } from './app'

/** 权限点（与后端 app/core/permissions.py 一致）：模块:动作，如 alarm:handle、tenant:manage */
export type Perm = string

export const useUserStore = defineStore('user', {
  state: () => ({ token: localStorage.getItem(TOKEN_KEY) || '', user: null as UserInfo | null, identityRequestSeq: 0 }),
  getters: {
    displayName: (s) => s.user?.display_name || s.user?.username || '',
    roleName: (s) => s.user?.role_name || '',
    /** 平台管理员（role_code = admin）；页面按钮请优先用 can() 按权限点判断 */
    isAdmin: (s) => !!s.user?.is_admin,
    permSet: (s) => new Set(s.user?.permissions ?? []),
    /** 租户范围文字：全部租户 / 租户A、租户B */
    scopeText: (s) => (s.user?.tenant_ids == null ? '全部租户' : (s.user?.tenants ?? []).map((t) => t.short_name).join('、') || '无'),
  },
  actions: {
    can(...perms: Perm[]) {
      return perms.every((p) => this.permSet.has(p))
    },
    canAny(...perms: Perm[]) {
      return perms.some((p) => this.permSet.has(p))
    },
    async login(username: string, password: string, remember = false) {
      const current = ++this.identityRequestSeq
      const data = await api.login(username, password, remember)
      if (current !== this.identityRequestSeq) return
      this.token = data.access_token
      this.user = data.user
      localStorage.setItem(TOKEN_KEY, data.access_token)
      const app = useAppStore()
      app.invalidateStats()
      app.ensureTenant()
    },
    async fetchMe() {
      const current = ++this.identityRequestSeq
      const token = this.token
      const user = await api.fetchMe()
      if (current !== this.identityRequestSeq || token !== this.token) return
      this.user = user
      useAppStore().ensureTenant()
    },
    clearSession() {
      this.identityRequestSeq++
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(TENANT_KEY)
      useAppStore().resetSession()
    },
    async logout() {
      try { await api.logout() } finally { this.clearSession() }
    },
  },
})
