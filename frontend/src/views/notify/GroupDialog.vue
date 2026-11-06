<template>
  <el-dialog :model-value="visible" :title="group ? `编辑联系组：${group.name}` : '新建联系组'" width="620px" append-to-body destroy-on-close
             @close="emit('update:visible', false)">
    <el-form ref="formRef" :model="f" :rules="rules" label-width="100px" :disabled="saving" @submit.prevent>
      <el-form-item label="代码" prop="code">
        <el-input v-model="f.code" :disabled="!!group" placeholder="小写字母开头，例如 ops-db" style="width: 260px" class="mono" />
        <span class="text-3 hint-inline">{{ group ? '创建后不可修改，路由按代码引用' : '创建后不能修改' }}</span>
      </el-form-item>
      <el-form-item label="名称" prop="name"><el-input v-model="f.name" maxlength="64" placeholder="例如：DBA 组" /></el-form-item>
      <el-form-item label="成员" prop="member_ids">
        <el-select v-model="f.member_ids" multiple filterable placeholder="选择已登记人员（通知发给全部成员）" class="w-full">
          <el-option v-for="c in contacts" :key="c.id" :value="c.id" :label="contactLabel(c)" :disabled="!c.enabled">
            <span>{{ contactLabel(c) }}</span><span class="text-3 opt">{{ c.email || c.phone || '未填写联系方式' }}{{ c.enabled ? '' : '（不可接收通知）' }}</span>
          </el-option>
        </el-select>
      </el-form-item>
      <el-form-item label="组长">
        <el-select v-model="f.leader_ids" multiple filterable placeholder="升级策略“本组组长”通知的人" class="w-full">
          <el-option v-for="c in contacts" :key="c.id" :value="c.id" :label="contactLabel(c)" :disabled="!c.enabled">
            <span>{{ contactLabel(c) }}</span><span class="text-3 opt">{{ c.email || c.phone || '未填写联系方式' }}{{ c.enabled ? '' : '（不可接收通知）' }}</span>
          </el-option>
        </el-select>
      </el-form-item>
      <div class="text-3 tip">组长可以不是成员，仅接收升级通知。人员来自“系统管理 → 用户管理”，请在那里维护姓名、联系方式和通知开关。</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { reactive, ref, watch } from 'vue'
import { type ContactGroup, createGroup, updateGroup } from '@/api'
import type { Contact } from '@/types'

const props = defineProps<{ visible: boolean; group: ContactGroup | null; contacts: Contact[] }>()
const emit = defineEmits<{ 'update:visible': [v: boolean]; saved: [] }>()
const formRef = ref<FormInstance>()
const saving = ref(false)
const contactLabel = (contact: Contact) => contact.username ? `${contact.name}（${contact.username}）` : contact.name
const f = reactive({ code: '', name: '', member_ids: [] as number[], leader_ids: [] as number[] })
const rules: FormRules = {
  code: [{ required: true, pattern: /^[a-z][a-z0-9-]{1,31}$/, message: '小写字母开头，2–32 位小写字母 / 数字 / 连字符', trigger: 'blur' }],
  name: [{ required: true, message: '请填写名称', trigger: 'blur' }],
  member_ids: [{ type: 'array', required: true, min: 1, message: '至少选择 1 个成员', trigger: 'change' }],
}
watch(() => props.visible, (v) => {
  if (!v) return
  const g = props.group
  Object.assign(f, { code: g?.code ?? '', name: g?.name ?? '', member_ids: [...(g?.member_ids ?? [])], leader_ids: [...(g?.leader_ids ?? [])] })
}, { immediate: true })

async function submit() {
  if (saving.value) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (saving.value) return
  saving.value = true
  try {
    const body = { code: f.code.trim(), name: f.name.trim(), member_ids: f.member_ids, leader_ids: f.leader_ids }
    if (props.group) await updateGroup(props.group.id, body)
    else await createGroup(body)
    ElMessage.success('已保存')
    emit('saved')
    emit('update:visible', false)
  } catch { /* 请求层提示错误，保留表单供重试。 */ }
  finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.hint-inline { margin-left: 10px; font-size: 12px; }
.opt { margin-left: 8px; font-size: 12px; }
.tip { margin-left: 100px; font-size: 12px; }
</style>
