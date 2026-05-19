export type EventLevel = 'info' | 'warn' | 'error'

const ERROR_HINTS = ['denied', 'failed', 'error']
const WARN_HINTS = ['quota', 'paused', 'retry', 'blacklist', 'degraded']

export function eventLevel(type: string, message: string): EventLevel {
  const hay = `${type} ${message}`.toLowerCase()
  if (ERROR_HINTS.some((h) => hay.includes(h))) return 'error'
  if (WARN_HINTS.some((h) => hay.includes(h))) return 'warn'
  return 'info'
}
