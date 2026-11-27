import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const PLUGIN = 'fafo-pane'
const NOW_S = 1_000_000
const OWN = 'sid-own-0001'
const ESC = String.fromCharCode(27)
const BEL = String.fromCharCode(7)
const NUL = String.fromCharCode(0)
const BS = String.fromCharCode(8)

const paneProps = (cols: number, rows = 40) =>
  ({
    title: 'FAFO',
    isFocused: false,
    bodyColumns: cols,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: rows },
    view: {},
  }) as const

function node(n: number, extra: Record<string, unknown> = {}) {
  return {
    session_id: `sid-${n}`,
    name: `work-${n}`,
    address: `sid-${n}`,
    pid: 1000 + n,
    agent: 'claude',
    project: `proj-${n}`,
    project_rule: 'cwd-name',
    task: `task number ${n}`,
    state: 'busy',
    branch: 'main',
    worktree: '/work/example',
    elapsed: '1h02',
    cpu: '3%',
    rss: '120M',
    is_current: false,
    stale: false,
    age: '',
    children: [
      { pid: 5000 + n, agent: 'codex', cmd: `codex exec run-${n}`, cpu: '1%', rss: '40M', stale: false, age: '' },
    ],
    children_hidden: 0,
    ...extra,
  }
}

function views(over: Record<string, unknown> = {}) {
  return {
    schema: 2,
    generated: NOW_S,
    status: 'ok',
    age_s: 1,
    banner: null,
    message: '',
    health: [
      { name: 'claude', status: 'ok', age: '1s' },
      { name: 'runners', status: 'stale', age: '9s' },
    ],
    host: { load: '1.5', cpu: '10%', mem: '40%', gpu: '' },
    sessions: [node(1), node(2), node(3)],
    sessions_hidden: 0,
    unattached: [],
    llm: {
      models: [{ name: 'model-a', vram: '4G', context: '8k', expires: '4m' }],
      queue: 0,
      inflight: [],
      gpu: '50%',
      stale: false,
    },
    runners: [
      { name: 'r1', state: 'busy', repo: 'example/repo', workflow: 'ci', job: 'build', step: '3/7', elapsed: '2m', load: '1', mem: '1', disk: '1', stale: false },
    ],
    runners_hidden: 0,
    quota: [{ name: 'claude', text: '5h 41%' }],
    session_index: [
      { session_id: OWN, name: 'my-session', pid: 4242, project: 'example-proj', state: 'idle', address: OWN },
    ],
    ...over,
  }
}

const OWN_NODE = {
  session_id: OWN,
  name: 'mine',
  project: 'proj-x',
  task: 'fix it',
  pid: 77,
  state: 'waiting',
}
const OWN_BANNER = 'you are here: mine | proj-x | fix it | pid 77 | waiting'

type Ctl = {
  file: { value: string | Error }
  reads: { count: number; open: number; maxOpen: number; paths: string[] }
  spawned: string[]
  isPaneOpen: boolean
}

