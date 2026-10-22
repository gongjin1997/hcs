<template>
  <div class="error-page">
    <div class="code num">{{ code }}</div>
    <h3>{{ code === 403 ? '暂无访问权限' : '页面不存在' }}</h3>
    <p>{{ code === 403 ? '该页面仅管理员可访问，如需操作请联系平台管理员。' : '请检查链接是否正确，或返回首页继续操作。' }}</p>
    <el-button type="primary" @click="$router.replace('/dashboard')">返回首页</el-button>
  </div>
</template>

<script setup lang="ts">
defineProps<{ code: number }>()
</script>

<style scoped lang="scss">
.error-page { min-height: 60vh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;
  .code { font-size: 96px; font-weight: 800; line-height: 1; background: linear-gradient(135deg, #1d63f0, #36cfc9); -webkit-background-clip: text; background-clip: text; color: transparent; }
  h3 { margin: 16px 0 8px; font-size: 20px; } p { margin: 0 0 24px; color: var(--hi-text-3); } }
</style>
