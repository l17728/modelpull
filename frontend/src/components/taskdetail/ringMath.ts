/** Returns an SVG stroke-dasharray "<fill> <gap>" for a given percent. */
export function ringDash(percent: number, circumference: number): string {
  const p = Math.min(100, Math.max(0, percent))
  const fill = (p / 100) * circumference
  return `${+fill.toFixed(6)} ${+(circumference - fill).toFixed(6)}`
}
