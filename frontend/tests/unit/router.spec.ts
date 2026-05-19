import { describe, expect, test } from 'vitest'
import { routes } from '@/router'

describe('routes', () => {
  test('has dashboard / taskList / taskCreate / taskDetail / login', () => {
    const byName = Object.fromEntries(
      routes.filter((r) => r.name).map((r) => [r.name, r]))
    expect(byName.dashboard?.path).toBe('/')
    expect(byName.taskList?.path).toBe('/tasks')
    expect(byName.taskCreate?.path).toBe('/tasks/new')
    expect(byName.taskDetail?.path).toBe('/tasks/:id')
    expect(byName.login?.meta?.public).toBe(true)
  })
})
