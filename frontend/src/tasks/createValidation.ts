import type { TaskCreateBody } from '@/api/types'

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/
const SHA_RE = /^[0-9a-f]{40}$/

export function validateCreate(b: Partial<TaskCreateBody>): string[] {
  const e: string[] = []
  if (!b.repo_id) e.push('repoRequired')
  else if (!REPO_RE.test(b.repo_id)) e.push('repoPattern')
  if (!b.revision) e.push('revRequired')
  else if (!SHA_RE.test(b.revision)) e.push('revPattern')
  if (!b.storage_id || b.storage_id <= 0) e.push('storageRequired')
  return e
}

export function mapCreateError(status: number | undefined): string {
  if (status === 403) return 'errors.forbidden'
  if (status === 409) return 'errors.conflict'
  if (status === 422) return 'errors.validation'
  if (status === 429) return 'errors.quota_exceeded'
  return 'errors.service_unavailable'
}
