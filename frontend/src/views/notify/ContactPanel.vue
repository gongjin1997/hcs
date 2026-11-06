<template>
  <div class="contact-panel">
    <div class="table-heading">
      <div class="text-3">联系人来自用户管理，本页只读。</div>
      <div v-if="canManageUsers" class="table-actions"><el-button type="primary" @click="router.push('/system?tab=users')">前往用户管理</el-button></div>
    </div>
    <div class="table-toolbar">
      <el-input v-model="keyword" clearable placeholder="姓名 / 账号 / 邮箱 / 电话" :prefix-icon="Search" style="width: 260px" />
    </div>
    <el-table :data="rows" size="default">
      <el-table-column label="姓名 / 账号" min-width="180"><template #default="{ row }"><span class="avatar">{{ row.name.slice(0, 1) }}</span><b>{{ row.name }}</b><div class="text-3 mono">{{ row.username || '-' }}</div></template></el-table-column>
      <el-table-column label="邮箱" min-width="220"><template #default="{ row }"><span class="mono">{{ row.email || '-' }}</span></template></el-table-column>
      <el-table-column label="电话" width="140"><template #default="{ row }"><span class="mono">{{ row.phone || '-' }}</span></template></el-table-column>
      <el-table-column label="所属联系组" min-width="300">
        <template #default="{ row }">
          <div class="groups">
            <el-tag v-for="g in row.groups" :key="g.code" size="small" :type="g.leader ? 'warning' : 'primary'" effect="plain">{{ g.name }}{{ g.leader ? ' · 组长' : '' }}</el-tag>
            <span v-if="!row.groups.length" class="text-3">未加入任何联系组（不会收到通知）</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="通知状态" width="110"><template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? (row.email ? '可接收通知' : '仅手机号') : '不可接收' }}</el-tag></template></el-table-column>
    </el-table>
    <div class="text-3 tip">邮箱用于邮件通知，手机号用于升级短信；仍须启用人员、通知开关及对应通知配置。停用保留联系组成员关系，人员信息在用户管理维护。</div>
  </div>
</template>

<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import type { Contact } from '@/types'

const props = defineProps<{ contacts: Contact[] }>()
const router = useRouter()
const user = useUserStore()
const canManageUsers = computed(() => !!user.user && user.user.tenant_ids == null && user.can('user:manage'))
const keyword = ref('')
const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return kw ? props.contacts.filter((c) => `${c.name} ${c.username ?? ''} ${c.email ?? ''} ${c.phone ?? ''}`.toLowerCase().includes(kw)) : props.contacts
})
</script>

<style scoped lang="scss">
.avatar { display: inline-grid; place-items: center; width: 26px; height: 26px; margin-right: 8px; border-radius: 50%; color: #fff; font-size: 12px;
  background: linear-gradient(135deg, #1d63f0, #5b8ff7); vertical-align: middle; }
.groups { display: flex; flex-wrap: wrap; gap: 6px; }
.tip { margin-top: 10px; font-size: 12px; }
</style>