async function boot($: Engine, on: On, opts: { ownId?: string; delayMs?: number; first?: Error } = {}) {
  const clock = mock.clock(on, { now: NOW_S * 1000 })
  mock.env(on, { HOME: '/home/example', XDG_RUNTIME_DIR: '/run/user/1' })
  const ctl: Ctl = {
    file: { value: opts.first ?? JSON.stringify(views()) },
    reads: { count: 0, open: 0, maxOpen: 0, paths: [] },
    spawned: [],
    isPaneOpen: true,
  }
  on('session.id', () => ({ value: opts.ownId ?? OWN }))
  on('command.register', () => ({ value: { command: 'fafo' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: ctl.isPaneOpen ? [{ id: 'fafo' }] : [] }) as never)
  on('ui.close', () => {
    ctl.isPaneOpen = false
    return { value: undefined } as never
  })
  on('process.run', () => {
    ctl.spawned.push('run')
    return { deny: 'no process here' }
  })
  on('process.spawn', async function* () {
    ctl.spawned.push('spawn')
    return { deny: 'no process here' } as never
  })
  on('fs.read', async (_, e) => {
    ctl.reads.count += 1
    ctl.reads.paths.push(e.path)
    ctl.reads.open += 1
    ctl.reads.maxOpen = Math.max(ctl.reads.maxOpen, ctl.reads.open)
    try {
      if (opts.delayMs) await clock.sleep(opts.delayMs)
      if (ctl.file.value instanceof Error) return { deny: ctl.file.value.message }
      return { value: ctl.file.value }
    } finally {
      ctl.reads.open -= 1
    }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  return { clock, ctl }
}

async function lines(ui: { findAll: (q: { type: string }) => Promise<{ text: string }[]> }) {
  const found = await ui.findAll({ type: 'Text' })
  return found.map(f => f.text)
}

async function draw($: Engine, cols: number, rows = 40) {
  return $.ui.mount({
    plugin: PLUGIN,
    surface: 'terminal',
    component: 'Pane',
    requestId: 'fafo',
    props: paneProps(cols, rows),
    viewport: { columns: cols + 4, rows: rows + 4 },
  })
}

async function load($: Engine, on: On, doc: unknown, opts: { delayMs?: number } = {}) {
  const r = await boot($, on, opts)
  r.ctl.file.value = typeof doc === 'string' ? doc : JSON.stringify(doc)
  await r.clock.advance(2000)
  return r
}

test('banner shows the own session found in sessions', async ($, on) => {
  await load($, on, views({ sessions: [node(1), node(2, OWN_NODE)] }))
  const ui = await draw($, 120)
  expect((await lines(ui))[0]).toBe(OWN_BANNER)
  await ui.unmount()
})

test('banner falls back to session_index when sessions lacks the own id', async ($, on) => {
  await load($, on, views())
  const ui = await draw($, 120)
  expect((await lines(ui))[0]).toBe('you are here: my-session | example-proj | ? | pid 4242 | idle')
  await ui.unmount()
})

test('banner is exactly unknown session when the own id is nowhere', async ($, on) => {
  await load($, on, views({ session_index: [] }))
  const ui = await draw($, 120)
  expect((await lines(ui))[0]).toBe('unknown session')
  await ui.unmount()
})

test('unknown session also when the own id is empty', async ($, on) => {
  const { ctl, clock } = await boot($, on, { ownId: '' })
  ctl.file.value = JSON.stringify(views())
  await clock.advance(2000)
  const ui = await draw($, 120)
  expect((await lines(ui))[0]).toBe('unknown session')
  await ui.unmount()
})

test('own session is first with a marker, children indented under their session', async ($, on) => {
  await load($, on, views({ sessions: [node(1), node(2), node(3, OWN_NODE)] }))
  const ui = await draw($, 120)
  const all = await lines(ui)
  const rows = all.filter(t => /work-|mine/.test(t) && !t.startsWith('you are here'))
  expect(rows[0]).toMatch(/^> mine /)
  expect(rows[1]).toMatch(/^ {2}work-1 /)
  expect(rows[2]).toMatch(/^ {2}work-2 /)
  const at = all.findIndex(t => /^ {2}work-1 /.test(t))
  expect(all[at + 1]).toMatch(/^ {4}codex exec run-1/)
  await ui.unmount()
})

test('stale rows are dimmed and carry an age tag', async ($, on) => {
  await load($, on, views({ sessions: [node(1, { stale: true, age: '30s' }), node(2)] }))
  const ui = await draw($, 120)
  const stale = await ui.find({ type: 'Text', text: /work-1/ })
  expect(stale?.text).toContain('[30s]')
  expect(stale?.props.dimColor).toBe(true)
  const fresh = await ui.find({ type: 'Text', text: /work-2/ })
  expect(fresh?.props.dimColor).toBe(false)
  await ui.unmount()
})

test('a read error keeps the last good views with an age tag', async ($, on) => {
  const { ctl, clock } = await load($, on, views({ sessions: [node(1, OWN_NODE)] }))
  ctl.file.value = new Error('ENOENT: no such file')
  await clock.advance(4000)
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all[0]).toBe(OWN_BANNER)
  expect(all.some(t => /^views\.json not found \(data 4s old\)$/.test(t))).toBe(true)
  expect(all.some(t => t.startsWith('> mine'))).toBe(true)
  await ui.unmount()
})

test('garbage JSON keeps the last good views', async ($, on) => {
  const { ctl, clock } = await load($, on, views({ sessions: [node(1, OWN_NODE)] }))
  ctl.file.value = '{"schema": 2, ' + NUL + 'garbage'
  await clock.advance(2000)
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all[0]).toBe(OWN_BANNER)
  expect(all.some(t => t.startsWith('views.json is not valid JSON'))).toBe(true)
  expect(all.some(t => t.startsWith('> mine'))).toBe(true)
  await ui.unmount()
})

test('a wrong top-level shape and an oversized file keep the last good', async ($, on) => {
  const { ctl, clock } = await load($, on, views({ sessions: [node(1, OWN_NODE)] }))
  ctl.file.value = '[1, 2, 3]'
  await clock.advance(2000)
  let ui = await draw($, 120)
  expect((await lines(ui)).some(t => t.startsWith('views.json has the wrong shape'))).toBe(true)
  await ui.unmount()
  ctl.file.value = ' '.repeat(2 * 1024 * 1024 + 1)
  await clock.advance(2000)
  ui = await draw($, 120)
  const all = await lines(ui)
  expect(all.some(t => t.startsWith('views.json too large'))).toBe(true)
  expect(all[0]).toBe(OWN_BANNER)
  await ui.unmount()
})

test('malformed rows are ignored without throwing', async ($, on) => {
  const doc = views({
    sessions: [null, 7, 'x', node(1, { pid: 'abc', children: 'nope', cpu: { a: 1 } }), node(2, OWN_NODE)],
    health: 'bad',
    unattached: [{ name: 5, procs: [null], sessions: 'x' }],
    llm: 'bad',
    runners: [1, null],
    quota: [[], { name: 3 }],
  })
  await load($, on, doc)
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all[0]).toBe(OWN_BANNER)
  expect(all.some(t => /work-1/.test(t))).toBe(true)
  await ui.unmount()
})

