import { shallowRef, watchEffect, type Directive, type ShallowRef, type WatchStopHandle } from 'vue'
import { useUserStore } from '@/stores/user'

/**
 * v-perm="'alarm:handle'" / v-perm="['ledger:edit', 'ledger:sync']"（满足任一即可）：无权限时隐藏元素。
 * 只用于隐藏按钮，接口层 require_perm 仍会再次校验。
 */
type Permissions = string | string[]
const bindings = new WeakMap<HTMLElement, { perms: ShallowRef<Permissions>; stop: WatchStopHandle }>()
function mountPermission(el: HTMLElement, value: Permissions) {
  const display = el.style.display
  const perms = shallowRef(value)
  const stop = watchEffect(() => {
    const values = Array.isArray(perms.value) ? perms.value : [perms.value]
    el.style.display = useUserStore().canAny(...values) ? display : 'none'
  }, { flush: 'sync' })
  bindings.set(el, { perms, stop })
}

export const vPerm: Directive<HTMLElement, Permissions> = {
  mounted(el, binding) {
    mountPermission(el, binding.value)
  },
  updated(el, binding) { const entry = bindings.get(el); if (entry) entry.perms.value = binding.value },
  beforeUnmount(el) { bindings.get(el)?.stop(); bindings.delete(el) },
}

/** v-admin：兼容旧写法，等价于 v-perm="'tenant:manage'"（平台管理员） */
export const vAdmin: Directive<HTMLElement> = {
  mounted(el) {
    mountPermission(el, 'tenant:manage')
  },
  beforeUnmount(el) { bindings.get(el)?.stop(); bindings.delete(el) },
}
