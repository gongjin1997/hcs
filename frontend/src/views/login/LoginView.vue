<template>
  <!-- 运维组内部使用：账号 + 密码即可登录，不接 SSO、不做验证码；账号由管理员在「系统管理 → 用户管理」开通 -->
  <div class="login">
    <div class="bg" />
    <div class="card">
      <div class="brand">
        <svg viewBox="0 0 32 32" width="40" height="40"><rect width="32" height="32" rx="9" fill="#1d63f0" />
          <path d="M16 6l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V9z" fill="none" stroke="#fff" stroke-width="2" />
          <path d="M12 16l3 3 5-6" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        <div><h1>HCS 云服务巡检平台</h1><p>多租户巡检告警 · 容器事件 · 资源台账 · 运维报表</p></div>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" size="large" class="form" @keyup.enter="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="账号" :prefix-icon="User" autocomplete="username" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" show-password :prefix-icon="Lock" autocomplete="current-password" />
        </el-form-item>
        <div class="row">
          <el-checkbox v-model="form.remember">7 天内免登录</el-checkbox>
          <el-tooltip content="请联系平台管理员在「系统管理 → 用户管理」重置" placement="top"><span class="forgot">忘记密码？</span></el-tooltip>
        </div>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="err" />
        <el-button type="primary" class="submit" :loading="loading" @click="submit">登录</el-button>
      </el-form>

      <div v-if="showDemo" class="demo">
        <span class="k">演示账号（点击填入）</span>
        <span v-for="a in DEMO" :key="a.u" class="acc" @click="fillDemo(a)"><b>{{ a.u }} / {{ a.p }}</b><em>{{ a.role }}</em></span>
      </div>
      <div class="foot">运维组内部使用 · 账号由管理员开通</div>
    </div>
    <footer>© 2026 HCS-Inspector · v0.3.0 · 适配 HCS 8.5.1</footer>
  </div>
</template>

<script setup lang="ts">
import { Lock, User } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const LAST_USER = 'hi_last_user'
const showDemo = import.meta.env.DEV || ['localhost', '127.0.0.1'].includes(location.hostname)
const form = reactive({ username: localStorage.getItem(LAST_USER) || '', password: '', remember: true })
// 演示环境点击即可填入（生产构建且非 localhost 时不显示）
const DEMO = [{ u: 'admin', p: 'admin123', role: '平台管理员' }, { u: 'oncall', p: 'oncall123', role: '值班运维' }]
const fillDemo = (a: { u: string; p: string }) => Object.assign(form, { username: a.u, password: a.p })
const rules: FormRules = { username: [{ required: true, message: '请输入账号' }], password: [{ required: true, message: '请输入密码' }] }
const formRef = ref<FormInstance>()
const loading = ref(false)
const error = ref('')
const user = useUserStore()
const route = useRoute()
const router = useRouter()

async function submit() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  loading.value = true
  error.value = ''
  try {
    await user.login(form.username.trim(), form.password, form.remember)
    localStorage.setItem(LAST_USER, form.username.trim())
    router.replace((route.query.redirect as string) || '/dashboard')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
.login { position: relative; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #f4f6fa; overflow: hidden; }
.bg { position: absolute; inset: 0; pointer-events: none;
  background: radial-gradient(900px 480px at 12% -10%, rgba(29, 99, 240, 0.16), transparent 60%),
              radial-gradient(700px 420px at 100% 110%, rgba(91, 143, 247, 0.16), transparent 60%);
  &::after { content: ''; position: absolute; inset: 0; opacity: 0.5;
    background-image: linear-gradient(rgba(29, 99, 240, 0.06) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 99, 240, 0.06) 1px, transparent 1px);
    background-size: 36px 36px; mask-image: radial-gradient(circle at 50% 45%, #000 0%, transparent 70%); } }
.card { position: relative; width: 400px; padding: 36px 36px 22px; background: #fff; border: 1px solid var(--hi-border); border-radius: 14px;
  box-shadow: 0 16px 48px rgba(16, 24, 40, 0.08); }
.brand { display: flex; align-items: center; gap: 14px; margin-bottom: 28px;
  h1 { margin: 0; font-size: 20px; font-weight: 700; color: var(--hi-text-1); letter-spacing: 0.5px; }
  p { margin: 4px 0 0; font-size: 13px; color: var(--hi-text-3); letter-spacing: 1px; } }
.form { .row { display: flex; justify-content: space-between; align-items: center; margin: -6px 0 18px; }
  .forgot { font-size: 13px; color: var(--hi-text-3); cursor: help; &:hover { color: var(--el-color-primary); } }
  .err { margin-bottom: 14px; }
  .submit { width: 100%; height: 42px; font-size: 15px; letter-spacing: 2px; } }
.demo { margin-top: 18px; padding: 8px 12px; font-size: 12px; color: var(--hi-text-2); background: var(--hi-bg-soft); border-radius: 6px;
  display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 4px 16px;
  .k { flex-basis: 100%; text-align: center; color: var(--hi-text-3); }
  .acc { cursor: pointer; white-space: nowrap; &:hover b { color: var(--el-color-primary); } }
  b { font-family: var(--hi-font-mono); font-weight: 600; } em { font-style: normal; margin-left: 4px; color: var(--hi-text-3); } }
.foot { margin-top: 16px; text-align: center; font-size: 12px; color: var(--hi-text-3); }
footer { position: relative; margin-top: 28px; font-size: 12px; color: var(--hi-text-3); }
</style>
