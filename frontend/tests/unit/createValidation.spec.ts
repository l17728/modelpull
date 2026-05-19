import { describe, expect, test } from 'vitest'
import { validateCreate, mapCreateError } from '@/tasks/createValidation'

describe('validateCreate', () => {
  test('valid', () => {
    expect(validateCreate({ repo_id: 'org/m', revision: 'a'.repeat(40),
      storage_id: 1 })).toEqual([])
  })
  test('errors', () => {
    const e = validateCreate({ repo_id: 'bad', revision: 'xyz', storage_id: 0 })
    expect(e).toContain('repoPattern')
    expect(e).toContain('revPattern')
    expect(e).toContain('storageRequired')
  })
})
describe('mapCreateError', () => {
  test('http status → i18n key', () => {
    expect(mapCreateError(409)).toBe('errors.conflict')
    expect(mapCreateError(422)).toBe('errors.validation')
    expect(mapCreateError(429)).toBe('errors.quota_exceeded')
    expect(mapCreateError(403)).toBe('errors.forbidden')
    expect(mapCreateError(503)).toBe('errors.service_unavailable')
    expect(mapCreateError(500)).toBe('errors.service_unavailable')
  })
})
