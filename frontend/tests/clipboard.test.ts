import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from '../src/utils/clipboard'

afterEach(() => vi.unstubAllGlobals())
function setup(writeText?: () => Promise<void>) {
  const area = { value: '', readOnly: false, style: { position: '', left: '' }, focus: vi.fn(), select: vi.fn(), remove: vi.fn() }
  const previous = { focus: vi.fn() }
  const append = vi.fn()
  const exec = vi.fn(() => true)
  const create = vi.fn(() => area)
  vi.stubGlobal('navigator', writeText ? { clipboard: { writeText } } : {})
  vi.stubGlobal('document', { createElement: create, activeElement: previous, body: { appendChild: append }, execCommand: exec })
  return { area, previous, append, exec, create }
}

describe('clipboard on LAN HTTP pages', () => {
  it('uses the clipboard API when available', async () => {
    const write = vi.fn(async () => {})
    const { create } = setup(write)
    expect(await copyText('resource-id')).toBe(true)
    expect(write).toHaveBeenCalledWith('resource-id')
    expect(create).not.toHaveBeenCalled()
  })
  it('copies using a selected textarea when the clipboard API is unavailable', async () => {
    const { area, exec } = setup()
    expect(await copyText('resource-id')).toBe(true)
    expect(area.value).toBe('resource-id')
    expect(area.readOnly).toBe(true)
    expect(area.select).toHaveBeenCalledOnce()
    expect(exec).toHaveBeenCalledWith('copy')
  })
  it('falls back when the clipboard API rejects', async () => {
    const { exec } = setup(async () => { throw new Error('permission denied') })
    expect(await copyText('alarm-link')).toBe(true)
    expect(exec).toHaveBeenCalledOnce()
  })
  it('reports failure when copying is unsupported', async () => {
    const { exec } = setup()
    exec.mockReturnValue(false)
    expect(await copyText('resource-id')).toBe(false)
  })
  it('removes the temporary input and restores focus', async () => {
    const { area, previous } = setup()
    await copyText('resource-id')
    expect(area.remove).toHaveBeenCalledOnce()
    expect(previous.focus).toHaveBeenCalledWith({ preventScroll: true })
  })
  it('cleans up and reports failure when the copy command throws', async () => {
    const { exec, area, previous } = setup()
    exec.mockImplementation(() => { throw new Error('not supported') })
    expect(await copyText('resource-id')).toBe(false)
    expect(area.remove).toHaveBeenCalledOnce()
    expect(previous.focus).toHaveBeenCalledOnce()
  })
  it('works without a previously focused element', async () => {
    const { area } = setup()
    Object.defineProperty(document, 'activeElement', { value: null })
    expect(await copyText('resource-id')).toBe(true)
    expect(area.remove).toHaveBeenCalledOnce()
  })
})
