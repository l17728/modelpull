export interface NavItem {
  route: string // route name
  labelKey: string // i18n key under nav.*
  icon: string // element-plus icon component name
  roles?: string[] // if set, visible only to these roles
}

export const NAV_ITEMS: NavItem[] = [
  { route: 'dashboard', labelKey: 'nav.dashboard', icon: 'Odometer' },
  { route: 'taskList', labelKey: 'nav.tasks', icon: 'List' },
  { route: 'taskCreate', labelKey: 'nav.createTask', icon: 'Plus' },
  { route: 'executors', labelKey: 'nav.executors', icon: 'Monitor' },
  { route: 'audit', labelKey: 'nav.audit', icon: 'Document' },
  { route: 'quota', labelKey: 'nav.quota', icon: 'DataLine' },
  { route: 'settings', labelKey: 'nav.settings', icon: 'Setting' },
]

export function visibleNav(role: string, items: NavItem[] = NAV_ITEMS): NavItem[] {
  return items.filter((i) => !i.roles || i.roles.includes(role))
}
