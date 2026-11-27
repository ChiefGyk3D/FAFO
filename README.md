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
Claude session in the snapshot. It only works when the command runs from a terminal inside
that Claude session. With no match, nothing is marked.

### Claude pane

A Claude Code pane that reads the same snapshot is planned; it is not part of this repository
yet.

### Configuration

FAFO needs no config file. Without one, four local sources are enabled: `host`, `claude`,
`codex` and `gemini`. `ollama`, `runners` and `copilot` are off until configured. To change
anything, copy `config.example.toml` somewhere and pass it with `--config PATH` to `run`,
`doctor` or any render command; FAFO does not search for a config file on its own.

Helper processes: a session that runs many short helpers (`sleep`, `tail`, shells) would fill
the screen, so the table, the terminal UI and the Claude pane show one summary row per session,
for example `+16 helpers (sleep x6, bash x4, cat x3)` (the count is exact, the three most common
names are listed). A helper stays visible when it uses 5 percent CPU or more, or when a process
that is not boring runs below it. Set `collapse_helpers = false` to turn this off, or change the
`boring_procs` list (program basenames, at most 64 of 1-32 characters from `A-Za-z0-9._-`).
In the terminal UI, Enter or `x` on the summary row shows the full list for that session and
toggles it back; the session screen always lists every process. The pane reads `collapsed` and
`collapsed_names` from `views.json` (additive fields, schema unchanged).

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

An idle session shows its last-known repo with a trailing `~`. The snapshot records that in a
per-session `project_source` key, written only when it is not `live`. A snapshot with no such
session has the same keys as before. A snapshot that does contain one is refused by older FAFO
readers by design: the snapshot schema is strict, and the version number is not bumped for this.

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
- Secrets never go in the config file. A source that needs a token names an environment
  variable (`token_env`); the file is rejected if it contains a secret-looking key.
- Everything printed passes through control-character stripping, so a hostile process name
  cannot send escape sequences to your terminal.

Status: early development. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the implementation plan.

Contributing and review process: see `CONTRIBUTING.md`.

License: Apache-2.0. Security reports: see `SECURITY.md`.
