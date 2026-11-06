/** 与后端 schemas/guide.py 保持一致；只有说明，不含租户观测。 */
export interface GuideField {
  key: string
  label: string
  unit: string | null
  meaning: string
  source: string
  data_type: 'number' | 'integer' | 'string' | 'boolean' | 'object' | 'array'
  optional: boolean
  availability: 'collected' | 'derived' | 'not_collected'
  check_items: string[]
  limitations: string[]
}

export interface ResourceGuide {
  resource_type: string
  name: string
  service: string
  category: string
  ledger: boolean
  summary: string
  collection_tasks: string[]
  metrics: GuideField[]
  facts: GuideField[]
  attributes: GuideField[]
  ledger_fields: GuideField[]
  guidance: string[]
  best_practices: string[]
  limitations: string[]
}
