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

Ollama: one `url`, or up to 8 `[[sources.ollama.servers]]` tables (`name`, `url`), polled on their own so one
dead server never hides the others; its last rows stay and show their age. See `config.example.toml`.

### Security notes

- Read-only: FAFO never kills, dispatches, merges or writes to the things it observes. Its
  only write is the snapshot file (mode 0600; directories it creates are mode 0700).
- Redaction happens in the collector before anything is written: command arguments are
  scrubbed against an allowlist, environments and transcript text are never collected, and
  configured host names are replaced by aliases.
- Secrets never go in the config file. A source that needs a token names an environment
  variable (`token_env`); the file is rejected if it contains a secret-looking key.
- Everything printed passes through control-character stripping, so a hostile process name
  cannot send escape sequences to your terminal.

Status: early development. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the implementation plan.

License: Apache-2.0. Security reports: see `SECURITY.md`.
