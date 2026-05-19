import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import AggregateRing from '@/components/taskdetail/AggregateRing.vue'

describe('AggregateRing', () => {
  test('renders percent + counts', () => {
    const w = mount(AggregateRing, {
      props: {
        percent: 67, filesDone: 108, filesTotal: 163,
        bytesDone: 1000, bytesTotal: 2000,
      },
    })
    expect(w.text()).toContain('67%')
    expect(w.text()).toContain('108')
    expect(w.text()).toContain('163')
    expect(w.find('circle').exists()).toBe(true)
  })
})
