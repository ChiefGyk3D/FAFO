# FAFO: Framework Activity & Flow Observability

Fuck Around and Find Out. Now with real-time telemetry.

Your agents fuck around. You find out.

FAFO is a read-only observer of Claude, Codex, Gemini, Copilot cloud, Ollama and CI runners.
A collector polls each source on its own interval with a hard timeout, so one hung source never
blanks the rest, and writes a single timestamped snapshot. Renderers (a table or JSON view, a
Textual TUI and a Claude Code pane) only read that snapshot. It never kills, dispatches or merges
anything.

Status: early development. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the implementation plan.

License: Apache-2.0. Security reports: see `SECURITY.md`.
