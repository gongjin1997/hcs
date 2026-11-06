import { get } from './request'
import type { ResourceGuide } from '@/types/guides'

export const listResourceGuides = () => get<ResourceGuide[]>('/guides')
export const getResourceGuide = (resourceType: string) =>
  get<ResourceGuide>(`/guides/${encodeURIComponent(resourceType)}`)
