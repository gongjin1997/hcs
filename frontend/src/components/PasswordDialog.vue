<template>
  <el-dialog :model-value="modelValue" title="修改密码" width="420px" append-to-body @update:model-value="$emit('update:modelValue', $event)" @closed="reset">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="84px" @keyup.enter="submit">
      <el-form-item label="当前密码" prop="old"><el-input v-model="form.old" type="password" show-password autocomplete="current-password" /></el-form-item>
      <el-form-item label="新密码" prop="pwd"><el-input v-model="form.pwd" type="password" show-password autocomplete="new-password" placeholder="至少 8 位" /></el-form-item>
      <el-form-item label="确认新密码" prop="pwd2"><el-input v-model="form.pwd2" type="password" show-password autocomplete="new-password" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { reactive, ref } from 'vue'
import { changePassword } from '@/api'
import { useUserStore } from '@/stores/user'
import { useRouter } from 'vue-router'

const user = useUserStore()
const router = useRouter()

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const formRef = ref<FormInstance>()
const saving = ref(false)
const form = reactive({ old: '', pwd: '', pwd2: '' })
const rules: FormRules = {
  old: [{ required: true, message: '请输入当前密码' }],
  pwd: [{ required: true, message: '请输入新密码' }, { min: 8, max: 64, message: '长度 8~64 位' }],
  pwd2: [{ validator: (_r, v, cb) => (v === form.pwd ? cb() : cb(new Error('两次输入不一致'))) }],
}
const reset = () => { Object.assign(form, { old: '', pwd: '', pwd2: '' }); formRef.value?.clearValidate() }
async function submit() {
  if (saving.value) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (saving.value) return
  saving.value = true
  try {
    await changePassword(form.old, form.pwd)
    user.clearSession()
    ElMessage.success('密码已修改，请重新登录')
    emit('update:modelValue', false)
    await router.replace('/login')
  } finally { saving.value = false }
}
</script>
