"""Source registry: builds enabled sources from the config, fails fast, never leaks the token."""

import dataclasses
import logging
from pathlib import Path

import pytest

from fafo import registry
from fafo.config import Config, ConfigError, SourceCfg, load_config
from fafo.registry import build, build_sources
from fafo.source import Ctx, SourceResult

SENTINEL = "SENTINEL-TOKEN-VALUE-0123456789"

SAMPLE = """
[sources.host]
[sources.claude]
sessions_dir = "SESSIONS"
[sources.codex]
[sources.gemini]
[sources.ollama]
url = "http://ollama.example.invalid:11434"
admission_log = "/var/log/example-admission.log"
[sources.runners]
repos = ["example-org/example-repo"]
token_env = "FAFO_TEST_GH"
[[sources.runners.hosts]]
name = "runner-1"
ssh = "runner-1.example.invalid"
key_path = "/home/example/.ssh/id_example"
[sources.copilot]
repos = ["example-org/example-repo"]
token_env = "FAFO_TEST_GH"
"""


def cfg_from(tmp_path: Path, text: str) -> Config:
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def only(tmp_path: Path, enabled: str, body: str = "") -> Config:
    """A config with every source disabled except `enabled`, which gets `body`."""
    names = ("host", "claude", "codex", "gemini", "ollama", "runners", "copilot")
    text = "".join(f"[sources.{n}]\nenabled = false\n" for n in names if n != enabled)
    return cfg_from(tmp_path, text + f"[sources.{enabled}]\n{body}")


class Stub:
    def __init__(self, name: str, **kw):
        self.name = name
        self.kw = kw

    def collect(self, ctx: Ctx) -> SourceResult:
        return SourceResult()


def stub_factories(calls: list):
    def make(name):
        def factory(scfg, cfg, env):
            calls.append((name, scfg))
            return Stub(name)

        return factory

    return {n: make(n) for n in registry.SOURCE_FACTORIES}


def test_sample_builds_every_source_with_stub_factories(tmp_path):
    calls: list = []
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    got = build_sources(cfg, {"FAFO_TEST_GH": SENTINEL}, factories=stub_factories(calls))
    assert [s.name for s in got] == [
        "host", "claude", "codex", "gemini", "copilot", "ollama", "runners",
    ]  # fmt: skip
    assert {n for n, _ in calls} == {s.name for s in got}


def test_disabled_sources_are_not_built(tmp_path):
    calls: list = []
    cfg = only(tmp_path, "codex")
    got = build_sources(cfg, {}, factories=stub_factories(calls))
    assert [s.name for s in got] == ["codex"]
    assert [n for n, _ in calls] == ["codex"]


def test_real_factories_build_all_without_io(tmp_path):
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    got = build_sources(cfg, {"FAFO_TEST_GH": SENTINEL})
    assert sorted(s.name for s in got) == sorted(
        ["host", "claude", "codex", "gemini", "copilot", "ollama", "runners"]
    )


def test_options_and_show_args_reach_the_sources(tmp_path):
    cfg = cfg_from(
        tmp_path,
        'show_args = ["exec", "--zzz"]\n'
        + SAMPLE.replace("SESSIONS", str(tmp_path / "sess")),
    )
    by = {s.name: s for s in build_sources(cfg, {"FAFO_TEST_GH": SENTINEL})}
    assert by["claude"].dir == tmp_path / "sess"
    assert by["claude"].show_args == {"exec", "--zzz"}
    assert by["codex"]._show_args == {"exec", "--zzz"}
    assert by["gemini"]._show_args == {"exec", "--zzz"}
    assert by["ollama"]._url == "http://ollama.example.invalid:11434"
    assert by["ollama"]._log == Path("/var/log/example-admission.log")
    r = by["runners"]
    assert r.cfg.repos == ["example-org/example-repo"]
    assert r.cfg.token_env == "FAFO_TEST_GH"
    assert r.cfg.runners[0].name == "runner-1"
    assert r.cfg.runners[0].ssh.alias == "runner-1.example.invalid"
    assert r.cfg.runners[0].ssh.key_path == "/home/example/.ssh/id_example"
    assert r.gh is not None
    assert by["copilot"]._repos == ["example-org/example-repo"]
    assert by["copilot"]._runs_api is not None


# --- fail fast, naming the source, never echoing a value -------------------------------------


