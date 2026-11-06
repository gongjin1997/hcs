<template>
  <el-dialog :model-value="visible" :title="route ? `编辑路由：${route.name}` : '新建通知路由'" width="760px" top="6vh" append-to-body destroy-on-close
             @close="emit('update:visible', false)">
    <el-form ref="formRef" :model="f" :rules="rules" label-width="100px" :disabled="saving" @submit.prevent>
      <el-form-item label="名称" prop="name"><el-input v-model="f.name" maxlength="64" show-word-limit placeholder="例如：租户B 告警 → 互联网运维组" /></el-form-item>
      <el-form-item label="优先级" prop="priority">
        <el-input-number v-model="f.priority" :min="0" :max="10000" :step="10" controls-position="right" style="width: 140px" />
        <span class="text-3 hint-inline">数字越小越先匹配；保存后也可以在列表中上移 / 下移</span>
      </el-form-item>

      <div class="block">
        <div class="block-title">匹配条件 <span class="text-3">（都不填 = 全部告警，适合做兜底路由；条件之间为“并且”）</span></div>
        <el-form-item v-if="app.tenants.length > 1" label="租户">
          <el-select v-model="m.tenant_ids" multiple clearable placeholder="不限" class="w-full">
            <el-option v-for="t in app.tenants" :key="t.id" :value="t.id" :label="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="服务">
          <el-select v-model="m.services" multiple clearable filterable placeholder="不限" class="w-full">
            <el-option v-for="(n, code) in SERVICES" :key="code" :value="code" :label="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="资源类型">
          <el-select v-model="m.resource_types" multiple clearable filterable placeholder="不限" class="w-full">
            <el-option v-for="(n, code) in RESOURCE_TYPES" :key="code" :value="code" :label="n" />
          </el-select>
        </el-form-item>
        <el-form-item label="级别">
          <el-checkbox-group v-model="m.severities">
            <el-checkbox v-for="s in SEVERITIES" :key="s" :value="s"><SeverityTag :level="s" short /></el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </div>

      <el-form-item label="通知联系组" prop="group_code">
        <el-select v-model="f.group_code" filterable placeholder="选择联系组" style="width: 320px">
          <el-option v-for="g in groups" :key="g.code" :value="g.code" :label="g.name">
            <span>{{ g.name }}</span><span class="text-3 opt">{{ g.members.length }} 人 · {{ g.code }}</span>
          </el-option>
        </el-select>
      </el-form-item>
      <el-form-item label="继续匹配">
        <el-switch v-model="f.continue_match" />
        <span class="text-3 hint-inline">命中后继续向下匹配（例如 P1 同时通知负责人和值班组）；关闭则命中即停止</span>
      </el-form-item>

      <div class="block">
        <div class="block-title">升级策略 <span class="text-3">（告警超过 N 分钟仍未认领时追加通知；认领后停止）</span></div>
        <div v-for="(e, i) in f.escalation" :key="i" class="esc-row">
          <span>超过</span>
          <el-input-number v-model="e.after_min" :min="5" :max="1440" :step="5" size="small" controls-position="right" style="width: 110px" />
          <span>分钟未认领，通知</span>
          <el-select v-model="e.to" size="small" style="width: 170px">
            <el-option value="leader" label="本组组长" />
            <el-option v-for="g in groups" :key="g.code" :value="g.code" :label="`联系组：${g.name}`" />
          </el-select>
          <span>级别</span>
          <el-select v-model="e.severities" multiple collapse-tags size="small" placeholder="全部级别" style="width: 150px">
            <el-option v-for="s in SEVERITIES" :key="s" :value="s" :label="s" />
          </el-select>
          <el-button link type="danger" :icon="Delete" @click="f.escalation.splice(i, 1)" />
        </div>
        <el-button size="small" :icon="Plus" :disabled="f.escalation.length >= 5" @click="addEsc">添加升级</el-button>
      </div>

      <el-form-item label="启用"><el-switch v-model="f.enabled" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { reactive, ref, watch } from 'vue'
import { type ContactGroup, type NotifyRoute, createRoute, updateRoute } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import { useAppStore } from '@/stores/app'
import type { Escalation, RouteIn } from '@/types'
import { RESOURCE_TYPES, SERVICES, SEVERITIES } from '@/utils/dict'

const props = defineProps<{ visible: boolean; route: NotifyRoute | null; groups: ContactGroup[]; nextPriority: number }>()
const emit = defineEmits<{ 'update:visible': [v: boolean]; saved: [] }>()
const app = useAppStore()
const formRef = ref<FormInstance>()
const saving = ref(false)
const f = reactive({ name: '', priority: 100, group_code: '', continue_match: false, enabled: true, escalation: [] as Escalation[] })
const m = reactive({ tenant_ids: [] as number[], services: [] as string[], resource_types: [] as string[], severities: [] as string[] })
const rules: FormRules = {
  name: [{ required: true, message: '请填写名称', trigger: 'blur' }],
  group_code: [{ required: true, message: '请选择联系组', trigger: 'change' }],
}

watch(() => props.visible, (v) => {
  if (!v) return
  const r = props.route
  Object.assign(f, {
    name: r?.name ?? '', priority: r?.priority ?? props.nextPriority, group_code: r?.group_code ?? '',
    continue_match: r?.continue_match ?? false, enabled: r?.enabled ?? true,
    escalation: (r?.escalation ?? []).map((e) => ({ after_min: e.after_min, to: e.to, severities: [...(e.severities ?? [])] })),
  })
  Object.assign(m, {
    tenant_ids: [...(r?.match.tenant_ids ?? [])], services: [...(r?.match.services ?? [])],
    resource_types: [...(r?.match.resource_types ?? [])], severities: [...(r?.match.severities ?? [])],
  })
}, { immediate: true })

const addEsc = () => f.escalation.push({ after_min: f.escalation.length ? f.escalation[f.escalation.length - 1].after_min + 15 : 15, to: 'leader', severities: ['P1', 'P2'] })

async function submit() {
  if (saving.value) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (saving.value) return
  const match: RouteIn['match'] = {}
  for (const k of ['tenant_ids', 'services', 'resource_types', 'severities'] as const) if (m[k].length) (match as Record<string, unknown>)[k] = [...m[k]]
  const body: RouteIn = { name: f.name.trim(), priority: f.priority, match, group_code: f.group_code, escalation: f.escalation, continue_match: f.continue_match, enabled: f.enabled }
  saving.value = true
  try {
    if (props.route) await updateRoute(props.route.id, body)
    else await createRoute(body)
    ElMessage.success('路由已保存')
    emit('saved')
    emit('update:visible', false)
  } catch { /* 请求层提示错误，保留表单供重试。 */ }
  finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.hint-inline { margin-left: 10px; font-size: 12px; }
.opt { margin-left: 8px; font-size: 12px; }
.block { margin: 4px 0 14px; padding: 12px 16px; border-radius: var(--hi-radius-sm); background: var(--hi-bg-soft); border: 1px solid var(--hi-border-light);
  .block-title { margin-bottom: 10px; font-weight: 600; font-size: 13px; .text-3 { font-weight: 400; } } }
.esc-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 13px; color: var(--hi-text-2); }
</style>
