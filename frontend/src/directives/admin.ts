import type { Directive } from 'vue'
import { useUserStore } from '@/stores/user'

/**
 * v-perm="'alarm:handle'" / v-perm="['ledger:edit', 'ledger:sync']"（满足任一即可）：无权限时移除元素。
 * 只用于隐藏按钮，接口层 require_perm 仍会再次校验。
 */
export const vPerm: Directive<HTMLElement, string | string[]> = {
  mounted(el, binding) {
    const perms = Array.isArray(binding.value) ? binding.value : [binding.value]
    if (!useUserStore().canAny(...perms)) el.parentNode?.removeChild(el)
  },
}

/** v-admin：兼容旧写法，等价于 v-perm="'tenant:manage'"（平台管理员） */
export const vAdmin: Directive<HTMLElement> = {
  mounted(el) {
    if (!useUserStore().can('tenant:manage')) el.parentNode?.removeChild(el)
  },
}
