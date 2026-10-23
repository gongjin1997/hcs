<template>
  <div class="contact-panel">
    <div class="table-toolbar">
      <el-input v-model="keyword" clearable placeholder="姓名 / 邮箱 / 电话" :prefix-icon="Search" style="width: 260px" />
      <el-button v-if="canManage" type="primary" :icon="Plus" @click="open(null)">新建联系人</el-button>
    </div>
    <el-table :data="rows" size="default">
      <el-table-column label="姓名" width="140"><template #default="{ row }"><span class="avatar">{{ row.name.slice(0, 1) }}</span><b>{{ row.name }}</b></template></el-table-column>
      <el-table-column label="邮箱" min-width="220"><template #default="{ row }"><span class="mono">{{ row.email }}</span></template></el-table-column>
      <el-table-column label="电话" width="140"><template #default="{ row }"><span class="mono">{{ row.phone || '-' }}</span></template></el-table-column>
      <el-table-column label="所属联系组" min-width="300">
        <template #default="{ row }">
          <div class="groups">
            <el-tag v-for="g in row.groups" :key="g.code" size="small" :type="g.leader ? 'warning' : 'primary'" effect="plain">{{ g.name }}{{ g.leader ? ' · 组长' : '' }}</el-tag>
            <span v-if="!row.groups.length" class="text-3">未加入任何联系组（不会收到通知）</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '接收通知' : '已停用' }}</el-tag></template></el-table-column>
      <el-table-column v-if="canManage" label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="open(row as Contact)">编辑</el-button>
          <el-button link type="danger" @click="remove(row as Contact)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div class="text-3 tip">停用的联系人保留在联系组中但不再接收通知（例如休假）；删除会同时把他从所有联系组移除。</div>

    <el-dialog v-model="dlg.visible" :title="dlg.contact ? `编辑联系人：${dlg.contact.name}` : '新建联系人'" width="480px" append-to-body>
      <el-form ref="formRef" :model="f" :rules="rules" label-width="80px">
        <el-form-item label="姓名" prop="name"><el-input v-model="f.name" maxlength="64" /></el-form-item>
        <el-form-item label="邮箱" prop="email"><el-input v-model="f.email" maxlength="128" placeholder="name@example.com" /></el-form-item>
        <el-form-item label="电话"><el-input v-model="f.phone" maxlength="32" placeholder="可选，邮件里显示，便于电话升级" /></el-form-item>
        <el-form-item label="接收通知"><el-switch v-model="f.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Plus, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { createContact, deleteContact, updateContact } from '@/api'
import type { Contact } from '@/types'

const props = defineProps<{ contacts: Contact[]; canManage: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const keyword = ref('')
const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return kw ? props.contacts.filter((c) => `${c.name} ${c.email} ${c.phone ?? ''}`.toLowerCase().includes(kw)) : props.contacts
})
const formRef = ref<FormInstance>()
const dlg = reactive({ visible: false, contact: null as Contact | null, saving: false })
const f = reactive({ name: '', email: '', phone: '', enabled: true })
const rules: FormRules = {
  name: [{ required: true, message: '请填写姓名', trigger: 'blur' }],
  email: [{ required: true, type: 'email', message: '请填写正确的邮箱', trigger: 'blur' }],
}
function open(c: Contact | null) {
  Object.assign(f, { name: c?.name ?? '', email: c?.email ?? '', phone: c?.phone ?? '', enabled: c?.enabled ?? true })
  Object.assign(dlg, { visible: true, contact: c })
}
async function submit() {
  await formRef.value?.validate()
  dlg.saving = true
  try {
    const body = { name: f.name.trim(), email: f.email.trim(), phone: f.phone.trim() || null, enabled: f.enabled }
    if (dlg.contact) await updateContact(dlg.contact.id, body)
    else await createContact(body)
    ElMessage.success('已保存')
    dlg.visible = false
    emit('changed')
  } finally { dlg.saving = false }
}
async function remove(c: Contact) {
  const tip = c.groups.length ? `将从 ${c.groups.map((g) => g.name).join('、')} 中移除。` : ''
  await ElMessageBox.confirm(`${tip}确认删除联系人“${c.name}”？`, '删除联系人', { type: 'warning' })
  const r = await deleteContact(c.id)
  ElMessage.success(r.removed_from?.length ? `已删除，并从 ${r.removed_from.join('、')} 移除` : '已删除')
  emit('changed')
}
</script>

<style scoped lang="scss">
.avatar { display: inline-grid; place-items: center; width: 26px; height: 26px; margin-right: 8px; border-radius: 50%; color: #fff; font-size: 12px;
  background: linear-gradient(135deg, #1d63f0, #5b8ff7); vertical-align: middle; }
.groups { display: flex; flex-wrap: wrap; gap: 6px; }
.tip { margin-top: 10px; font-size: 12px; }
</style>
