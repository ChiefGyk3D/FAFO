// The one place that decides which file the pane may read. Pure: no engine calls.
// The file must be an absolute, already-normal path that ends in views.json, so a
// misconfigured option can never make the pane read another file.

export const FILE_NAME = 'views.json'
export const MAX_PATH = 1024

function hasControl(path: string): boolean {
  for (const ch of path) {
    const c = ch.charCodeAt(0)
    if (c < 32 || (c >= 127 && c < 160)) return true
  }
  return false
}

// True when `path` is absolute, short, free of control characters, has no empty, `.` or
// `..` component, and its last component is exactly views.json.
export function isAllowedPath(path: string): boolean {
  if (path.length === 0 || path.length > MAX_PATH) return false
  if (!path.startsWith('/') || hasControl(path)) return false
  const parts = path.slice(1).split('/')
  for (const part of parts) {
    if (part === '' || part === '.' || part === '..') return false
  }
  return parts[parts.length - 1] === FILE_NAME
}

// Joins a configured ~/ path onto home and requires the result to stay under home.
export function underHome(home: string, rest: string): string | null {
  const base = home.endsWith('/') ? home.slice(0, -1) : home
  const full = `${base}/${rest}`
  if (!isAllowedPath(full)) return null
  return full.startsWith(`${base}/`) ? full : null
}
