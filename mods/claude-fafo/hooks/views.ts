// Parsing and sanitizing of views.json (schema 2). Pure functions: no engine calls.
// The file is written by the collector, which already sanitizes, but nothing here
// trusts it: shapes are checked, rows that are malformed are dropped, strings are
// stripped of control characters and escape sequences and capped.

export const SCHEMAS: readonly number[] = [2, 3]
export const MAX_BYTES = 2 * 1024 * 1024
export const MAX_SESSIONS = 200
export const MAX_CHILDREN = 256
export const MAX_INDEX = 2000
export const MAX_GROUPS = 50
export const MAX_LIST = 100
export const MAX_HELPER_NAMES = 3

const CAP = 160

// OSC (ESC ] ... BEL or ESC \), CSI (ESC [ ... final), any other ESC pair, and the
// 8-bit forms of OSC and CSI.
const OSC = /(?:\u001b\]|\u009d)[^\u0007\u001b\u009c]*(?:\u0007|\u001b\\|\u009c)?/g
const CSI = /(?:\u001b\[|\u009b)[0-?]*[ -/]*[@-~]?/g
const ESC_PAIR = /\u001b[\s\S]?/g
// C0, DEL, C1, bidi controls, zero-width characters, line and paragraph separators.
// Built from a string so no raw separator character sits in the source.
const CONTROL = new RegExp(
  '[\\u0000-\\u001f\\u007f-\\u009f\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u2069\\ufeff]',
  'g',
)

export function clean(value: unknown, cap: number = CAP): string {
  if (typeof value !== 'string') return ''
  const stripped = value
    .slice(0, cap * 4)
    .replace(OSC, '')
    .replace(CSI, '')
    .replace(ESC_PAIR, '')
    .replace(CONTROL, ' ')
    .replace(/ {2,}/g, ' ')
    .trim()
  return stripped.length > cap ? stripped.slice(0, cap) : stripped
}

export function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function pidOf(value: unknown): number | null {
  const n = num(value)
  return n !== null && Number.isInteger(n) && n >= 0 ? n : null
}

function obj(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function rows(value: unknown, max: number): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  const out: Record<string, unknown>[] = []
  for (const item of value.slice(0, max)) {
    const o = obj(item)
    if (o) out.push(o)
  }
  return out
}

export type Child = {
  pid: number | null
  agent: string
  cmd: string
  cpu: string
  rss: string
  stale: boolean
  age: string
  sessionId: string
}

export type Sess = {
  sessionId: string
  name: string
  address: string
  pid: number | null
  agent: string
  project: string
  task: string
  state: string
  branch: string
  elapsed: string
  cpu: string
  rss: string
  stale: boolean
  age: string
  children: Child[]
  childrenHidden: number
  // Boring helper processes the collector folded into one summary (absent in older files).
  collapsed: number
  collapsedNames: string[]
}

export type Group = { name: string; procs: Child[]; sessions: Sess[]; hidden: number }
export type Source = { name: string; status: string; age: string }
export type IndexRow = {
  sessionId: string
  name: string
  pid: number | null
  project: string
  state: string
}
export type Model = { name: string; vram: string; context: string; expires: string }
export type Runner = {
  name: string
  state: string
  repo: string
  step: string
  elapsed: string
  stale: boolean
}

export type Views = {
  generated: number | null
  status: string
  health: Source[]
  host: string[]
  sessions: Sess[]
  sessionsHidden: number
  unattached: Group[]
  llm: {
    models: Model[]
    queue: number | null
    inflight: number
    gpu: string
    stale: boolean
  } | null
  runners: Runner[]
  quota: string[]
  index: IndexRow[]
  indexTruncated: boolean
  indexHidden: number
}

function child(o: Record<string, unknown>): Child {
  return {
    pid: pidOf(o.pid),
    agent: clean(o.agent, 24),
    cmd: clean(o.cmd, 120),
    cpu: clean(o.cpu, 12),
    rss: clean(o.rss, 12),
    stale: o.stale === true,
    age: clean(o.age, 12),
    sessionId: clean(o.session_id, 80),
  }
}

function sess(o: Record<string, unknown>): Sess {
  return {
    sessionId: clean(o.session_id, 80),
    name: clean(o.name, 60),
    address: clean(o.address, 80),
    pid: pidOf(o.pid),
    agent: clean(o.agent, 24),
    project: clean(o.project, 60),
    task: clean(o.task, 120),
    state: clean(o.state, 24),
    branch: clean(o.branch, 60),
    elapsed: clean(o.elapsed, 16),
    cpu: clean(o.cpu, 12),
    rss: clean(o.rss, 12),
    stale: o.stale === true,
    age: clean(o.age, 12),
    children: rows(o.children, MAX_CHILDREN).map(child),
    childrenHidden: Math.max(0, Math.floor(num(o.children_hidden) ?? 0)),
    collapsed: Math.max(0, Math.floor(num(o.collapsed) ?? 0)),
    collapsedNames: Array.isArray(o.collapsed_names)
      ? o.collapsed_names
          .slice(0, MAX_HELPER_NAMES)
          .map(n => clean(n, 40))
          .filter(n => n !== '')
      : [],
  }
}

export type ParseResult =
  | { ok: true; views: Views }
  | { ok: false; kind: 'parse' | 'shape' | 'schema'; schema: string }

export function parseViews(text: string): ParseResult {
  let doc: unknown
  try {
    doc = JSON.parse(text)
  } catch {
    return { ok: false, kind: 'parse', schema: '' }
  }
  const top = obj(doc)
  if (!top) return { ok: false, kind: 'shape', schema: '' }
  if (typeof top.schema !== 'number' || !SCHEMAS.includes(top.schema)) {
    const raw = typeof top.schema === 'number' ? String(top.schema) : '?'
    return { ok: false, kind: 'schema', schema: clean(raw, 12) }
  }
  const host = obj(top.host)
  const llm = obj(top.llm)
  const views: Views = {
    generated: num(top.generated),
    status: clean(top.status, 16),
    health: rows(top.health, MAX_LIST).map(o => ({
      name: clean(o.name, 24),
      status: clean(o.status, 16),
      age: clean(o.age, 12),
    })),
    host: host
      ? (['load', 'cpu', 'mem', 'gpu'] as const)
          .map(k => [k, clean(host[k], 24)] as const)
          .filter(([, v]) => v !== '')
          .map(([k, v]) => `${k} ${v}`)
      : [],
    sessions: rows(top.sessions, MAX_SESSIONS).map(sess),
    sessionsHidden: Math.max(0, Math.floor(num(top.sessions_hidden) ?? 0)),
    unattached: rows(top.unattached, MAX_GROUPS).map(g => ({
      name: clean(g.name, 40),
      procs: rows(g.procs, MAX_CHILDREN).map(child),
      sessions: rows(g.sessions, MAX_SESSIONS).map(sess),
      hidden: Math.max(0, Math.floor(num(g.hidden) ?? 0)),
    })),
    llm: llm
      ? {
          models: rows(llm.models, MAX_LIST).map(m => ({
            name: clean(m.name, 40),
            vram: clean(m.vram, 12),
            context: clean(m.context, 12),
            expires: clean(m.expires, 12),
          })),
          queue: num(llm.queue),
          inflight: Array.isArray(llm.inflight) ? llm.inflight.length : 0,
          gpu: clean(llm.gpu, 16),
          stale: llm.stale === true,
        }
      : null,
    runners: rows(top.runners, MAX_LIST).map(r => ({
      name: clean(r.name, 32),
      state: clean(r.state, 16),
      repo: clean(r.repo, 48),
      step: clean(r.step, 48),
      elapsed: clean(r.elapsed, 12),
      stale: r.stale === true,
    })),
    quota: rows(top.quota, MAX_LIST)
      .map(q => `${clean(q.name, 24)} ${clean(q.text, 60)}`.trim())
      .filter(s => s !== ''),
    indexTruncated: top.session_index_truncated === true,
    indexHidden: Math.max(
      0,
      Math.floor(num(top.index_hidden) ?? 0) ||
        Math.floor((num(top.session_index_count) ?? 0) - rows(top.session_index, MAX_INDEX).length),
    ),
    index: rows(top.session_index, MAX_INDEX).map(r => ({
      sessionId: clean(r.session_id, 80),
      name: clean(r.name, 60),
      pid: pidOf(r.pid),
      project: clean(r.project, 60),
      state: clean(r.state, 24),
    })),
  }
  return { ok: true, views }
}

// The row of the session whose id is `id`: sessions first (children too, in case a
// row carries an id), the unattached groups, then the compact session index.
export type Found = {
  name: string
  project: string
  task: string
  pid: number | null
  state: string
}

export function findOwn(views: Views, id: string | null): Found | null {
  if (!id) return null
  for (const s of views.sessions) {
    if (s.sessionId === id) return s
    for (const c of s.children) {
      if (c.sessionId === id) {
        return { name: c.cmd, project: s.project, task: '', pid: c.pid, state: '' }
      }
    }
  }
  for (const g of views.unattached) {
    for (const s of g.sessions) if (s.sessionId === id) return s
  }
  for (const r of views.index) {
    if (r.sessionId === id) return { ...r, task: '' }
  }
  return null
}
