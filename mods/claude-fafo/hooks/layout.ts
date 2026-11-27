// Turns parsed views into plain text lines that fit a width. Pure: no engine calls.
// Every line is cut to the width in terminal cells, so the pane never overflows.
import { findOwn } from './views'
import type { Child, Group, Sess, Views } from './views'

export type Line = { text: string; color?: string; dim?: boolean; bold?: boolean }

export type Shown = {
  views: Views | null
  ownId: string | null
  nowMs: number
  goodAtMs: number | null
  // One short sanitized sentence about the last refresh, or null when it went well.
  note: string | null
}

const ELLIPSIS = '…'
const STALE_AFTER_S = 60

function cellWidth(ch: string): number {
  const c = ch.codePointAt(0) ?? 0
  if (c >= 0x300 && c <= 0x36f) return 0
  if (
    (c >= 0x1100 && c <= 0x115f) ||
    (c >= 0x2e80 && c <= 0xa4cf) ||
    (c >= 0xac00 && c <= 0xd7a3) ||
    (c >= 0xf900 && c <= 0xfaff) ||
    (c >= 0xfe30 && c <= 0xfe6f) ||
    (c >= 0xff00 && c <= 0xff60) ||
    (c >= 0xffe0 && c <= 0xffe6) ||
    (c >= 0x1f300 && c <= 0x1f64f) ||
    (c >= 0x1f900 && c <= 0x1f9ff) ||
    (c >= 0x20000 && c <= 0x3fffd)
  ) {
    return 2
  }
  return 1
}

export function width(text: string): number {
  let w = 0
  for (const ch of text) w += cellWidth(ch)
  return w
}

export function fit(text: string, max: number): string {
  if (max <= 0) return ''
  if (width(text) <= max) return text
  if (max === 1) return ELLIPSIS
  let out = ''
  let w = 0
  for (const ch of text) {
    const cw = cellWidth(ch)
    if (w + cw > max - 1) break
    out += ch
    w += cw
  }
  return out + ELLIPSIS
}

function pad(text: string, w: number): string {
  const cut = fit(text, w)
  return cut + ' '.repeat(Math.max(0, w - width(cut)))
}

type Col = { key: string; min: number; max: number; flex?: boolean }

// Columns are listed in priority order: the first is always kept, each later one
// only while it still fits. A flex column may be squeezed down to its minimum.
function pickColumns(defs: Col[], cells: Record<string, string>[], avail: number) {
  const out: { key: string; w: number }[] = []
  let used = 0
  for (const [i, d] of defs.entries()) {
    let longest = d.min
    for (const row of cells) longest = Math.max(longest, width(row[d.key] ?? ''))
    const natural = Math.min(d.max, longest)
    const room = avail - used - (i === 0 ? 0 : 1)
    if (i === 0) {
      out.push({ key: d.key, w: Math.max(1, Math.min(natural, avail)) })
      used += out[0]?.w ?? 0
    } else if (natural <= room) {
      out.push({ key: d.key, w: natural })
      used += natural + 1
    } else if (d.flex && room >= d.min) {
      out.push({ key: d.key, w: room })
      used += room + 1
    }
  }
  return out
}

function render(cols: { key: string; w: number }[], cell: Record<string, string>): string {
  return cols
    .map(c => pad(cell[c.key] ?? '', c.w))
    .join(' ')
    .trimEnd()
}

const STATE_COLOR: Record<string, string> = {
  busy: 'success',
  waiting: 'warning',
  error: 'error',
}

const sessCols: Col[] = [
  { key: 'name', min: 6, max: 18 },
  { key: 'state', min: 5, max: 8 },
  { key: 'age', min: 3, max: 8 },
  { key: 'project', min: 6, max: 16 },
  { key: 'task', min: 10, max: 30, flex: true },
  { key: 'pid', min: 3, max: 8 },
  { key: 'elapsed', min: 5, max: 8 },
  { key: 'cpu', min: 3, max: 6 },
  { key: 'rss', min: 3, max: 7 },
  { key: 'agent', min: 5, max: 8 },
  { key: 'branch', min: 6, max: 14 },
]

const procCols: Col[] = [
  { key: 'cmd', min: 8, max: 40, flex: true },
  { key: 'pid', min: 3, max: 8 },
  { key: 'cpu', min: 3, max: 6 },
  { key: 'rss', min: 3, max: 7 },
  { key: 'agent', min: 5, max: 8 },
  { key: 'age', min: 3, max: 8 },
]

function sessCell(s: Sess, mark: string): Record<string, string> {
  return {
    name: mark + (s.name || s.sessionId || '?'),
    state: s.state,
    age: s.stale ? `[${s.age || 'stale'}]` : '',
    project: s.project,
    task: s.task,
    pid: s.pid === null ? '' : String(s.pid),
    elapsed: s.elapsed,
    cpu: s.cpu,
    rss: s.rss,
    agent: s.agent,
    branch: s.branch,
  }
}

function procCell(c: Child): Record<string, string> {
  return {
    cmd: c.cmd || c.agent,
    pid: c.pid === null ? '' : String(c.pid),
    cpu: c.cpu,
    rss: c.rss,
    agent: c.agent,
    age: c.stale ? `[${c.age || 'stale'}]` : '',
  }
}

