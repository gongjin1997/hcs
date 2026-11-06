import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import type { UserImportResult } from '../src/types'
import { useUserImport } from '../src/views/system/useUserImport'
import { deferred } from './businessViewHarness'

const dispose: (() => void)[] = []
afterEach(() => { dispose.splice(0).forEach(stop => stop()) })
export const personnelPreview = (overrides: Partial<UserImportResult> = {}): UserImportResult => ({
  total: 2, created: 1, updated: 1, unchanged: 0, error_count: 0, errors: [],
  items: [{ row: 2, username: 'directory', action: 'update', changes: { 姓名: ['旧姓名', '新姓名'] } }],
  dry_run: true, applied: 0, ...overrides,
})
function setup() {
  const allowed = ref(true)
  const importer = vi.fn<(file: File, dryRun: boolean, signal?: AbortSignal) => Promise<UserImportResult>>()
  const scope = effectScope()
  const model = scope.run(() => useUserImport({ allowed: () => allowed.value, importer }))!
  dispose.push(() => scope.stop())
  return { model, importer, allowed, scope }
}
const file = () => new File(['fixture'], '人员.xlsx')

describe('人员 XLSX 原子导入', () => {
  it('必须先预览，确认使用同一文件，不发送跳过错误参数且阻止重复提交', async () => {
    const { model, importer } = setup()
    expect(await model.apply()).toBeNull()
    const selected = file()
    importer.mockResolvedValueOnce(personnelPreview())
    await model.preview(selected)
    expect(importer).toHaveBeenCalledWith(selected, true, expect.any(AbortSignal))
    const pending = deferred<UserImportResult>()
    importer.mockReturnValueOnce(pending.promise)
    const apply = model.apply()
    expect(importer).toHaveBeenLastCalledWith(selected, false)
    expect(await model.apply()).toBeNull()
    await model.preview(file())
    expect(importer).toHaveBeenCalledTimes(2)
    pending.resolve(personnelPreview({ dry_run: false, applied: 2 }))
    expect((await apply)?.applied).toBe(2)
    expect(model.canApply.value).toBe(false)
    expect(await model.apply()).toBeNull()
  })

  it.each([
    { error_count: 1, errors: [{ row: 3, username: 'bad', message: '账号重复' }] },
    { error_count: 0, errors: [{ row: 3, username: 'bad', message: '校验错误' }] },
    { created: 0, updated: 0, unchanged: 2 },
    { dry_run: false, applied: 2 },
  ])('错误、无变更或已应用的预览不能确认：%j', async invalid => {
    const { model, importer } = setup()
    importer.mockResolvedValueOnce(personnelPreview(invalid))
    await model.preview(file())
    expect(model.canApply.value).toBe(false)
    expect(await model.apply()).toBeNull()
    expect(importer).toHaveBeenCalledTimes(1)
  })

  it.each(['resolve', 'reject'] as const)('更换文件会废弃旧预览的%s', async outcome => {
    const { model, importer } = setup()
    const old = deferred<UserImportResult>()
    importer.mockReturnValueOnce(old.promise).mockResolvedValueOnce(personnelPreview({ created: 2, updated: 0 }))
    const first = model.preview(file())
    const selected = new File(['new'], '新人员.xlsx')
    await model.preview(selected)
    expect(importer.mock.calls[0][2]?.aborted).toBe(true)
    if (outcome === 'resolve') old.resolve(personnelPreview({ created: 9 }))
    else old.reject(new Error('过期错误'))
    await first
    expect(model.file.value).toBe(selected)
    expect(model.result.value?.created).toBe(2)
    expect(model.parseError.value).toBe('')
  })

  it.each(['close', 'permission', 'dispose'] as const)('%s 后旧预览不能恢复', async action => {
    const { model, importer, allowed, scope } = setup()
    const pending = deferred<UserImportResult>()
    importer.mockReturnValueOnce(pending.promise)
    const preview = model.preview(file())
    if (action === 'close') model.reset()
    if (action === 'permission') allowed.value = false
    if (action === 'dispose') scope.stop()
    pending.resolve(personnelPreview())
    await preview
    expect(model.result.value).toBeNull()
    expect(await model.apply()).toBeNull()
  })

  it('确认时重新校验失败展示服务端错误，必须重新预览再确认', async () => {
    const { model, importer } = setup()
    importer.mockResolvedValueOnce(personnelPreview())
    await model.preview(file())
    importer.mockRejectedValueOnce(Object.assign(new Error('角色已删除'), {
      data: personnelPreview({ error_count: 1, errors: [{ row: 2, username: 'directory', message: '角色不存在' }] }),
    }))
    expect(await model.apply()).toBeNull()
    expect(model.result.value?.errors[0].message).toBe('角色不存在')
    expect(model.parseError.value).toContain('重新预览')
    expect(await model.apply()).toBeNull()
    importer.mockResolvedValueOnce(personnelPreview())
    await model.preview(file())
    expect(model.canApply.value).toBe(true)
  })

  it('不确定的网络失败不能重复使用旧预览', async () => {
    const { model, importer } = setup()
    importer.mockResolvedValueOnce(personnelPreview())
    await model.preview(file())
    importer.mockRejectedValueOnce(new Error('网络异常'))
    await model.apply()
    expect(model.canApply.value).toBe(false)
    expect(model.parseError.value).toContain('网络异常')
    expect(await model.apply()).toBeNull()
  })

  it('拒绝非 XLSX / 超过 10MB 的文件，旧预览不再有效', async () => {
    const { model, importer } = setup()
    importer.mockResolvedValueOnce(personnelPreview())
    await model.preview(file())
    await model.preview(new File(['csv'], '人员.csv'))
    expect(model.parseError.value).toContain('.xlsx')
    expect(model.canApply.value).toBe(false)
    await model.preview(new File([new Uint8Array(10 * 1024 * 1024 + 1)], '大文件.xlsx'))
    expect(model.parseError.value).toContain('10 MB')
    expect(importer).toHaveBeenCalledTimes(1)
  })
})
