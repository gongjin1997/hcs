<template>
  <div class="recipient-select">
    <el-select v-model="selection" multiple filterable class="w-full" :loading="loading" :disabled="disabled || loading" placeholder="选择人员">
      <el-option v-for="row in rows" :key="row.id" :value="row.id" :label="`${row.label}${row.email ? ` <${row.email}>` : ''}${row.reason ? ` · ${row.reason}` : ''}`"
                 :disabled="!!row.reason && !modelValue.includes(row.id)">
        <span>{{ row.label }}</span><span class="text-3 option-email">{{ row.email || '缺少邮箱' }}{{ row.reason ? ` · ${row.reason}` : '' }}</span>
      </el-option>
    </el-select>
    <div v-if="failed" class="text-danger hint">人员选项加载失败。<el-button link type="primary" :disabled="disabled || loading" @click="load">重试</el-button></div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { listUserOptions } from '@/api'
import { useLatestRequest } from '@/composables/useLatestRequest'
import type { UserOption } from '@/types'
import { recipientRows } from '@/utils/recipients'

const props = withDefaults(defineProps<{ modelValue: number[]; tenantId?: number | null; globalOnly?: boolean; disabled?: boolean; active?: boolean }>(), {
  tenantId: null, globalOnly: false, disabled: false, active: true,
})
const emit = defineEmits<{ 'update:modelValue': [value: number[]] }>()
const users = ref<UserOption[]>([])
const failed = ref(false)
const request = useLatestRequest()
const loading = request.loading
const rows = computed(() => recipientRows(users.value, props.modelValue, props.tenantId, props.globalOnly))
const selection = computed({
  get: () => props.modelValue,
  set: (ids: number[]) => emit('update:modelValue', [...new Set(ids)].filter(id => props.modelValue.includes(id) || rows.value.some(row => row.id === id && !row.reason))),
})
async function load() {
  if (!props.active || loading.value) return
  failed.value = false
  await request.run(async () => {
    try { return { users: await listUserOptions(props.tenantId, undefined, true), failed: false } }
    catch { return { users: [] as UserOption[], failed: true } }
  }, result => { users.value = result.users; failed.value = result.failed })
}
watch(() => [props.active, props.tenantId, props.globalOnly], () => {
  request.invalidate()
  users.value = []
  failed.value = false
  void load()
}, { immediate: true })
</script>

<style scoped lang="scss">
.recipient-select, .w-full { width: 100%; }
.option-email { margin-left: 8px; font-size: 12px; }
.hint { font-size: 12px; margin-top: 4px; }
</style>
