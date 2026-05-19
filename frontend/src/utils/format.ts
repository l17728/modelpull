const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  let v = n
  let i = 0
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${UNITS[i] ?? 'B'}`
}

export function formatRate(bytesPerSec: number | null | undefined): string {
  if (!bytesPerSec || bytesPerSec <= 0) return '—'
  return `${formatBytes(bytesPerSec)}/s`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.floor(seconds))
  if (s === 0) return '0s'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}