test('never a good read: collector not running', async ($, on) => {
  const { clock } = await boot($, on, { first: new Error('ENOENT') })
  await clock.advance(2000)
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all[0]).toBe('unknown session')
  expect(all[1]).toBe('collector not running')
  await ui.unmount()
})

test('a file older than 60 s says the collector is not running and dims the rows', async ($, on) => {
  await load($, on, views({ generated: NOW_S - 100, sessions: [node(1, OWN_NODE)] }))
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all.some(t => /^collector not running \(last update 102s ago\)$/.test(t))).toBe(true)
  const row = await ui.find({ type: 'Text', text: /^> mine/ })
  expect(row?.props.dimColor).toBe(true)
  await ui.unmount()
})

test('status no-collector from the file says collector not running', async ($, on) => {
  await load($, on, views({ status: 'no-collector', sessions: [] }))
  const ui = await draw($, 120)
  expect((await lines(ui)).some(t => t === 'collector not running')).toBe(true)
  await ui.unmount()
})

test('a schema other than 2 is named and the last good stays', async ($, on) => {
  const { ctl, clock } = await load($, on, views({ sessions: [node(1, OWN_NODE)] }))
  ctl.file.value = JSON.stringify({ ...views(), schema: 3 })
  await clock.advance(2000)
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all.some(t => t.startsWith('views.json schema 3 not supported (need 2)'))).toBe(true)
  expect(all[0]).toBe(OWN_BANNER)
  await ui.unmount()
})

test('no line exceeds the pane width at any width', async ($, on) => {
  const long = 'x'.repeat(300)
  const many = Array.from({ length: 30 }, (_, i) =>
    node(i + 1, { task: long, project: long, name: long, children: [node(1).children[0], node(1).children[0]] }),
  )
  await load($, on, views({ sessions: [...many, node(40, OWN_NODE)] }))
  for (const cols of [144, 120, 80, 60, 40, 24]) {
    for (const rows of [12, 40]) {
      const ui = await draw($, cols, rows)
      const all = await lines(ui)
      expect(all.length).toBeGreaterThan(2)
      expect(all.length).toBeLessThanOrEqual(rows)
      for (const t of all) {
        expect([...t].length).toBeLessThanOrEqual(cols)
      }
      await ui.unmount()
    }
  }
})

test('rows that do not fit end in a +N more line, the current session stays first', async ($, on) => {
  const many = Array.from({ length: 30 }, (_, i) => node(i + 1))
  await load($, on, views({ sessions: [...many, node(40, OWN_NODE)] }))
  const ui = await draw($, 100, 14)
  const all = await lines(ui)
  expect(all.length).toBeLessThanOrEqual(14)
  expect(all[all.length - 1]).toMatch(/^\+\d+ more$/)
  expect(all.some(t => t.startsWith('> mine'))).toBe(true)
  await ui.unmount()
})

test('narrow widths drop columns by priority, wide widths show them', async ($, on) => {
  await load($, on, views({ sessions: [node(1, OWN_NODE)] }))
  const wide = await draw($, 144)
  const w = (await lines(wide)).find(t => t.startsWith('> mine')) ?? ''
  expect(w).toContain('proj-x')
  expect(w).toContain('fix it')
  expect(w).toContain('77')
  await wide.unmount()
  const narrow = await draw($, 30)
  const n = (await lines(narrow)).find(t => t.startsWith('> mine')) ?? ''
  expect(n).toContain('mine')
  expect(n).toContain('waiting')
  expect(n).not.toContain('fix it')
  await narrow.unmount()
})

