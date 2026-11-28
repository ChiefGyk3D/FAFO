# FAFO: Framework Activity & Flow Observability

Fuck Around and Find Out. Now with real-time telemetry.

Your agents fuck around. You find out.

FAFO is a read-only observer of Claude, Codex, Gemini, Copilot cloud, Ollama and CI runners.
A collector polls each source on its own interval with a hard timeout, so one hung source never
blanks the rest, and writes a single timestamped snapshot. Renderers (a table or JSON view, a
Textual TUI and a Claude Code pane) only read that snapshot. It never kills, dispatches or merges
anything.

## Quickstart

Install from a clone of the repository (Python 3.11 or newer). The preferred way is pipx,
which puts the `fafo` command in `~/.local/bin` (run `pipx ensurepath` once if that directory
is not on your PATH):

```
git clone <this repository> && cd FAFO
pipx install .
fafo doctor     # run every enabled source once and report ok, absent, down or stale
```

The commands below are written for the pipx install. To use a virtual environment instead,
see "Alternative: virtual environment" further down.

```
fafo run        # the collector: polls the sources and writes the snapshot until Ctrl-C
fafo table      # print the table once (plain text, no color, fits the terminal)
fafo sessions   # one line per Claude session: name, session id, project
fafo tui        # full-screen view (needs Textual, which is a dependency)
fafo json       # the snapshot as JSON, for scripts
fafo line       # one short line for a status bar or shell prompt (empty when idle)
```

The design used flags such as `--table`; the commands above are subcommands instead.
Every render command takes `--snapshot PATH` and `--config PATH`. The snapshot path is
`--snapshot`, else `snapshot_path` from the config, else `$XDG_RUNTIME_DIR/fafo/snapshot.json`
(or `~/.local/state/fafo/snapshot.json` when `XDG_RUNTIME_DIR` is unset). With no collector
running they show "collector not running" and exit 0; `fafo json` prints
`{"status": "no-collector"}`.

To keep the collector running, see `examples/systemd/` for a systemd user unit (its `ExecStart=`
must point at your install: `~/.local/bin/fafo` for pipx, the venv `bin` otherwise).

### Alternative: virtual environment

Without pipx, install into a virtual environment. Its `fafo` is not on your PATH, so every
command is written with the full path (or run `source ~/.local/share/fafo-venv/bin/activate`
in that shell first and then use the bare commands above).

```
python3 -m venv ~/.local/share/fafo-venv
~/.local/share/fafo-venv/bin/pip install .
~/.local/share/fafo-venv/bin/fafo doctor
~/.local/share/fafo-venv/bin/fafo run
~/.local/share/fafo-venv/bin/fafo table
```

### You are here

`fafo table`, `fafo sessions` and `fafo tui` take `--session ID` to mark one session id as
yours. `--session auto` finds it by process ancestry: the nearest parent process that is a
Claude or Codex session in the snapshot. It only works when the command runs from a terminal
inside that session's process tree. With no match, nothing is marked.

### Using FAFO with Codex

Codex has no pane API, so run `fafo tui` in a tmux pane or a second terminal next to Codex.
Without anything else, Codex processes show as plain rows. To get a Codex session row with its
session id as the address, and `you are here`, install a SessionStart hook:

1. Copy `examples/codex/fafo-session-hook.sh` somewhere you own and make it executable.
2. Register it as a Codex `SessionStart` command hook in `$CODEX_HOME/hooks.json` (usually
   `~/.codex/hooks.json`; the exact path below must be absolute and quoted for a shell string):

   ```json
   {
     "hooks": {
       "SessionStart": [
         {
           "matcher": "startup|resume",
           "hooks": [
             {
               "type": "command",
               "command": "'/home/you/.codex/hooks/fafo-session-hook.sh'",
               "statusMessage": "Recording session for FAFO"
             }
           ]
         }
       ]
     }
   }
   ```

   The `matcher` makes resumed sessions record too. The `timeout` default is irrelevant: the
   hook finishes at once.
3. Trust the hook once. Codex requires you to review and trust a user hook definition before it
   runs: open `/hooks` inside Codex and trust it. An untrusted hook does not run, and FAFO then
   shows the session as a plain process row.
4. Verify by hand: (a) start Codex; (b) check that `<codex pid>.json` exists in the
   `codex-here` folder (see below) with mode 0600 and contains only `session_id`, `ppid` and
   `started_at`; (c) run `fafo tui --session auto` in a pane whose shell descends from the Codex
   process, or `fafo tui --session CODEX_SESSION_ID` from anywhere, and look for the marker.

What is documented and what is not: per the Codex hooks page as read 2026-11-27 (confirm with
`/hooks`), user hooks live in `$CODEX_HOME/hooks.json`, the format is JSON, SessionStart entries
have a `matcher` and a `hooks` list of `type`/`command`/`statusMessage` handlers, the hook gets
one JSON object on stdin with `session_id`, and exit 0 with no output is success. The exact
top-level `{"hooks": {...}}` wrapper was inferred from the statement that SessionStart sits in
the top-level hooks object, not copied verbatim. The page does not say whether `command` is a
shell string or an argument list (the quoted path above works for a shell string), what the
hook's environment is, or which process is the hook's parent.

