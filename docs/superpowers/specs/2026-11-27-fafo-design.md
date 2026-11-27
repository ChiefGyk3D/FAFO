# FAFO: Framework Activity & Flow Observability

Fuck Around and Find Out. Now with real-time telemetry.
Your agents fuck around. You find out.

(Both lines are owner-approved project copy, 2026-11-27. Use verbatim.)

Status: DRAFT for owner approval. Nothing built. Date: 2026-11-27.

## Intent and the why

The owner runs about seven concurrent Claude Code sessions plus Codex, Gemini,
Copilot, Ollama and CI runners, across DevOps and DevSecOps work. Today the
view is a Claude Code sidebar pane (`ai-activity` mod, `ai-status` script,
`safo agents-status`). It fails in three ways:

1. It vanishes under load. The pane runs `ai-status` every 2 s with a 5 s
   timeout; the script itself does ssh calls (4-6 s timeouts) and sampling, so
   it overruns exactly when the machine is busy, and the pane swaps the last
   good data for "ai-status unavailable". It also only seats at 144+ columns.
2. It cannot say who is doing what. Six of seven sessions share cwd `~/src`
   and have auto names (`src-76`). A worktree path is not a project.
3. It is shallow: counts, not activity. No per-process PID, command, worktree,
   owning session, or project; runners show job counts, not what runs.

Goal: htop-quality, always-on observation of the whole agent and CI flow: a
panopticon. Public open-source tool, usable by anyone; owner's own setup is
one configuration of it. FAFO replaces `ai-status`, the `ai-activity` mod and
the display half of `safo agents-status`.

## Goals

- Every agent process (Claude, Codex, Gemini, Copilot, Ollama clients) shown
  with PID, owning session, project (not just worktree), worktree, branch,
  redacted command, state, CPU, memory, elapsed time.
- Claude sessions identified by project and current task, ties to session id,
  PID and status (busy/idle/waiting-for-input).
- Local LLM server depth: loaded models, VRAM, context, expiry, queue and
  admission state, in-flight requests with the requesting session where known,
  server GPU/CPU/memory.
- Runners depth: per runner, the job, repo, workflow, step, elapsed, queue,
  plus host load, memory, disk. Not just a count.
- Local host htop view: load, CPU, memory, disk, network, top processes, with
  agent processes highlighted and attributed.
- Never blanks under load. Stale data is dimmed with its age, never replaced.
- Two renderers over one data source: full-screen TUI and a Claude Code pane.
- Quota and rate-limit windows per provider where obtainable.
- Optional SAFO and SHIT adapters add issue, board card, epic, effort, Kind
  to each session. Works fully without them.

## Non-goals

- Not a controller: read-only. No kill, no dispatch, no merge. (Any later
  actions are a separate design with their own gate.)
- Not a metrics database or alerting system. Short in-memory history for
  sparklines only; export is a later item.
- Not a replacement for SAFO enforcement hooks (guards stay in SAFO).
- No cloud service, no telemetry sent anywhere. Local only.

## Anti-goals (failure even if it technically works)

- Blanking, freezing, or "unavailable" when a source is slow.
- Adding meaningful load to a loaded machine (the observer must be cheap).
- Leaking secrets: argv, env, tokens, transcript text, hostnames, serials,
  callsign/grid. The repo is public; so is every screenshot.
- Hard-coding the owner's GPUs, ports, hostnames, repos or paths.
- Claiming a depth a source cannot provide: unknown shows as unknown.

## Constraints

- Public repo, personal account by default (owner confirms at creation).
  Created only after this design is approved; then tracked in SAFO
  `tracking_repos` (owner OK), issue + board card + epic first.
- CI calls GYST reusable workflows (python-ci, security, artifact/package
  release), `CI green` gate, pins by commit. Dependabot cooldown set.
- All dependencies pinned; new releases held 7 days.
- American English, no em dashes, dark theme only, no light mode.
- Secrets never in repo, argv, or logs. Config by env var or config file with
  no secrets in it.
- Every setting configurable (hosts, ports, intervals, paths, which sources).
- SAFO/SHIT parity: SAFO keeps its own base features; SHIT ports FAFO only as
  configurable adapter settings. FAFO sits beneath both as a shared dependency
  (consistent with consolidate-into-shared-tools doctrine).

## Approaches considered

1. Grow `ai-status`/`agents-status` in place. Cheapest; private; not
   reusable; keeps the blocking-poll failure. Rejected.
2. Collector daemon + renderers. Chosen by owner 2026-11-27.
3. TUI polls directly, no daemon. Simpler but blanks under load and gives the
   pane nothing to share. Rejected.

## Architecture (the chosen approach; details below are my calls)

```
sources (plugins) -> collector (per-source workers, own timers, timeouts)
                  -> redactor -> snapshot store (timestamped fields)
                  -> snapshot file / unix socket (read-only)
renderers: TUI (Textual)  |  Claude Code pane (thin reader)  |  --json / --table
adapters (optional): SAFO, SHIT -> enrich sessions with issue/card/epic
```

- Each source runs in its own worker with its own interval, hard timeout and
  backoff. A slow or dead source marks only its own rows stale. No source can
  block another or a renderer.
- Snapshot carries per-field `as_of` and per-source health. Renderers dim by
  age, show a health strip (source ok / stale Ns / down).
- Redaction happens in the collector, once, before anything is written:
  argv scrubbed by pattern and by an allowlist of shown arguments, env never
  collected, transcript text never stored (only derived project and a short
  task label), hostnames/IPs mapped to configured aliases.
