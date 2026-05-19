export function oidcLoginUrl(base: string | undefined): string {
  return `${base ?? ''}/api/v1/auth/login`
}
