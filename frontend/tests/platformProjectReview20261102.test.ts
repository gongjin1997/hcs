import { afterEach, describe, expect, it } from 'vitest'
import { mountBusinessScript } from './businessViewHarness'

interface Run { tenant_id: number; project_ref: number | null; status: string }
interface Task { runs: Run[] }
interface State { latestByTenant: (task: Task) => Run[]; successRate: (task: Task) => number | null }
const dispose: (() => void)[] = []
afterEach(() => dispose.splice(0).forEach(stop => stop()))
function view() {
  const component = mountBusinessScript<State>('system/TaskPanel.vue')
  dispose.push(component.unmount)
  return component.state
}
const row = (project: number | null, status = 'SUCCESS'): Run => ({ tenant_id: 1, project_ref: project, status })
describe('平台任务项目范围', () => {
  it('同租户两个项目和历史未知项目均独立显示', () => {
    const runs = view().latestByTenant({ runs: [row(102), row(101, 'FAILED'), row(null, 'PARTIAL'), row(102)] })
    expect(runs).toHaveLength(3)
    expect(runs.map(run => run.project_ref)).toEqual([null, 101, 102])
  })
  it('未执行的跳过不能进入成功率分母', () => {
    expect(view().successRate({ runs: [row(101), row(102, 'SKIPPED'), row(null, 'RUNNING')] })).toBe(100)
  })
  it('没有已执行记录的成功率是未知而不是0', () => {
    expect(view().successRate({ runs: [row(101, 'SKIPPED')] })).toBeNull()
  })
  it('部分成功不能进入成功数', () => {
    expect(view().successRate({ runs: [row(101), row(102, 'PARTIAL')] })).toBe(50)
  })
})