- Renderers never touch sources. The pane and TUI cannot add load to sources.
- Project attribution (the hard problem): precedence is (1) adapter data
  (SAFO `issue:` line in the agent brief, board card), (2) git toplevel and
  remote of the session's recent file activity and child process cwds,
  (3) the cwd's repo, (4) cwd name. Each row shows which rule produced it.
  Task label comes from the session's recent turn summary in its own data,
  stored truncated and redacted.

## Sources (depth, honest)

| Source | Reads | Depth |
|---|---|---|
| Claude Code | `~/.claude/sessions/*.json`, process tree, transcripts (derive project/task only), usage windows | deepest |
| Codex | process tree, `codex exec` children, its session files | high |
| Gemini | process tree, `~/.gemini` state | medium, no per-task view |
| Copilot (owner uses the cloud agent, 2026-11-27) | GitHub REST: Actions runs/jobs/steps for the coding agent (INFERRED that it runs as Actions; verify in vendor docs before building), linked PRs; local CLI logs only as an optional extra | medium, API-bound |
| Ollama / local LLM | server `/api/ps`, `/api/version`, `llm-local` admission log, server host stats | high |
| Runners | ssh read-only to runner host: Runner.Worker, `_diag` logs, load/mem/disk; GitHub API runs/jobs/steps | high |
| Host | psutil: CPU, mem, disk, net, GPU (nvidia-smi if present), top processes | high |

Each source is a plugin with a declared capability list and a self-test.
Sources that are absent are hidden, not errors.

## UI (sketch; final layout is the builder's, within these rules)

```
FAFO  host load 4.1  mem 61%  gpu 78%      sources: claude ok codex ok ollama ok runners stale 6s
SESSIONS
 PID     AGENT   PROJECT        TASK                     STATE    BRANCH/WT       ELAPSED CPU
 3941975 claude  reach-around   PR #482 review fix       busy     wt-482          1h12    3%
 1884786 claude  safo           waiting: input needed    waiting  main            3h40    0%
PROCESSES (agent children)  PID  SESSION  CMD(redacted)  CPU  MEM
LOCAL LLM  gemma4:12b 8.4G ctx128k exp 4m | in-flight 1 | queue 0 | gpu 78%
RUNNERS   r1 busy  repo/workflow/step 3/7  elapsed 2m10  load 3.2 | r2 idle
QUOTA     claude 5h 41%  wk 63%   codex ...
```

- Keys: sort, filter (agent, project, state), tree view, drill-in, pause,
  scope (this session / all). Mouse optional.
- Full-screen TUI and pane share one view model; the pane shows a compact
  subset and never needs 144 columns (degrades by dropping columns).
- Fits 1920x1080 without scrolling on default layout.

## Integration

- SAFO and SHIT: adapters read their status commands (`safo status`, board
  data) and enrich sessions. SAFO adapter ships first (owner's setup); SHIT
  adapter is a configurable port. Neither is required.
- Claude Code pane: a small mod that reads the snapshot; no polling scripts,
  no subprocess per refresh.
- `fafo --table` and `--json` replace `ai-status --table` for scripts.
- Cutover: run FAFO beside `ai-status` until parity checked, then retire
  `ai-status` and the `ai-activity` mod (owner's decision, cutover is its own
  step).

## Testing and measurement (what "done" means)

- Under synthetic load (CPU stress plus a hung ssh target) the display keeps
  rendering, shows stale age, never "unavailable". Measured.
- Collector overhead budget stated and measured (target under 2% of one core
  idle, under 5% active; memory under 100 MB), reported in CI or a script.
- Redaction: fixture suite of argv with tokens, urls with credentials, keys;
  nothing sensitive survives to the snapshot. Revert-proved tests.
- Source plugins tested against recorded fixtures, no network in tests.
- Project attribution tested on the real ambiguity case (several sessions in
  one cwd, different repos in children).

## Open items left to the builder

- Python minimum version, Textual vs alternative if Textual overhead fails the
  budget, snapshot transport (file vs socket), history length, config format.
- Exact column sets per width, color theme within dark-only.
- Packaging (pipx, release artifact via GYST).

## Governance sequence (after approval)

Epic and issues filed first, board cards with Kind/Epic/Effort, repo created
(personal, public), added to SAFO `tracking_repos` with owner OK, GYST caller
CI from the first commit, then implementer agents (Sonnet) per issue; lead
verifies. Security review on the Daybreak tier before first release.

## Access plan (owner decisions 2026-11-27)

- GitHub App token, read-only: Actions: read (run, job, step names; job names
  its runner). Pull requests: read only if docs confirm it is needed to link
  Copilot runs. Administration: read NOT requested (runner host view is ssh).
- Pre-repo tracking: issue in AI-Shit (private), card on the homelab research
  board (name confirmed at filing). Moves to the FAFO repo once created.

## Calls I made (flagged for owner)

- Name expands to FAFO; repo name `FAFO` (owner may rename).
- Python + Textual; psutil; read-only.
- Redaction in collector, allowlist-first.
- Replaces `ai-status` and the display half of `safo agents-status`, with a
  cutover step rather than an immediate removal.
- Remote hosts (runners, AI server) are observed agentless over read-only ssh
  first; an optional small FAFO agent on a remote host is a later item.
- GitHub data via REST only, conditional requests (ETag), a configured poll
  floor; never GraphQL board listing (scarce budget). Read-only scopes only
  (Actions: read, metadata), from a GitHub App token, never a PAT.
- Snapshot file is mode 0600, owner-only; socket likewise.
- Owner's "take over entirely" read as: FAFO becomes the one observer; SAFO
  keeps enforcement.
