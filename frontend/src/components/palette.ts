import { visibleNav } from '@/nav/registry'

export interface Command {
  id: string
  label: string
  kind: 'nav' | 'action'
  routeName?: string
  action?: 'createTask' | 'openTaskById' | 'openCopilot'
}

export function buildCommands(role: string, t: (k: string) => string): Command[] {
  const nav: Command[] = visibleNav(role).map((i) => ({
    id: `nav:${i.route}`, label: t(i.labelKey), kind: 'nav', routeName: i.route,
  }))
  const actions: Command[] = [
    { id: 'action:createTask', label: t('palette.createTask'),
      kind: 'action', action: 'createTask' },
    { id: 'action:openTaskById', label: t('palette.openTaskById'),
      kind: 'action', action: 'openTaskById' },
    { id: 'action:openCopilot', label: t('palette.openCopilot'),
      kind: 'action', action: 'openCopilot' },
  ]
  return [...nav, ...actions]
}