function bodyLines(views: Views, ownId: string | null, w: number): Line[] {
  const all: Sess[] = [...views.sessions]
  const own = ownId ? all.findIndex(s => s.sessionId === ownId) : -1
  if (own > 0) all.unshift(...all.splice(own, 1))
  const groups: Group[] = views.unattached.filter(g => g.procs.length + g.sessions.length > 0)

  const sessRows = [...all, ...groups.flatMap(g => g.sessions)]
  const avail = Math.max(1, w - 2)
  const sc = pickColumns(sessCols, sessRows.map(s => sessCell(s, '')), avail)
  const allProcs = [...all.flatMap(s => s.children), ...groups.flatMap(g => g.procs)]
  const pc = pickColumns(procCols, allProcs.map(procCell), Math.max(1, w - 4))

  const out: Line[] = []
  const session = (s: Sess) => {
    const isOwn = ownId !== null && s.sessionId === ownId
    const mark = isOwn ? '> ' : '  '
    out.push({
      text: fit(mark + render(sc, sessCell(s, '')), w),
      color: isOwn ? 'claude' : STATE_COLOR[s.state],
      bold: isOwn,
      dim: s.stale,
    })
    for (const c of s.children) {
      out.push({ text: fit('    ' + render(pc, procCell(c)), w), dim: c.stale || s.stale })
    }
    if (s.childrenHidden > 0) {
      out.push({ text: fit(`    +${s.childrenHidden} more`, w), dim: true })
    }
  }
  if (all.length > 0) out.push({ text: fit('SESSIONS', w), bold: true })
  for (const s of all) session(s)
  if (views.sessionsHidden > 0) {
    out.push({ text: fit(`  +${views.sessionsHidden} more sessions`, w), dim: true })
  }
  for (const g of groups) {
    out.push({ text: fit(g.name || 'OTHER', w), bold: true })
    for (const s of g.sessions) session(s)
    for (const c of g.procs) {
      out.push({ text: fit('  ' + render(pc, procCell(c)), w), dim: c.stale })
    }
    if (g.hidden > 0) out.push({ text: fit(`  +${g.hidden} more`, w), dim: true })
  }
  return out
}

function extraLines(views: Views, w: number): Line[] {
  const out: Line[] = []
  const l = views.llm
  if (l) {
    const head = [
      'LLM',
      l.queue === null ? '' : `queue ${l.queue}`,
      `in-flight ${l.inflight}`,
      l.gpu ? `gpu ${l.gpu}` : '',
    ]
      .filter(Boolean)
      .join(' | ')
    out.push({ text: fit(head, w), bold: true, dim: l.stale })
    for (const m of l.models.slice(0, 3)) {
      const t = [m.name, m.vram, m.context ? `ctx ${m.context}` : '', m.expires ? `exp ${m.expires}` : '']
      out.push({ text: fit('  ' + t.filter(Boolean).join(' '), w), dim: l.stale })
    }
  }
  if (views.runners.length > 0) {
    out.push({ text: fit('RUNNERS', w), bold: true })
    for (const r of views.runners.slice(0, 4)) {
      const t = [r.name, r.state, r.repo, r.step, r.elapsed].filter(Boolean).join(' ')
      out.push({ text: fit('  ' + t, w), dim: r.stale })
    }
  }
  if (views.quota.length > 0) {
    out.push({ text: fit('QUOTA ' + views.quota.join(' | '), w), dim: true })
  }
  return out
}

function ageText(s: number): string {
  return `${Math.max(0, Math.round(s))}s`
}

export function buildLines(shown: Shown, w: number, rowsMax: number): Line[] {
  const { views, ownId } = shown
  const head: Line[] = []

  const found = views ? findOwn(views, ownId) : null
  if (found) {
    const pid = found.pid === null ? '?' : String(found.pid)
    const text = `you are here: ${found.name || '?'} | ${found.project || '?'} | ${found.task || '?'} | pid ${pid} | ${found.state || '?'}`
    head.push({ text: fit(text, w), bold: true, color: 'claude' })
  } else {
    const note =
      views && views.indexTruncated ? ` (index truncated, ${views.indexHidden} hidden)` : ''
    head.push({ text: fit('unknown session' + note, w), bold: true, dim: true })
  }

  if (!views) {
    head.push({ text: fit('collector not running', w), color: 'warning' })
    if (shown.note) head.push({ text: fit(shown.note, w), dim: true })
    return head.slice(0, rowsMax)
  }

  const sinceGood =
    shown.goodAtMs === null ? 0 : Math.max(0, (shown.nowMs - shown.goodAtMs) / 1000)
  const genAge = views.generated === null ? null : shown.nowMs / 1000 - views.generated
  const collectorDown =
    views.status === 'no-collector' || (genAge !== null && genAge > STALE_AFTER_S)
  if (collectorDown) {
    const last =
      views.status === 'no-collector' || genAge === null
        ? ''
        : ` (last update ${ageText(genAge)} ago)`
    head.push({ text: fit(`collector not running${last}`, w), color: 'warning' })
  }
  if (shown.note) {
    head.push({
      text: fit(`${shown.note} (data ${ageText(sinceGood)} old)`, w),
      color: 'warning',
    })
  }

  const h = views.health.map(s => `${s.name} ${s.status}${s.age ? ' ' + s.age : ''}`)
  if (h.length > 0) head.push({ text: fit(h.join(' | '), w), dim: collectorDown })
  if (views.host.length > 0) head.push({ text: fit(views.host.join(' '), w), dim: collectorDown })

  const stale = collectorDown
  const body = bodyLines(views, ownId, w).map(l => (stale ? { ...l, dim: true } : l))
  const room = rowsMax - head.length
  let lines: Line[]
  if (body.length <= room) {
    lines = [...head, ...body]
    const extra = extraLines(views, w).map(l => (stale ? { ...l, dim: true } : l))
    const left = rowsMax - lines.length
    if (left >= 2) {
      const take = extra.slice(0, left)
      lines.push(...take)
    }
  } else {
    const keep = Math.max(0, room - 1)
    const hidden = body.length - keep
    lines = [...head, ...body.slice(0, keep), { text: fit(`+${hidden} more`, w), dim: true }]
  }
  return lines.slice(0, Math.max(1, rowsMax))
}
