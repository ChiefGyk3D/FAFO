"""Configuration loader. TOML only; secrets are never read from the file."""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_NAMES = ("host", "claude", "codex", "gemini", "copilot", "ollama", "runners")
DEFAULT_INTERVAL_S = 2.0
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_SHOW_ARGS = ("exec", "resume", "--resume", "--model")
TOP_KEYS = {"snapshot_path", "show_args", "sources", "aliases"}
SOURCE_KEYS = {"enabled", "interval_s", "timeout_s"}
SECRET_WORDS = ("token", "password", "secret", "key")


class ConfigError(Exception):
    """Raised for any invalid or unsafe configuration."""


@dataclass(frozen=True)
class SourceCfg:
    enabled: bool = True
    interval_s: float = DEFAULT_INTERVAL_S
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Config:
    sources: dict[str, SourceCfg]
    aliases: dict[str, str]
    snapshot_path: Path
    show_args: set[str]
    _alias_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    _alias_map: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def alias(self, text: str) -> str:
        """Replace configured hostnames/IPs with their aliases (longest match first)."""
        if self._alias_re is None:
            return text
        return self._alias_re.sub(lambda m: self._alias_map[m.group(0).lower()], text)


def _default_snapshot_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".local" / "state"
    return base / "fafo" / "snapshot.json"


def _check_secrets(data: dict, where: str = "", word_split: bool = False) -> None:
    for k, v in data.items():
        low = str(k).lower()
        if word_split:
            # Alias keys are hostnames; match whole words so "turkey.invalid" passes.
            hit = any(w in SECRET_WORDS for w in re.split(r"[^a-z0-9]+", low))
        else:
            hit = any(w in low for w in SECRET_WORDS)
        if hit:
            raise ConfigError(
                f"secret-looking key '{where}{k}' in config file: secrets come from "
                "environment variables, never the config file"
            )
        if isinstance(v, dict):
            _check_secrets(v, f"{where}{k}.", word_split or k == "aliases")


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a positive number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number, got {value!r}")
    return value


def _parse_sources(raw: object) -> dict[str, SourceCfg]:
    if not isinstance(raw, dict):
        raise ConfigError("'sources' must be a table")
    out = {name: SourceCfg() for name in SOURCE_NAMES}
    for name, body in raw.items():
        if name not in SOURCE_NAMES:
            raise ConfigError(f"unknown source '{name}'")
        if not isinstance(body, dict):
            raise ConfigError(f"sources.{name} must be a table")
        for k in body:
            if k not in SOURCE_KEYS:
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
        )
    return out


def _compile_aliases(aliases: dict[str, str]) -> re.Pattern[str] | None:
    if not aliases:
        return None
    names = sorted(aliases, key=len, reverse=True)
    body = "|".join(re.escape(n) for n in names)
    # Do not match inside a longer hostname or IP (10.0.0.1 inside 10.0.0.12).
    # Hostnames are case-insensitive, so match any case variant.
    return re.compile(rf"(?<![\w.-])(?:{body})(?![\w-])(?!\.\w)", re.IGNORECASE)


def _validate_alias_keys(aliases: dict[str, str]) -> dict[str, str]:
    """Return a lowercase-keyed map; reject blank keys and keys that differ only by case."""
    lowered: dict[str, str] = {}
    for k, v in aliases.items():
        if not k.strip():
            raise ConfigError("'aliases' has an empty or blank host key")
        if k.lower() in lowered:
            raise ConfigError(f"'aliases' key '{k}' duplicates another key except for case")
        lowered[k.lower()] = v
    return lowered


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
        _alias_re=_compile_aliases(aliases),
        _alias_map=alias_map,
    )