@pytest.mark.parametrize(
    "source,body",
    [
        ("ollama", ""),
        ("ollama", 'url = "ftp://x.invalid"\n'),
        ("ollama", 'url = "http://user:hunter2@x.invalid"\n'),
        ("ollama", 'url = "http://x.invalid/?k=hunter2"\n'),
        ("runners", ""),
        ("runners", "repos = []\n"),
        ("runners", 'repos = ["a/b"]\n[[sources.runners.hosts]]\nname = "x"\nkey_path = "/k"\n'),
        ("copilot", ""),
    ],
)
def test_misconfiguration_raises_config_error_naming_source(tmp_path, source, body):
    cfg = only(tmp_path, source, body)
    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {})
    msg = str(ei.value)
    assert f"sources.{source}" in msg
    assert "hunter2" not in msg


def test_unknown_source_name_raises(tmp_path):
    base = load_config(None)
    cfg = dataclasses.replace(base, sources={**base.sources, "bogus": SourceCfg()})
    with pytest.raises(ConfigError, match="bogus"):
        build_sources(cfg, {})


def test_duplicate_source_names_rejected(tmp_path):
    cfg = cfg_from(tmp_path, "[sources.codex]\n[sources.gemini]\n")
    cfg = dataclasses.replace(
        cfg, sources={k: dataclasses.replace(v, enabled=k in ("codex", "gemini")) for k, v in cfg.sources.items()}
    )
    f = {"codex": lambda s, c, e: Stub("gemini"), "gemini": lambda s, c, e: Stub("gemini")}
    with pytest.raises(ConfigError, match="duplicate"):
        build_sources(cfg, {}, factories=f)


def test_factory_returning_wrong_name_rejected(tmp_path):
    cfg = only(tmp_path, "codex")
    with pytest.raises(ConfigError, match="codex"):
        build_sources(cfg, {}, factories={"codex": lambda s, c, e: Stub("other")})


def test_duplicate_toml_table_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        cfg_from(tmp_path, "[sources.codex]\n[sources.codex]\n")


def test_unexpected_factory_error_becomes_config_error_without_value(tmp_path):
    cfg = only(tmp_path, "codex")

    def boom(s, c, e):
        raise ValueError("value hunter2 leaked")

    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {}, factories={"codex": boom})
    assert "sources.codex" in str(ei.value)
    assert "hunter2" not in str(ei.value)


# --- missing optional dependency: skipped with a reason --------------------------------------


def test_missing_dependency_is_skipped_with_reason(tmp_path):
    cfg = only(tmp_path, "host")

    def nodep(s, c, e):
        raise ImportError("no psutil", name="psutil")

    res = build(cfg, {}, factories={"host": nodep})
    assert res.sources == []
    assert "psutil" in res.skipped["host"]


def test_real_host_factory_skips_when_psutil_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_have_module", lambda name: False)
    res = build(only(tmp_path, "host"), {})
    assert res.sources == []
    assert "psutil" in res.skipped["host"]


def test_missing_token_env_value_runners_ssh_only_copilot_skipped(tmp_path):
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    res = build(cfg, {})  # FAFO_TEST_GH not set
    by = {s.name: s for s in res.sources}
    assert by["runners"].gh is None
    assert "copilot" not in by
    assert "FAFO_TEST_GH" in res.skipped["copilot"]


# --- the token value never appears ----------------------------------------------------------


def test_token_name_is_read_value_never_copied(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    res = build(cfg, {"FAFO_TEST_GH": SENTINEL})
    by = {s.name: s for s in res.sources}
    assert by["runners"].gh is not None  # name was looked up in env: a client exists
    blob = repr(res) + repr(cfg) + caplog.text
    for s in res.sources:
        blob += repr(vars(s)) + repr(getattr(s, "gh", None))
        gh = getattr(s, "gh", None)
        if gh is not None:
            blob += repr(vars(gh))
    assert SENTINEL not in blob
    assert "FAFO_TEST_GH" in blob  # only the NAME is held


def test_token_value_not_in_config_error_messages(tmp_path):
    cfg = only(tmp_path, "runners", "")
    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {"FAFO_TEST_GH": SENTINEL})
    assert SENTINEL not in str(ei.value)


