import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'
import CopilotConfirmCard from '@/components/copilot/CopilotConfirmCard.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US',
                          messages: { 'en-US': en } })

const pending = {
  callId: 'c1', tool: 'dlw_cancel_task',
  input: { task_id: 'abc' }, rationale: 'Cancel abc.', estimatedImpact: {},
}

function mountCard() {
  return mount(CopilotConfirmCard, {
    props: { pending },
    global: { plugins: [ElementPlus, i18n] },
  })
}

describe('CopilotConfirmCard (SP4b)', () => {
  test('renders tool, rationale, input', () => {
    const w = mountCard()
    const txt = w.text()
    expect(txt).toContain('dlw_cancel_task')
    expect(txt).toContain('Cancel abc.')
    expect(txt).toContain('task_id')
  })

  test('Approve emits approve', async () => {
    const w = mountCard()
    await w.find('[data-test="copilot-confirm-approve"]').trigger('click')
    expect(w.emitted('approve')).toBeTruthy()
  })

  test('Reject emits reject', async () => {
    const w = mountCard()
    await w.find('[data-test="copilot-confirm-reject"]').trigger('click')
    expect(w.emitted('reject')).toBeTruthy()
  })

  test('Modify → edit JSON → submit emits modify with parsed object', async () => {
    const w = mountCard()
    await w.find('[data-test="copilot-confirm-modify"]').trigger('click')
    await w.find('textarea').setValue('{"task_id":"xyz"}')
    await w.find('[data-test="copilot-confirm-modify-submit"]').trigger('click')
    const ev = w.emitted('modify')
    expect(ev).toBeTruthy()
    expect(ev?.[0]?.[0]).toEqual({ task_id: 'xyz' })
  })
})
