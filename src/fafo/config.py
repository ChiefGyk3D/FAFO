"""Configuration loader. TOML only; secrets are never read from the file."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import ARGS_MODES
from .repospec import valid_repo

SOURCE_NAMES = ("host", "claude", "codex", "gemini", "copilot", "ollama", "runners")
# Sources that need no options are on by default; the rest need a table in the file.
DEFAULT_ENABLED = ("host", "claude", "codex", "gemini")
DEFAULT_INTERVAL_S = 2.0
DEFAULT_TIMEOUT_S = 5.0
MAX_SECONDS = 86400
DEFAULT_SHOW_ARGS = ("exec", "resume", "--resume", "--model")
TOP_KEYS = {
    "snapshot_path", "show_args", "args_mode", "sources", "aliases", "collapse_helpers",
    "boring_procs",
}
DEFAULT_BORING_PROCS = (
    "sleep", "tail", "bash", "sh", "zsh", "dash", "cat", "head", "wc", "grep", "timeout",
    "env", "true",
)
MAX_BORING = 64
_PROC_NAME = re.compile(r"[A-Za-z0-9._-]{1,32}")
SOURCE_KEYS = {"enabled", "interval_s", "timeout_s"}
TASK_LABEL_MODES = ("off", "activity", "prompt")
SECRET_WORDS = ("token", "password", "secret", "key")

# Source-specific option keys, per source. Anything else is an unknown key.
OPTION_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset({"sessions_dir", "task_labels"}),
    "ollama": frozenset({"url", "admission_log", "servers"}),
    "runners": frozenset({"repos", "token_env", "token_file", "hosts"}),
    "copilot": frozenset({"repos", "session_paths", "token_env", "token_file"}),
}
HOST_KEYS = frozenset({"name", "ssh", "key_path", "repos"})
MAX_PATH_LEN = 4096
MAX_URL_LEN = 2048
MAX_LIST = 100
MAX_HOSTS = 32
MAX_OLLAMA_SERVERS = 8
SERVER_KEYS = frozenset({"name", "url", "admission_log"})
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_LABEL = re.compile(r"[A-Za-z0-9._@-]{1,100}")  # host name and ssh alias (same as sshro)
_SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_PEM_MARK = "-----" + "BEGIN"


class ConfigError(Exception):
    """Raised for any invalid or unsafe configuration."""


@dataclass(frozen=True)
class SourceCfg:
    enabled: bool = True
    interval_s: float = DEFAULT_INTERVAL_S
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Validated, source-specific options (see OPTION_KEYS). Never holds a secret value.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    sources: dict[str, SourceCfg]
    aliases: dict[str, str]
    snapshot_path: Path
    show_args: set[str]
    collapse_helpers: bool = True
    boring_procs: tuple[str, ...] = DEFAULT_BORING_PROCS
    args_mode: str = "allowlist"
    _alias_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    _alias_map: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def alias(self, text: str) -> str:
        """Replace configured hostnames/IPs with their aliases (longest match first)."""
        if self._alias_re is None:
            return text
        return self._alias_re.sub(
            lambda m: self._alias_map.get(m.group(0).casefold(), "<host>"), text
        )


def _default_snapshot_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".local" / "state"
    return base / "fafo" / "snapshot.json"


def _env_name_ok(value: object) -> bool:
    return isinstance(value, str) and _ENV_NAME.fullmatch(value) is not None


def _path_ok(value: object) -> bool:
    """A plain filesystem path string. It is never opened or read by the config."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PATH_LEN
        and _PEM_MARK not in value
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


# The only secret-word keys that are allowed, by exact location. Each holds a NAME or a path,
# never a secret; _parse_options validates the value (_env_name_ok, _path_ok) for every one.
_ALLOWED_SECRET_KEYS = frozenset(
    {
        "sources.runners.token_env",
        "sources.copilot.token_env",
        "sources.runners.token_file",
        "sources.copilot.token_file",
        "sources.runners.hosts.key_path",
    }
)


def _check_secrets(data: dict, where: str = "", word_split: bool = False) -> None:
    for k, v in data.items():
        low = str(k).lower()
        if word_split:
            # Alias keys are hostnames; match whole words so "turkey.invalid" passes.
            hit = any(w in SECRET_WORDS for w in re.split(r"[^a-z0-9]+", low))
        else:
            hit = any(w in low for w in SECRET_WORDS)
        if hit and f"{where}{k}" not in _ALLOWED_SECRET_KEYS:
            raise ConfigError(
                f"secret-looking key '{where}{k}' in config file: secrets come from "
                "environment variables, never the config file"
            )
        if isinstance(v, dict):
            _check_secrets(v, f"{where}{k}.", word_split or k == "aliases")
        elif isinstance(v, list):
            for item in v:  # arrays of tables, such as [[sources.runners.hosts]]
                if isinstance(item, dict):
                    _check_secrets(item, f"{where}{k}.", word_split)


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a positive number")
    # Compare without converting: a huge TOML integer overflows float().
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{name} must be a positive finite number")
    if value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    if value > MAX_SECONDS:
        raise ConfigError(f"{name} must be at most {MAX_SECONDS} seconds")
    return value


