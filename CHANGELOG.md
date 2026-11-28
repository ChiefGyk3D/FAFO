# Changelog

All notable changes to FAFO are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); while the major version is 0, any
release may change behavior, and the notes say so.

## [Unreleased]

## [0.1.0]

First release. FAFO is a read-only observer: it never kills, dispatches or merges anything.

### Added

- A collector (`fafo run`) that polls each source on its own interval with a hard timeout, keeps the
  last good row when a source goes stale, and writes one timestamped snapshot.
- Sources: Claude sessions (with project attribution and child processes), Codex, Gemini, Copilot
  cloud, one or several Ollama servers, and CI runners read over a read-only ssh helper and the
  GitHub REST API. Per-source configuration, with GitHub tokens read from a file or the environment.
- `fafo doctor`, which runs every enabled source once and reports ok, absent, down or stale.
- Renderers that only read the snapshot: `fafo table`, `fafo json`, `fafo sessions` and a Textual
  full-screen view (`fafo tui`) with six views and a dark theme.
- A Claude Code pane mod (`mods/claude-fafo`) that reads `views.json` and starts no process.
- Redaction of command lines and sanitizing of terminal text before anything is shown or written,
  and an opt-in mode that shows only safe arguments.
- A systemd user unit example, a performance note and a quickstart in the README.
- A release workflow that builds the sdist and wheel, attaches them with SBOMs and build provenance
  to the GitHub release, and can publish to PyPI through Trusted Publishing when enabled.
