# Type-check of the Claude pane mod

CI type-checks the hooks module in `mods/claude-fafo/hooks` with a pinned TypeScript against
a committed copy of the Claude Code plugin API types. The engine writes its own type file only
on a developer machine, so a CI runner cannot read it; the copy is
`mods/claude-fafo/types/claude-code.d.ts`, and its header names the source, the engine build
and the copy date. The check is the `Pane mod type-check` job in `.github/workflows/ci.yml`,
and `CI green` needs it.

## Run it

```
cd mods/claude-fafo
npm ci --ignore-scripts
npx --no-install tsc -p .
```

Exit 0 and no output means it passes. `typescript` is pinned to an exact version in
`package.json`; `package-lock.json` holds the integrity hash. The settings are in
`tsconfig.json` (`strict`, `noUncheckedIndexedAccess`).

## Refresh the vendored types

After a Claude Code update, load the mod once from a folder the engine hot-reloads
(`claude --plugin-dir mods/claude-fafo`). The engine writes the new file to
`mods/claude-fafo/.claude-plugin/types/claude-code/index.d.ts`. Then:

1. Copy it over `mods/claude-fafo/types/claude-code.d.ts`.
2. Replace each em dash with ` - ` (the repo bans em dashes).
3. Put back the header at the top of the file: source, the new engine build (the first line
   of the upstream file names it) and today's date.
4. Run the check above and fix any type errors in the mod. Do not loosen `tsconfig.json`.
