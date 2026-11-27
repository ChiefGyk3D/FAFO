# FAFO pane for Claude Code

A Claude Code mod that shows the FAFO view in the sidebar: who is doing what across your
agent sessions, runners and local LLM. It only reads one prepared file. It never starts a
process.

The plugin name is `fafo-pane` (the folder is `mods/claude-fafo`). Claude Code's validator
refuses third-party plugin names that start with `claude-`.

## Load it

```
claude --plugin-dir mods/claude-fafo
```

The pane opens at session start (id `fafo`, title FAFO). `/fafo` toggles it. A pane opened
by the mod seats from 144 columns; below that, run `/fafo` once to seat it at any width.

## What it reads

`views.json`, schema 2, written atomically by the FAFO collector next to its snapshot
(`fafo.render.export.write_views`). Every 2 seconds the mod does one bounded text read
(at most 2 MiB, one read in flight at a time) and stops when the pane closes.

Where the file is, in order:

1. The `viewsPath` option, if set. It must be an absolute path, or `~/` followed by a path
   under your home folder.
2. `$XDG_RUNTIME_DIR/fafo/views.json`
3. `~/.local/state/fafo/views.json`

This matches the collector's default. If you pointed the collector somewhere else, set
`viewsPath` in the plugin's config row (`/config`) or in settings under
`pluginConfigs["fafo-pane"].options.viewsPath`. A relative path is refused with a note
in the pane.

## What it shows

- Top line: `you are here: <name> | <project> | <task> | pid <pid> | <state>`, found by the
  session id from `$.session.id()` in `sessions` (then `session_index`). No match shows
  exactly `unknown session`.
- A health line (per source status and age), then host load.
- Sessions, the current one first and marked `>`, each with its commands indented under it,
  then unattached groups, then LLM, runners and quota when there is room. Stale rows are
  dimmed with an age tag.
- Columns are dropped by priority as the pane narrows, text is cut with an ellipsis, and a
  `+N more` line shows rows that do not fit. Nothing is wider than the pane.

## It never blanks

A missing file, read error, bad JSON, wrong shape, oversized file or schema other than 2
keeps the last good view on screen with a one line note and its age. With no good read yet
it shows `collector not running`. A file whose `generated` time is over 60 seconds old shows
`collector not running (last update Ns ago)` over dimmed rows.

## Safety

Every string from the file has control characters and ANSI/OSC sequences stripped and is
length capped. Rows are shape checked, malformed rows are skipped, rows are capped (200
sessions, 256 children each). Strings are drawn as plain text only. Dark theme only: no
fills, only theme text colors and dimming.

## Tests

```
claude plugin test mods/claude-fafo
claude plugin validate mods/claude-fafo
```