test('control characters and escape sequences are stripped from every string', async ($, on) => {
  const bad = (s: string) => `${ESC}[31m${s}${ESC}]0;title${BEL}${NUL}${BS}${ESC}[2J`
  const sess = node(2, {
    ...OWN_NODE,
    name: bad('mine'),
    project: bad('proj-x'),
    task: bad('fix it'),
    state: bad('waiting'),
    children: [{ pid: 9, agent: 'codex', cmd: bad('run'), cpu: '1%', rss: '1M', stale: false, age: '' }],
  })
  await load($, on, views({ sessions: [sess], health: [{ name: bad('claude'), status: 'ok', age: '1s' }] }))
  const ui = await draw($, 120)
  const all = await lines(ui)
  expect(all.length).toBeGreaterThan(3)
  expect(all[0]).toBe(OWN_BANNER)
  for (const t of all) {
    expect([...t].some(ch => ch.charCodeAt(0) < 32 || (ch.charCodeAt(0) >= 127 && ch.charCodeAt(0) < 160))).toBe(false)
  }
  expect(all.some(t => t.includes('title'))).toBe(false)
  expect(all.some(t => t.includes('[31m'))).toBe(false)
  await ui.unmount()
})

test('markup in a string is drawn as plain text', async ($, on) => {
  const task = '<Text color="red">**x**</Text>'
  await load($, on, views({ sessions: [node(2, { ...OWN_NODE, task })] }))
  const ui = await draw($, 120)
  expect((await lines(ui))[0]).toBe(`you are here: mine | proj-x | ${task} | pid 77 | waiting`)
  await ui.unmount()
})

test('no process is started over a session of refreshes and pane toggles', async ($, on) => {
  const { ctl, clock } = await boot($, on)
  await clock.advance(10_000)
  expect(ctl.reads.count).toBeGreaterThan(2)
  await $.command.run({ command: 'fafo', args: '' } as never)
  await clock.advance(10_000)
  expect(ctl.spawned).toEqual([])
})

test('refresh reads every 2 s, one at a time, and stops when the pane closes', async ($, on) => {
  const { ctl, clock } = await boot($, on, { delayMs: 5000 })
  await clock.advance(10_000)
  expect(ctl.reads.maxOpen).toBe(1)
  expect(ctl.reads.count).toBeGreaterThan(1)
  await $.command.run({ command: 'fafo', args: '' } as never)
  await clock.advance(10_000)
  const frozen = ctl.reads.count
  await clock.advance(20_000)
  expect(ctl.reads.count).toBe(frozen)
})

test('the default path is under XDG_RUNTIME_DIR', async ($, on) => {
  const { ctl } = await boot($, on)
  expect(ctl.reads.paths[0]).toBe('/run/user/1/fafo/views.json')
})

test('a relative viewsPath is refused with a note, never read', { options: { viewsPath: 'views.json' } }, async ($, on) => {
  const { ctl, clock } = await boot($, on)
  await clock.advance(2000)
  expect(ctl.reads.count).toBe(0)
  const ui = await draw($, 120)
  expect((await lines(ui)).some(t => t.startsWith('views.json path not set'))).toBe(true)
  await ui.unmount()
})

test('an absolute viewsPath is used as given', { options: { viewsPath: '/srv/example/views.json' } }, async ($, on) => {
  const { ctl } = await boot($, on)
  expect(ctl.reads.paths[0]).toBe('/srv/example/views.json')
})

test('a ~/ viewsPath expands against HOME', { options: { viewsPath: '~/state/views.json' } }, async ($, on) => {
  const { ctl } = await boot($, on)
  expect(ctl.reads.paths[0]).toBe('/home/example/state/views.json')
})

test('the /fafo command reopens a closed pane and reading resumes', async ($, on) => {
  const { ctl, clock } = await boot($, on)
  await $.command.run({ command: 'fafo', args: '' } as never)
  await clock.advance(10_000)
  const closedAt = ctl.reads.count
  await clock.advance(10_000)
  expect(ctl.reads.count).toBe(closedAt)
  await $.command.run({ command: 'fafo', args: '' } as never)
  await clock.advance(6000)
  expect(ctl.reads.count).toBeGreaterThan(closedAt)
})
