"""Source registry: turn the validated config into the list of enabled, constructed sources.

Fail fast: a misconfigured enabled source raises ConfigError naming it, with no values in the
message. A source whose optional dependency is missing is skipped with a reason. The GitHub
token is never read here: only the NAME of its environment variable is passed on, and the
client reads the value from the real environment at call time. `env` is used only to check
that the variable is set.
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import SOURCE_NAMES, Config, ConfigError, SourceCfg
from .ghapi import GhClient
from .redact import sanitize_text
from .source import Source
from .sources.claude import ClaudeSource
from .sources.codex import CodexSource
from .sources.copilot import CopilotSource
from .sources.gemini import GeminiSource
from .sources.host import HostSource
from .sources.ollama import OllamaSource
from .sources.runners import RunnerHost, RunnersCfg, RunnersSource
from .sshro import SshHost

DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"

Factory = Callable[[SourceCfg, Config, Mapping[str, str]], Source]


class Skip(Exception):
    """Raised by a factory: leave this source out and report why. Not an error."""


@dataclass
class BuildResult:
    sources: list[Source] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # name -> reason
    notes: dict[str, str] = field(default_factory=dict)  # name -> degraded-mode note


def _have_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _need(name: str, key: str, scfg: SourceCfg) -> Any:
    value = scfg.options.get(key)
    if value is None or value == "" or value == []:
        raise ConfigError(f"sources.{name}: '{key}' is required when the source is enabled")
    return value


def _path(text: str) -> Path:
    return Path(os.path.expanduser(text))


def _host(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    if not _have_module("psutil"):
        raise Skip("missing optional dependency: psutil")
    return HostSource()


def _claude(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    d = scfg.options.get("sessions_dir")
    return ClaudeSource(_path(d) if d else None, show_args=set(cfg.show_args))


def _codex(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    return CodexSource(show_args=set(cfg.show_args))


def _gemini(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    return GeminiSource(show_args=set(cfg.show_args))


def _ollama(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    url = _need("ollama", "url", scfg)
    log = scfg.options.get("admission_log")
    try:
        return OllamaSource(str(url), _path(log) if log else None)
    except ValueError:
        raise ConfigError("sources.ollama: 'url' is not an acceptable http or https URL") from None


def _client(scfg: SourceCfg, env: Mapping[str, str]) -> tuple[GhClient | None, str]:
    """A GhClient when the token variable is set, else None. Returns (client, variable NAME)."""
    token_env = scfg.options.get("token_env", DEFAULT_TOKEN_ENV)
    if not env.get(token_env):
        return None, token_env
    return GhClient(token_env), token_env


def _runners(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    repos = list(_need("runners", "repos", scfg))
    hosts: list[RunnerHost] = []
    for h in scfg.options.get("hosts", []):
        if "key_path" in h and "ssh" not in h:
            raise ConfigError("sources.runners.hosts: 'key_path' needs an 'ssh' alias")
        ssh = None
        if "ssh" in h:
            kp = h.get("key_path")
            ssh = SshHost(alias=h["ssh"], key_path=os.path.expanduser(kp) if kp else None)
        hosts.append(RunnerHost(name=h["name"], ssh=ssh, repos=h.get("repos")))
    gh, token_env = _client(scfg, env)
    rcfg = RunnersCfg(repos=repos, runners=hosts, token_env=token_env)
    return RunnersSource(rcfg, gh)


def _copilot(scfg: SourceCfg, cfg: Config, env: Mapping[str, str]) -> Source:
    repos = list(_need("copilot", "repos", scfg))
    gh, token_env = _client(scfg, env)
    if gh is None:
        raise Skip(f"environment variable {token_env} is not set")
    return CopilotSource(gh, repos, session_paths=frozenset(scfg.options.get("session_paths", [])))


SOURCE_FACTORIES: dict[str, Factory] = {
    "host": _host,
    "claude": _claude,
    "codex": _codex,
    "gemini": _gemini,
    "copilot": _copilot,
    "ollama": _ollama,
    "runners": _runners,
}


_NAME_OK = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _class_name(obj: object) -> str:
    return sanitize_text(type(obj).__name__, 40)


def _source_name(src: object) -> str | None:
    """The source's name if `src` looks like a Source, else None. Never raises."""
    try:
        name = src.name
        ok = isinstance(name, str) and _NAME_OK.fullmatch(name) is not None
        return name if ok and callable(getattr(src, "collect", None)) else None
    except Exception:  # noqa: BLE001 - a hostile or broken object is just "invalid"
        return None


def build(
    cfg: Config, env: Mapping[str, str], factories: Mapping[str, Factory] | None = None
) -> BuildResult:
    table: dict[str, Factory] = dict(SOURCE_FACTORIES)
    if factories:
        table.update(factories)
    for name in cfg.sources:
        if name not in table:
            raise ConfigError(f"unknown source '{name}'")
    res = BuildResult()
    seen: set[str] = set()
    built: list[tuple[str, str]] = []
    order = [n for n in SOURCE_NAMES if n in cfg.sources]
    order += [n for n in cfg.sources if n not in SOURCE_NAMES]
    for name in order:
        scfg = cfg.sources[name]
        if not scfg.enabled:
            continue
        failed = ""
        src = None
        try:
            src = table[name](scfg, cfg, env)
        except Skip as why:
            res.skipped[name] = str(why)
            continue
        except ImportError as exc:
            res.skipped[name] = f"missing optional dependency: {exc.name or 'unknown'}"
            continue
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - any factory failure is a config problem
            failed = type(exc).__name__  # the class name only: the text may hold a value
        if failed:
            # Raised outside the except block so the original exception is not chained.
            raise ConfigError(f"sources.{name}: invalid configuration ({failed})")
        ok_name = _source_name(src)
        if ok_name is None:
            raise ConfigError(f"sources.{name}: invalid source object ({_class_name(src)})")
        if ok_name in seen:
            raise ConfigError(f"duplicate source name '{ok_name}'")
        seen.add(ok_name)
        built.append((name, ok_name))
        res.sources.append(src)
        if name == "runners" and getattr(src, "gh", 1) is None:
            res.notes[name] = "GitHub token variable not set: job data unavailable, ssh only"
    for name, built_name in built:
        if built_name != name:
            raise ConfigError(f"sources.{name}: built a source with a different name")
    return res


def build_sources(
    cfg: Config, env: Mapping[str, str], factories: Mapping[str, Factory] | None = None
) -> list[Source]:
    return build(cfg, env, factories).sources