def _bad(name: str, what: str) -> ConfigError:
    return ConfigError(f"{name} {what}")  # never includes the offending value


def _opt_text(value: object, name: str, max_len: int) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= max_len
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise _bad(name, f"must be a non-empty single-line string of at most {max_len} characters")
    return value


def _opt_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not _path_ok(value):
        raise _bad(name, f"must be a plain path string of at most {MAX_PATH_LEN} characters")
    return value


def _opt_abs_path(value: object, name: str) -> str:
    """An absolute path string for a secret file. The config never opens or reads it."""
    if not isinstance(value, str) or not _path_ok(value) or not value.startswith("/"):
        raise _bad(name, f"must be an absolute path string of at most {MAX_PATH_LEN} characters")
    return value


def _opt_repos(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise _bad(name, f"must be a list of at most {MAX_LIST} owner/name strings")
    for item in value:
        if not isinstance(item, str) or not valid_repo(item):
            raise _bad(name, "must contain only owner/name strings")
    return list(value)


def _opt_env(value: object, name: str) -> str:
    if not isinstance(value, str) or not _env_name_ok(value):
        raise _bad(name, "must be an environment variable NAME such as GITHUB_TOKEN")
    return value


def _opt_label(value: object, name: str) -> str:
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None or value.startswith("-"):
        raise _bad(name, "must be 1-100 characters from A-Z a-z 0-9 . _ @ - and not start with -")
    return value


def _parse_hosts(value: object) -> list[dict[str, Any]]:
    name = "sources.runners.hosts"
    if not isinstance(value, list) or len(value) > MAX_HOSTS:
        raise _bad(name, f"must be an array of at most {MAX_HOSTS} tables")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in value:
        if not isinstance(h, dict):
            raise _bad(name, "must be an array of tables")
        for k in h:
            if k not in HOST_KEYS:
                raise ConfigError(f"unknown key '{name}.{k}'")
        if "name" not in h:
            raise _bad(name, "entries need a name")
        item: dict[str, Any] = {"name": _opt_label(h["name"], f"{name}.name")}
        if item["name"].casefold() in seen:
            raise _bad(name, "has a duplicate host name")
        seen.add(item["name"].casefold())
        if "ssh" in h:
            item["ssh"] = _opt_label(h["ssh"], f"{name}.ssh")
        if "key_path" in h:
            item["key_path"] = _opt_path(h["key_path"], f"{name}.key_path")
        if "repos" in h:
            item["repos"] = _opt_repos(h["repos"], f"{name}.repos")
        out.append(item)
    return out


def _parse_ollama_servers(value: object) -> list[dict[str, Any]]:
    name = "sources.ollama.servers"
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OLLAMA_SERVERS:
        raise _bad(name, f"must be 1 to {MAX_OLLAMA_SERVERS} [[{name}]] tables")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in value:
        if not isinstance(s, dict):
            raise _bad(name, "must be an array of tables")
        for k in s:
            if k not in SERVER_KEYS:
                raise ConfigError(f"unknown key '{name}.{k}'")
        if "name" not in s or "url" not in s:
            raise _bad(name, "entries need a name and a url")
        sname = s["name"]
        if not isinstance(sname, str) or _SERVER_NAME.fullmatch(sname) is None:
            raise _bad(f"{name}.name", "must be 1-32 characters from A-Z a-z 0-9 . _ -")
        if sname.casefold() in seen:
            raise _bad(name, "has a duplicate server name")
        seen.add(sname.casefold())
        item: dict[str, Any] = {"name": sname, "url": _opt_text(s["url"], f"{name}.url", MAX_URL_LEN)}
        if "admission_log" in s:
            item["admission_log"] = _opt_path(s["admission_log"], f"{name}.admission_log")
        out.append(item)
    return out


def _parse_options(source: str, body: dict) -> dict[str, Any]:
    p = f"sources.{source}"
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k in SOURCE_KEYS:
            continue
        n = f"{p}.{k}"
        if k in ("sessions_dir", "admission_log"):
            out[k] = _opt_path(v, n)
        elif k == "task_labels":
            if v not in TASK_LABEL_MODES:
                raise _bad(n, "must be one of: " + ", ".join(TASK_LABEL_MODES))
            out[k] = v
        elif k == "url":
            out[k] = _opt_text(v, n, MAX_URL_LEN)
        elif k == "repos":
            out[k] = _opt_repos(v, n)
        elif k == "token_env":
            out[k] = _opt_env(v, n)
        elif k == "token_file":
            out[k] = _opt_abs_path(v, n)
        elif k == "servers":
            out[k] = _parse_ollama_servers(v)
        elif k == "hosts":
            out[k] = _parse_hosts(v)
        elif k == "session_paths":
            if not isinstance(v, list) or len(v) > MAX_LIST:
                raise _bad(n, f"must be a list of at most {MAX_LIST} strings")
            out[k] = [_opt_text(x, n, 200) for x in v]
    if "servers" in out and ("url" in out or "admission_log" in out):
        raise _bad(
            f"{p}.servers",
            "cannot be combined with url or admission_log; give each server its own",
        )
    return out


def _parse_sources(raw: object) -> dict[str, SourceCfg]:
    if not isinstance(raw, dict):
        raise ConfigError("'sources' must be a table")
    out = {name: SourceCfg(enabled=name in DEFAULT_ENABLED) for name in SOURCE_NAMES}
    for name, body in raw.items():
        if name not in SOURCE_NAMES:
            raise ConfigError(f"unknown source '{name}'")
        if not isinstance(body, dict):
            raise ConfigError(f"sources.{name} must be a table")
        allowed = SOURCE_KEYS | OPTION_KEYS.get(name, frozenset())
        for k in body:
            if k not in allowed:
                raise ConfigError(f"unknown key 'sources.{name}.{k}'")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"sources.{name}.enabled must be true or false")
        out[name] = SourceCfg(
            enabled=enabled,
            interval_s=_positive_number(
                body.get("interval_s", DEFAULT_INTERVAL_S), f"sources.{name}.interval_s"
            ),
            timeout_s=_positive_number(
                body.get("timeout_s", DEFAULT_TIMEOUT_S), f"sources.{name}.timeout_s"
            ),
            options=_parse_options(name, body),
        )
    return out


