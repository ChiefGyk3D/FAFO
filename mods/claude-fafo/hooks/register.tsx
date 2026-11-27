import type { EngineInterface, Register } from 'claude-code'

import { buildLines } from './layout'
import { FILE_NAME, isAllowedPath, underHome } from './path'
import type { Shown } from './layout'
import { MAX_BYTES, clean, parseViews } from './views'
import type { Views } from './views'

const PANE = 'fafo'
const EVERY_MS = 2000

type Timer = { cancel: () => void }

// The pane reads one prepared file. It never starts a process: the collector does
// the sampling and writes views.json; this module only reads and draws it.
// State lives in module variables: a reload starts them over and session.start
// runs again, which is all this pane needs.
const state: {
  ownId: string | null
  timer: Timer | null
  isReading: boolean
  last: Views | null
  goodAtMs: number | null
  nowMs: number
  note: string | null
  configured: string
} = {
  ownId: null,
  timer: null,
  isReading: false,
  last: null,
  goodAtMs: null,
  nowMs: 0,
  note: null,
  configured: '',
}

type Resolved = { path: string } | { refused: true } | { unset: true }

async function resolvePath($: EngineInterface): Promise<Resolved> {
  const configured = state.configured
  if (configured !== '') {
    if (configured.startsWith('/')) {
      return isAllowedPath(configured) ? { path: configured } : { refused: true }
    }
    if (configured.startsWith('~/')) {
      const home = await $.env.get('HOME')
      if (!home || !home.startsWith('/')) return { refused: true }
      const full = underHome(home, configured.slice(2))
      return full === null ? { refused: true } : { path: full }
    }
    return { refused: true }
  }
  const runtime = await $.env.get('XDG_RUNTIME_DIR')
  let candidate: string | null = null
  if (runtime && runtime.startsWith('/')) {
    candidate = underHome(runtime, `fafo/${FILE_NAME}`)
  } else {
    const home = await $.env.get('HOME')
    if (home && home.startsWith('/')) candidate = underHome(home, `.local/state/fafo/${FILE_NAME}`)
  }
  if (candidate === null) return { unset: true }
  return { path: candidate }
}

function failure(kind: string): string {
  return clean(`views.json ${kind}`, 80)
}

async function readOnce($: EngineInterface): Promise<void> {
  const resolved = await resolvePath($)
  if ('refused' in resolved) {
    state.note = 'viewsPath not allowed'
    return
  }
  if ('unset' in resolved) {
    state.note = failure('path not set (use an absolute viewsPath)')
    return
  }
  const path = resolved.path
  let text: string
  try {
    text = await $.fs.read(path, { as: 'text', length: MAX_BYTES + 1 })
  } catch (err) {
    const msg = err instanceof Error ? err.message : ''
    state.note = failure(/ENOENT|no such|not found|missing/i.test(msg) ? 'not found' : 'read failed')
    return
  }
  if (typeof text !== 'string' || text.length > MAX_BYTES) {
    state.note = failure('too large')
    return
  }
  const parsed = parseViews(text)
  if (parsed.ok) {
    state.last = parsed.views
    state.goodAtMs = await $.clock.now()
    state.note = null
  } else if (parsed.kind === 'schema') {
    state.note = failure(`schema ${parsed.schema} not supported (need 2 or 3)`)
  } else {
    state.note = failure(parsed.kind === 'parse' ? 'is not valid JSON' : 'has the wrong shape')
  }
}

async function refresh($: EngineInterface): Promise<void> {
  if (state.isReading) return
  state.isReading = true
  try {
    await readOnce($)
  } catch {
    state.note = failure('read failed')
  } finally {
    state.isReading = false
    state.nowMs = await $.clock.now()
    $.ui.invalidate('ui.render')
  }
}

function start($: EngineInterface): void {
  if (state.timer) return
  state.timer = $.clock.every(EVERY_MS, () => {
    void refresh($)
  })
  void refresh($)
}

function stop(): void {
  state.timer?.cancel()
  state.timer = null
}

export const register: Register = (on, options) => {
  state.configured = typeof options.viewsPath === 'string' ? options.viewsPath.trim() : ''

  on('session.start', async ($, e, next) => {
    try {
      state.ownId = await $.session.id()
    } catch {
      state.ownId = null
    }
    await $.command.register({ name: 'fafo', description: 'Show or hide the FAFO pane' })
    await $.ui.open({ id: PANE, title: 'FAFO' })
    start($)
    return next(e)
  })

  on('command.run', { command: 'fafo' }, async $ => {
    const panes = await $.ui.panes()
    if (panes.some(p => p.id === PANE)) {
      await $.ui.close({ id: PANE })
      stop()
      return { text: 'FAFO pane closed.' }
    }
    await $.ui.open({ id: PANE, title: 'FAFO' })
    start($)
    return { text: 'FAFO pane opened.' }
  })

  on('ui.close', { id: PANE }, async ($, e, next) => {
    const closed = await next(e)
    stop()
    return closed
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const props = e.props as { bodyColumns?: number; scroll?: { bodyRows?: number } }
    const cols = [props.bodyColumns, e.viewport?.columns].filter(
      (n): n is number => typeof n === 'number' && Number.isFinite(n) && n > 0,
    )
    const w = Math.max(10, Math.min(...cols, 240))
    const rawRows = props.scroll?.bodyRows ?? (e.viewport?.rows ?? 30) - 4
    const rows = Math.max(3, Math.min(Number.isFinite(rawRows) ? rawRows : 30, 400))
    const lines = buildLines({
        views: state.last,
        ownId: state.ownId,
        nowMs: state.nowMs,
        goodAtMs: state.goodAtMs,
        note: state.note,
      }, w, rows)

    return (
      <Box flexDirection="column">
        {lines.map(line => (
          <Text
            color={line.color}
            dimColor={line.dim === true}
            bold={line.bold === true}
            wrap="truncate-end"
          >
            {line.text}
          </Text>
        ))}
      </Box>
    )
  })
}