What the hook writes: `<state dir>/codex-here/<codex pid>.json` with exactly `session_id`,
`ppid` and `started_at`, atomically, mode 0600 in a 0700 directory. The state dir is the
directory of the FAFO snapshot: `$XDG_RUNTIME_DIR/fafo`, else `~/.local/state/fafo` (the hook
takes a different directory as its first argument; the collector reads the directory next to
`snapshot_path`). Privacy: only the session id is recorded. The hook never reads
`transcript_path` or any other field, prints nothing and always exits 0.

The collector reads those files read-only. It does not list the directory: for each live
`codex` process it opens `<pid>.json` only (no-follow, regular file, owned by you, owner-only
mode, at most 4 KB, strict schema; at most 256 processes per pass), so stale files never hide
current ones. A record is used only if the process started at most 10 minutes before the record
was written and at most 0.25 s after it (the hook runs after the process starts; the small
allowance covers the different clocks). A reused pid, or a Codex session whose hook did not run
within 10 minutes of its start, is not matched and shows as a plain process row. The collector
never deletes files; remove stale ones yourself, for example
`find ~/.local/state/fafo/codex-here -name '*.json' -mtime +7 -delete`.
The hook's parent process being the Codex process is not stated by the Codex docs. FAFO relies
on it and checks it with the pid and start-time rule above: if Codex starts hooks through an
intermediate process, no record matches and no session row appears.

### Status line

`fafo line` prints exactly one line from the snapshot, for a status bar or a shell prompt. It
reads the snapshot once, with no network and no subprocess. Segments, in this order, each shown
only while something is busy: `cl 2/5` (Claude sessions busy of total), `cx 1/3` (Codex
processes using at least 1% CPU, of all Codex processes), `ol 1/2` (Ollama busy requests of
loaded models; queued plus in-flight requests, shown as `99+` above 99), `gm 1` (Gemini processes using at least 1% CPU), `rn 1/4` (runners busy of
total), `cp 1` (Copilot cloud runs running). A source that is down (its health status is
`down`, an Ollama server is unreachable, or a runner is down) gets a trailing `!` (`ol!`), even
when idle. A stale source (for example a collector timeout) gets no `!`: with `--color` it is
dimmed while busy, and it is omitted while idle. When everything is idle the line is empty
(just a newline).

Options: `--width N`, `--color`, `--quiet`, `--stale-after SECONDS` (and `--snapshot`, `--config`).

- `--width N` (default 80, clamped to 3 to 300): the line never has more visible characters.
  Whole segments are dropped from the end and a cut ends in `...`; no segment is cut in half.
- `--color` (off by default): dim for a stale source, red for a down one. Output is plain ASCII
  without it.
- A snapshot that is missing, unreadable, over the size cap or older than `--stale-after`
  (default 30 seconds) prints `fafo: no collector`, or nothing with `--quiet`.
- It always exits 0 and never prints a traceback, so a status bar is never broken. A bad
  `--width` or `--stale-after` value falls back to the default instead of failing; an unknown
  option is still a usage error.

```
# in a status bar or prompt: show the line only when there is something to show
PS1='$(fafo line --quiet) \$ '
```

### Claude pane

The Claude Code pane ships in this repository as the mod in `mods/claude-fafo` (plugin name
`fafo-pane`). It shows the same view as the other renderers: a "you are here" line, source
health, sessions with their commands, the local LLM, runners and quota. It does not read the
snapshot. It reads `views.json`, which the collector writes next to the snapshot, one bounded
read every 2 seconds, and it starts no process. Load it with
`claude --plugin-dir mods/claude-fafo`; `/fafo` toggles the pane. A missing or bad file keeps
the last good view or shows `collector not running`. See `mods/claude-fafo/README.md` for
setup and the `viewsPath` option, and `docs/claude-pane-types.md` for the type definitions.

CI type-checks the mod's hooks with a pinned TypeScript (the `Pane mod type-check` job). Its
behavior tests run locally with `claude plugin test mods/claude-fafo`.

### Configuration

FAFO needs no config file. Without one, four local sources are enabled: `host`, `claude`,
`codex` and `gemini`. `ollama`, `runners`, `copilot` and `codex_jobs` are off until configured. To change
anything, copy `config.example.toml` somewhere and pass it with `--config PATH` to `run`,
`doctor` or any render command; FAFO does not search for a config file on its own.

Ollama: one `url`, or up to 8 `[[sources.ollama.servers]]` tables (`name`, `url`), polled on their own so one
dead server never hides the others; its last rows stay and show their age. Each server's poll (two requests) has one deadline, min(6, `timeout_s` minus 0.5) seconds, always less than `timeout_s` (the Ollama `timeout_s` must be at least 1 second, a lower value is refused; 2.5 s for the `timeout_s = 3` in the example, 4.5 s for the default 5), with the servers polled in parallel; a hung server costs one abandoned daemon thread, never more. See `config.example.toml`.

