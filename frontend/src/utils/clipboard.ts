/** Copy after a user action; LAN HTTP browsers may not expose navigator.clipboard. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* Try the selection-based path when permission is denied. */ }

  const previous = document.activeElement as HTMLElement | null
  const input = document.createElement('textarea')
  input.value = text
  input.readOnly = true
  input.style.position = 'fixed'
  input.style.left = '-9999px'
  document.body.appendChild(input)
  try {
    input.focus()
    input.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    input.remove()
    previous?.focus?.({ preventScroll: true })
  }
}
