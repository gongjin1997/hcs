<template>
  <el-dialog :model-value="visible" title="分类映射" width="min(1100px, calc(100vw - 24px))" top="5vh" append-to-body
             :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @close="emit('update:visible', false)">
    <div class="table-heading mb-12">
      <span class="text-3">从上到下首次命中生效；匹配文字按包含判断，不区分大小写。未匹配归为“{{ fallback }}”。</span>
      <div class="table-actions"><el-button v-if="editable" :icon="Plus" :disabled="loading || failed || saving || rules.length >= 200" @click="add">添加规则</el-button></div>
    </div>
    <el-alert v-if="failed" type="error" :closable="false" title="分类映射加载失败" class="mb-12"><el-button link type="primary" @click="load">重试</el-button></el-alert>
    <el-table v-loading="loading" :data="rules" row-key="id" max-height="440">
      <el-table-column label="资源类型" min-width="170"><template #default="{ row }">
        <el-select v-model="row.resource_type" filterable :disabled="!editable || saving">
          <el-option v-for="type in typeOptions" :key="type.code" :value="type.code" :label="type.name" />
        </el-select>
      </template></el-table-column>
      <el-table-column label="字段" min-width="125"><template #default="{ row }">
        <el-select v-model="row.field" :disabled="!editable || saving"><el-option v-for="field in FIELDS" :key="field.value" :value="field.value" :label="field.label" /></el-select>
      </template></el-table-column>
      <el-table-column label="匹配文字" min-width="180"><template #default="{ row }"><el-input v-model="row.pattern" maxlength="128" :disabled="!editable || saving" /></template></el-table-column>
      <el-table-column label="分类" min-width="130"><template #default="{ row }"><el-input v-model="row.category" maxlength="64" :disabled="!editable || saving" /></template></el-table-column>
      <el-table-column label="启用" width="72"><template #default="{ row }"><el-switch v-model="row.enabled" :disabled="!editable || saving" /></template></el-table-column>
      <el-table-column v-if="editable" label="顺序 / 操作" min-width="185"><template #default="{ row, $index }">
        <el-button link type="primary" :disabled="saving || $index === 0" @click="move($index, -1)">上移</el-button>
        <el-button link type="primary" :disabled="saving || $index === rules.length - 1" @click="move($index, 1)">下移</el-button>
        <el-button link type="danger" :disabled="saving" @click="remove(row.id)">删除</el-button>
      </template></el-table-column>
    </el-table>
    <div class="text-3 hint">{{ rules.length }} / 200 条；分类仅用于台账展示、筛选和导出。</div>
    <template #footer>
      <span v-if="!editable" class="text-3">修改需要全部租户范围的“编辑台账”权限。</span>
      <el-button :disabled="saving" @click="emit('update:visible', false)">{{ editable ? '取消' : '关闭' }}</el-button>
      <el-button v-if="editable" type="primary" :loading="saving" :disabled="loading || failed" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { getClassifications, saveClassifications } from '@/api'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useUserStore } from '@/stores/user'
import type { ClassificationRule } from '@/types'
import { typeName } from '@/utils/dict'

const props = defineProps<{ visible: boolean; resourceTypes: { code: string; name: string }[] }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; saved: [] }>()
const FIELDS = [{ value: 'os', label: '操作系统' }, { value: 'name', label: '名称' }, { value: 'business', label: '业务系统' }, { value: 'spec', label: '规格' }]
const user = useUserStore()
const editable = computed(() => !!user.user && user.user.tenant_ids == null && user.can('ledger:edit'))
const rules = ref<ClassificationRule[]>([])
const fallback = ref('其他')
const saving = ref(false)
const failed = ref(false)
const request = useLatestRequest()
const loading = request.loading
const typeOptions = computed(() => {
  const types = [...props.resourceTypes]
  for (const rule of rules.value) if (!types.some(type => type.code === rule.resource_type)) types.push({ code: rule.resource_type, name: typeName(rule.resource_type) })
  return types
})
async function load() {
  if (!props.visible || loading.value) return
  failed.value = false
  await request.run(async () => {
    try { return await getClassifications() }
    catch { return null }
  }, config => {
    failed.value = !config
    if (config) { rules.value = config.rules.map(rule => ({ ...rule })); fallback.value = config.fallback }
  })
}
watch(() => props.visible, visible => { request.invalidate(); if (visible) void load() }, { immediate: true })
function add() {
  if (!editable.value || loading.value || failed.value || saving.value || rules.value.length >= 200) return
  const id = globalThis.crypto?.randomUUID?.() ?? `rule-${Date.now()}-${Math.random().toString(36).slice(2)}`
  rules.value.push({ id, resource_type: props.resourceTypes.find(type => type.code === 'ecs_server')?.code ?? props.resourceTypes[0]?.code ?? 'ecs_server', field: 'os', pattern: '', category: '', enabled: true })
}
function move(index: number, offset: number) {
  if (!editable.value || saving.value || index + offset < 0 || index + offset >= rules.value.length) return
  const [rule] = rules.value.splice(index, 1)
  rules.value.splice(index + offset, 0, rule)
}
function remove(id: string) { if (editable.value && !saving.value) rules.value = rules.value.filter(rule => rule.id !== id) }
async function save() {
  if (!editable.value || saving.value || loading.value || failed.value) return
  if (rules.value.length > 200) return ElMessage.warning('最多 200 条规则')
  if (rules.value.some(rule => !rule.resource_type || !rule.pattern.trim() || !rule.category.trim())) return ElMessage.warning('请填写资源类型、匹配文字和分类')
  saving.value = true
  try {
    await saveClassifications({ rules: rules.value.map(rule => ({ ...rule, pattern: rule.pattern.trim(), category: rule.category.trim() })), fallback: fallback.value })
    ElMessage.success('分类映射已保存')
    emit('saved')
    emit('update:visible', false)
  } catch { /* 请求层提示错误，保留草稿。 */ }
  finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.el-select { width: 100%; }
.hint { font-size: 12px; margin-top: 8px; }
</style>