Helper processes: a session that runs many short helpers (`sleep`, `tail`, shells) would fill
the screen, so the table, the terminal UI and the Claude pane show one summary row per session,
for example `+16 helpers (sleep x6, bash x4, cat x3)` (the count is exact, the three most common
names are listed). A helper stays visible when it uses 5 percent CPU or more, or when a process
that is not boring runs below it. Set `collapse_helpers = false` to turn this off, or change the
`boring_procs` list (program basenames, at most 64 of 1-32 characters from `A-Za-z0-9._-`).
In the terminal UI, Enter or `x` on the summary row lists up to 256 of that session's helpers, followed by a
row counting any not shown, and toggles it back; the session screen always lists every process. The pane reads `collapsed` and
`collapsed_names` from `views.json` (additive fields, schema unchanged).

Codex plugin jobs (`[sources.codex_jobs]`): shows running jobs and jobs finished within
`window_minutes` (default 60, 1 to 1440) as a short JOBS section in the table, with running,
done and failed counts. `jobs_dir` is required: the folder holding the plugin's
`<id>/jobs/*.json` folders (one level) or the job files directly. A running job counts only while
its `pid` is alive. Job files are untrusted: each is opened without following links and must be a
regular file you own, not group or world writable, at most 64 KB, with a known `status`;
anything else is counted as unreadable and never shown. At most 512 directory entries are
examined and 64 rows kept; the rest are counted. Job titles can contain your prompt text, so they
are hidden unless `show_titles = true`; shown titles are credential-masked, stripped of control
characters and cut to 60 characters, and are never logged. The snapshot gains an optional
`jobs` key, present only when this source ran, so snapshots without it are unchanged.

### Task labels

The TASK column of a Claude session shows what it is doing. By default (`task_labels =
"activity"` under `[sources.claude]`) that is the last tool plus a minimal object, such as
`Bash: make` (first word of the command only) or `Edit: widget.py` (file name only), read from
the last 64 KB of the session transcript and capped at 80 characters. When that tool call is more than two minutes old the label gets an
age suffix such as `(5m ago)`. `"off"` shows the state
only. `"prompt"` shows the first 60 characters of your last message. Masking is best effort
(token shapes and keyword phrases such as `password: ...`), so it may still display other
sensitive prose, on screen and in screenshots. Use it only if you are comfortable with that.

### Snapshot compatibility

The snapshot is strict JSON with a version number. Version 2 (current) adds `llms`, the list of
local LLM servers (each with `name`, `last_ok` and `reachable`), and keeps `llm`, a copy of the
first server. FAFO still reads version 1 files (one `llm`, no names; it becomes one server named
`ollama`). An older FAFO that only knows version 1 refuses a version 2 file by design (strict
version and key checks), so run the collector and the readers from the same release.

An idle session shows its last-known repo with a trailing `~`. The snapshot records that in a
per-session `project_source` key, written only when it is not `live`, under either version. A
snapshot with no such session has the same session keys as before. A snapshot that does contain
one is refused by readers that predate the key, because the schema is strict.

### Security notes

- Read-only: FAFO never kills, dispatches, merges or writes to the things it observes. Its
  only write is the snapshot file (mode 0600; directories it creates are mode 0700).
- Redaction happens in the collector before anything is written: command arguments are
  scrubbed against an allowlist, environments are never collected, and transcript text is read only for the short task label described above, and
  configured host names are replaced by aliases.
- Command lines are shown allowlist-first (`args_mode = "allowlist"`, the default). The opt-in
  `args_mode = "safe"` also shows short bare words, flag names and the base name of a script,
  module or path, at most 12 tokens. It never shows the word after a flag (a flag's value) unless
  the flag is in `show_args`, and it hides URLs, anything with `@`, `:`, `=`, quotes or spaces,
  and secret-looking words. A bare word that is a positional argument and does not follow a flag
  can still show. Safe mode shows more of each command line on screen, in snapshots and in
  screenshots, so it is off by default.
- `show_value_args` lists the flags in `show_args` that take a value (default `--model` and
  `--resume`). Only the token after a listed flag is shown, and it still goes through credential
  masking and the 256-character cap. A flag shown but not listed is boolean: the next token stays
  `<arg>`, in both `args_mode` settings. Entries must look like flag names (at most 32), and a
  secret-named flag such as `--token` is a config error.
- Secrets never go in the config file. A source that needs a token names an environment
  variable (`token_env`); the file is rejected if it contains a secret-looking key.
- Everything printed passes through control-character stripping, so a hostile process name
  cannot send escape sequences to your terminal.

Status: early development. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the implementation plan.

Contributing and review process: see `CONTRIBUTING.md`.

License: Apache-2.0. Security reports: see `SECURITY.md`.
