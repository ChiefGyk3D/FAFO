# systemd user unit

`fafo.service` runs `fafo run` (the collector) as a systemd user service.

```
mkdir -p ~/.config/systemd/user
cp examples/systemd/fafo.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fafo
```

Then `fafo table` shows what the collector sees. Logs: `journalctl --user -u fafo`.

Notes:

- The unit expects the `fafo` command at `~/.local/bin/fafo`. Edit `ExecStart=` if it is
  elsewhere.
- The snapshot is written to `$XDG_RUNTIME_DIR/fafo/snapshot.json`, which matches the default
  of `fafo table`, `fafo tui` and the others.
- The unit holds no secrets. A source that needs a token reads it from an environment variable
  named in your config (`token_env`); set that variable in a private drop-in
  (`systemctl --user edit fafo`) or an `EnvironmentFile` with mode 0600, never in this file.
- `ProtectHome=` is not set, because FAFO must read `~/.claude/sessions`.
- `MemoryDenyWriteExecute=` is not set, because it can break Python.
- Hardening is for user units only; for a system unit, review each option first.