def test_no_config_builds_exactly_the_four_local_sources():
    res = build(load_config(None), {})
    assert sorted(s.name for s in res.sources) == ["claude", "codex", "gemini", "host"]
    assert res.skipped == {}


def test_enabling_ollama_without_url_names_source_and_option(tmp_path):
    cfg = cfg_from(tmp_path, "[sources.ollama]\nenabled = true\n")
    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {})
    assert "ollama" in str(ei.value) and "url" in str(ei.value)


@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_enabling_without_repos_names_source_and_option(tmp_path, source):
    cfg = cfg_from(tmp_path, f"[sources.{source}]\nenabled = true\n")
    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {})
    assert source in str(ei.value) and "repos" in str(ei.value)


def test_runners_with_repos_and_no_token_env_var_still_builds(tmp_path):
    cfg = cfg_from(tmp_path, '[sources.runners]\nrepos = ["owner/name"]\n')
    res = build(cfg, {})
    assert "runners" in [s.name for s in res.sources]
    assert "runners" in res.notes


@pytest.mark.parametrize("exc", [RuntimeError, AttributeError, KeyError, ValueError, OSError, ZeroDivisionError])
def test_any_factory_failure_becomes_value_free_config_error(tmp_path, exc, caplog):
    cfg = only(tmp_path, "codex")

    def boom(s, c, e):
        raise exc("SENTINEL-SECRET-VALUE", "SENTINEL-ARG")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConfigError) as ei:
        build_sources(cfg, {}, factories={"codex": boom})
    err = ei.value
    assert "sources.codex" in str(err)
    assert exc.__name__ in str(err)
    seen, e = [], err
    while e is not None:
        seen.append(e)
        e = e.__cause__ or e.__context__
    assert len(seen) == 1  # nothing chained: the original exception is not reachable
    blob = " ".join(str(x) + repr(x) + repr(x.args) for x in seen) + caplog.text
    assert "SENTINEL" not in blob


def test_runners_gets_token_env_name_and_sends_bearer_token(tmp_path, monkeypatch, caplog):
    """Registry-built real GhClient, fake transport: the header is seen only there."""
    from fafo import ghapi

    sent: list[tuple[str, str, dict]] = []

    def fake_http(method, url, headers, timeout):
        sent.append((method, url, dict(headers)))
        return 200, {}, b'{"workflow_runs": []}'

    monkeypatch.setattr(ghapi, "_httpx_http", fake_http)
    monkeypatch.setenv("FAFO_TEST_GH", SENTINEL)
    caplog.set_level(logging.DEBUG)
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    env = {"FAFO_TEST_GH": SENTINEL}
    by = {s.name: s for s in build(cfg, env).sources}
    assert by["runners"].gh._token_env == "FAFO_TEST_GH"
    assert by["runners"].gh is not by["copilot"]._runs_api
    ctx = Ctx(cfg=cfg.sources["runners"], clock=lambda: 0.0, log=logging.getLogger("t"))
    by["runners"].collect(ctx)
    by["copilot"].collect(ctx)
    assert sent
    for method, url, headers in sent:
        assert method == "GET"
        assert url.startswith("https://api.github.com/repos/example-org/example-repo/")
        assert headers["Authorization"] == f"Bearer {SENTINEL}"
    blob = caplog.text + repr(by) + repr(vars(by["runners"].gh))
    assert SENTINEL not in blob


def test_token_failure_in_transport_does_not_leak(tmp_path, monkeypatch, caplog):
    from fafo import ghapi

    def failing(method, url, headers, timeout):
        raise OSError("boom " + headers["Authorization"])

    monkeypatch.setattr(ghapi, "_httpx_http", failing)
    monkeypatch.setenv("FAFO_TEST_GH", SENTINEL)
    caplog.set_level(logging.DEBUG)
    cfg = cfg_from(tmp_path, SAMPLE.replace("SESSIONS", str(tmp_path)))
    src = {s.name: s for s in build(cfg, {"FAFO_TEST_GH": SENTINEL}).sources}["runners"]
    ctx = Ctx(cfg=cfg.sources["runners"], clock=lambda: 0.0, log=logging.getLogger("t"))
    with pytest.raises(Exception) as ei:
        src.collect(ctx)
    e, texts = ei.value, []
    while e is not None:
        texts.append(str(e) + repr(e))
        e = e.__cause__ or e.__context__
    assert SENTINEL not in " ".join(texts) + caplog.text
