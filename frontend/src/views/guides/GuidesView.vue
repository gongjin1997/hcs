<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { get } from '@/api/request'
import { useGuideCatalog } from './useGuideCatalog'
import type { ResourceGuide } from '@/types/guides'
import { guideFieldGroups } from './guideFields'

const route = useRoute()
const router = useRouter()
const { guides, loading, loadError, load } = useGuideCatalog(signal =>
  get<ResourceGuide[]>('/guides', undefined, { signal, silent: true }))
const search = ref('')
const showTechnical = ref(false)

const selectedType = computed(() => typeof route.query.type === 'string'
  ? route.query.type : guides.value[0]?.resource_type ?? 'ecs_server')
const guide = computed(() => guides.value.find((item) => item.resource_type === selectedType.value))
const groups = computed(() => guide.value ? guideFieldGroups(guide.value, search.value) : [])
const matchedCount = computed(() => groups.value.reduce((total, group) => total + group.fields.length, 0))

function selectType(type: string) {
  void router.replace({ query: { ...route.query, type } })
}

onMounted(load)
watch(selectedType, () => { search.value = '' }, { flush: 'sync' })
</script>

<template>
  <div class="guides-page">
    <PageHeader title="资源与指标指南" :refresh="load" :refreshing="loading" />

    <div class="guide-controls">
      <el-select :model-value="selectedType" filterable aria-label="选择资源类型" class="guide-type-select"
        :disabled="loading && !guides.length" @change="selectType">
        <el-option v-for="item in guides" :key="item.resource_type" :value="item.resource_type"
          :label="showTechnical ? `${item.name}（${item.resource_type}）` : item.name" />
      </el-select>
      <el-checkbox v-model="showTechnical" aria-label="显示技术字段">技术字段</el-checkbox>
      <el-input v-model="search" clearable :prefix-icon="Search" aria-label="检索指标和字段"
        placeholder="搜索字段、含义或来源" class="guide-search" />
    </div>

    <el-alert v-if="loadError" :title="guides.length ? `刷新失败：${loadError}，显示上次加载内容` : loadError" type="error" show-icon :closable="false">
      <el-button link type="primary" :loading="loading" @click="load">重新加载</el-button>
    </el-alert>
    <div v-loading="loading" class="guide-content">
      <el-empty v-if="!loading && !loadError && !guide" :description="guides.length ? `未找到资源类型：${selectedType}` : '暂无指南'" />
      <template v-if="guide">
        <el-card shadow="never" class="guide-card">
          <div class="table-heading">
            <div>
              <h3 class="guide-section-title">{{ guide.name }}</h3>
              <p class="guide-description">{{ guide.summary }}</p>
            </div>
            <div class="guide-tags">
              <el-tag effect="plain">{{ guide.service.toUpperCase() }}</el-tag>
              <el-tag :type="guide.ledger ? 'info' : 'warning'" effect="plain">{{ guide.ledger ? '纳入台账' : '仅巡检' }}</el-tag>
            </div>
          </div>
          <p v-if="showTechnical" class="guide-description"><span class="guide-label">资源类型：</span><code>{{ guide.resource_type }}</code></p>
          <p v-if="showTechnical" class="guide-description"><span class="guide-label">巡检任务：</span>{{ guide.collection_tasks.join('、') || '无独立巡检任务；仅台账同步' }}</p>
          <ul class="guide-notes">
            <li v-for="note in guide.limitations" :key="note">{{ note }}</li>
          </ul>
        </el-card>

        <div class="guide-practices">
          <el-card shadow="never" class="guide-card">
            <h3 class="guide-section-title">排障顺序</h3>
            <ol class="guide-notes"><li v-for="note in guide.guidance" :key="note">{{ note }}</li></ol>
          </el-card>
          <el-card shadow="never" class="guide-card">
            <h3 class="guide-section-title">使用建议</h3>
            <ul class="guide-notes"><li v-for="note in guide.best_practices" :key="note">{{ note }}</li></ul>
          </el-card>
        </div>

        <p v-if="search.trim()" class="guide-description" aria-live="polite">找到 {{ matchedCount }} 项字段说明</p>
        <el-empty v-if="search.trim() && !matchedCount" description="没有匹配字段" />
        <section v-for="group in groups" :key="group.key" class="guide-field-section">
          <template v-if="!search.trim() || group.fields.length">
            <div class="table-heading">
              <div>
                <h3 class="guide-section-title">{{ group.label }} <span class="guide-count">{{ group.fields.length }} / {{ group.total }}</span></h3>
                <p class="guide-description">{{ group.description }}</p>
              </div>
            </div>
            <p v-if="!group.total" class="guide-empty">暂无此类字段。</p>
            <el-table v-else :data="group.fields" row-key="key" border class="guide-table">
              <el-table-column label="字段 / 中文名" min-width="195">
                <template #default="{ row }">
                  <div class="guide-field-label">{{ row.label }}</div>
                  <code v-if="showTechnical || search.trim()" class="guide-key">{{ group.prefix }}{{ row.key }}</code>
                  <div class="guide-field-tags">
                    <el-tag v-if="showTechnical" size="small" type="info" effect="plain">{{ row.data_type }}</el-tag>
                    <el-tag v-if="row.availability === 'not_collected'" size="small" type="warning">未采集</el-tag>
                    <el-tag v-else-if="row.availability === 'derived'" size="small" effect="plain">平台计算</el-tag>
                    <el-tag v-if="showTechnical && row.optional" size="small" type="info" effect="plain">可缺失</el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="单位" width="115">
                <template #default="{ row }">{{ row.unit ?? '无单位/原值' }}</template>
              </el-table-column>
              <el-table-column label="含义与适用限制" min-width="320">
                <template #default="{ row }">
                  <p class="guide-meaning">{{ row.meaning }}</p>
                  <ul v-if="row.limitations.length" class="guide-field-notes">
                    <li v-for="note in row.limitations" :key="note">{{ note }}</li>
                  </ul>
                </template>
              </el-table-column>
              <el-table-column v-if="showTechnical" label="来源 / 规则检查项" min-width="235">
                <template #default="{ row }">
                  <p class="guide-meaning">{{ row.source }}</p>
                  <div v-if="group.key === 'facts'" class="guide-checks">
                    <span class="guide-label">check_item：</span>{{ row.check_items.join('、') || '当前类型的流水线通用事实' }}
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.guides-page { min-width: 0; display: flex; flex-direction: column; gap: 18px; }
.guide-description { overflow-wrap: anywhere; margin: 8px 0 0; color: var(--el-text-color-secondary); line-height: 1.7; }
.guide-controls { display: flex; flex-wrap: wrap; gap: 12px; }
.guide-type-select { width: 360px; max-width: 100%; }
.guide-search { flex: 1; min-width: min(320px, 100%); }
.guide-content { min-width: 0; min-height: 160px; }
.guide-card { margin-bottom: 16px; }
.guide-practices { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.guide-section-title { margin: 0; font-size: 16px; }
.guide-tags, .guide-field-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.guide-notes { overflow-wrap: anywhere; padding-left: 21px; margin: 12px 0 0; line-height: 1.9; }
.guide-notes li + li { margin-top: 5px; }
.guide-label { color: var(--el-text-color-secondary); }
.guide-count { margin-left: 8px; font-size: 12px; font-weight: 400; color: var(--el-text-color-secondary); }
.guide-field-section { margin-top: 22px; }
.guide-table { margin-top: 12px; }
.guide-field-label { font-weight: 600; margin-bottom: 5px; }
.guide-key { overflow-wrap: anywhere; font-size: 12px; }
.guide-field-tags { margin-top: 8px; }
.guide-meaning { margin: 0; line-height: 1.7; overflow-wrap: anywhere; }
.guide-field-notes { margin: 8px 0 0; padding-left: 17px; color: var(--el-text-color-secondary); line-height: 1.7; }
.guide-checks { margin-top: 9px; overflow-wrap: anywhere; }
.guide-empty { color: var(--el-text-color-secondary); padding: 15px; background: var(--el-fill-color-light); border-radius: 4px; }
@media (max-width: 850px) {
  .guide-practices { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .guide-type-select, .guide-search { width: 100%; flex-basis: 100%; }
}
</style>