def _compile_aliases(aliases: dict[str, str]) -> re.Pattern[str] | None:
    if not aliases:
        return None
    names = sorted(aliases, key=len, reverse=True)
    body = "|".join(re.escape(n) for n in names)
    # Do not match inside a longer hostname or IP (10.0.0.1 inside 10.0.0.12).
    # Hostnames are case-insensitive, so match any case variant.
    return re.compile(rf"(?<![\w.-])(?:{body})(?![\w-])(?!\.\w)", re.IGNORECASE | re.ASCII)


def _validate_alias_keys(aliases: dict[str, str]) -> dict[str, str]:
    """Return a lowercase-keyed map; reject blank keys and keys that differ only by case."""
    lowered: dict[str, str] = {}
    for k, v in aliases.items():
        if not k.strip():
            raise ConfigError("'aliases' has an empty or blank host key")
        if not k.isascii():
            raise ConfigError(
                f"'aliases' key {k!r} is not ASCII: hostnames must be ASCII (use punycode)"
            )
        if k.casefold() in lowered:
            raise ConfigError(f"'aliases' key '{k}' duplicates another key except for case")
        lowered[k.casefold()] = v
    return lowered


def _parse_helpers(data: dict) -> tuple[bool, tuple[str, ...]]:
    collapse = data.get("collapse_helpers", True)
    if not isinstance(collapse, bool):
        raise _bad("collapse_helpers", "must be true or false")
    raw = data.get("boring_procs", list(DEFAULT_BORING_PROCS))
    if not isinstance(raw, list) or len(raw) > MAX_BORING:
        raise _bad("boring_procs", f"must be a list of at most {MAX_BORING} program names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or _PROC_NAME.fullmatch(item) is None:
            raise _bad("boring_procs", "must contain only names of 1-32 characters from A-Z a-z 0-9 . _ -")
        if item not in names:
            names.append(item)
    return collapse, tuple(names)


def load_config(path: Path | None) -> Config:
    data: dict = {}
    if path is not None:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from None

    _check_secrets(data)
    for k in data:
        if k not in TOP_KEYS:
            raise ConfigError(f"unknown config key '{k}'")

    sources = _parse_sources(data.get("sources", {}))

    aliases = data.get("aliases", {})
    if not isinstance(aliases, dict) or not all(isinstance(v, str) for v in aliases.values()):
        raise ConfigError("'aliases' must be a table of host = \"alias\" strings")

    alias_map = _validate_alias_keys(aliases)

    show = data.get("show_args", list(DEFAULT_SHOW_ARGS))
    if not isinstance(show, list) or not all(isinstance(a, str) for a in show):
        raise ConfigError("'show_args' must be a list of strings")

    collapse_helpers, boring_procs = _parse_helpers(data)
    args_mode = data.get("args_mode", "allowlist")
    if not isinstance(args_mode, str) or args_mode not in ARGS_MODES:
        raise ConfigError("'args_mode' must be \"allowlist\" or \"safe\"")

    snap = data.get("snapshot_path")
    if snap is None:
        snapshot_path = _default_snapshot_path()
    elif isinstance(snap, str) and snap:
        snapshot_path = Path(os.path.expandvars(snap)).expanduser()
    else:
        raise ConfigError("'snapshot_path' must be a non-empty string")

    return Config(
        sources=sources,
        aliases=dict(aliases),
        snapshot_path=snapshot_path,
        show_args=set(show),
        collapse_helpers=collapse_helpers,
        boring_procs=boring_procs,
        args_mode=args_mode,
        _alias_re=_compile_aliases(aliases),
        _alias_map=alias_map,
    )
