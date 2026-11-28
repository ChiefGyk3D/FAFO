"""claude_quota wiring: config option, registry factory, quota line rendering."""

from __future__ import annotations

import stat

import pytest
from test_render_view import NOW, snap
from test_sources_quota_claude import _fake_stat, put

from fafo import cli
from fafo.config import ConfigError, load_config
from fafo.registry import build
from fafo.render.view import build_views
from fafo.sources import quota_claude
from fafo.sources.quota_claude import ClaudeQuotaSource


def cfg_from(tmp_path, text):
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def test_off_by_default(tmp_path):
    cfg = cfg_from(tmp_path, "")
    assert cfg.sources["claude_quota"].enabled is False
    assert "claude_quota" not in [s.name for s in build(cfg, {}).sources]


def test_option_builds_source(tmp_path):
    cfg = cfg_from(tmp_path, '[sources.claude_quota]\nenabled = true\nusage_cache = "~/x/u.json"\n')
    assert cfg.sources["claude_quota"].options == {"usage_cache": "~/x/u.json"}
    src = [s for s in build(cfg, {}).sources if s.name == "claude_quota"]
    assert len(src) == 1 and isinstance(src[0], ClaudeQuotaSource)


def test_enabled_without_path_is_a_config_error(tmp_path):
    cfg = cfg_from(tmp_path, "[sources.claude_quota]\nenabled = true\n")
    with pytest.raises(ConfigError, match="usage_cache"):
        build(cfg, {})


def test_bad_option_value(tmp_path):
    with pytest.raises(ConfigError, match="usage_cache"):
        cfg_from(tmp_path, "[sources.claude_quota]\nusage_cache = 5\n")


def _win(pct, resets, stale=False, as_of=NOW - 10):
    return {"used_pct": pct, "resets_in_s": resets, "as_of": as_of, "stale": stale}


def test_view_renders_windows():
    q = {"claude": {"five_hour": _win(41.4, 7800), "weekly": _win(63.0, 3 * 86400)}}
    v = build_views(snap(quota=q), NOW)
    assert v.quota[0].text == "five_hour 41% resets 2h10m  weekly 63% resets 3d"


def test_view_marks_stale_with_original_age():
    q = {"claude": {"weekly": _win(63.0, None, stale=True, as_of=NOW - 7200)}}
    v = build_views(snap(quota=q), NOW)
    assert v.quota[0].text == "weekly 63% stale 2h"


def test_view_hostile_window_does_not_raise():
    q = {"claude": {"weekly": {"used_pct": float("nan"), "resets_in_s": "x", "as_of": None}}}
    v = build_views(snap(quota=q), NOW)
    assert len(v.quota) == 1


# --- doctor shows the cache reason word only -------------------------------------------------

_OFF = ("host", "claude", "codex", "gemini", "copilot", "ollama", "runners")


def _doctor_cfg(tmp_path, cache):
    text = "".join(f"[sources.{n}]\nenabled = false\n" for n in _OFF)
    text += f'[sources.claude_quota]\nenabled = true\ninterval_s = 0.2\ntimeout_s = 5\nusage_cache = "{cache}"\n'
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("reason", "payload", "secret"),
    [
        ("corrupt", b'{"windows": [ SECRET-CORRUPT-TEXT', "SECRET-CORRUPT-TEXT"),
        ("schema", b'{"windows":[{"label":"5h","used_percent":"SECRET-SCHEMA-9"}]}', "SECRET-SCHEMA-9"),
        ("refused", b'{"windows":[]}', None),
    ],
)
def test_doctor_shows_reason_word_and_no_input_text(tmp_path, capsys, monkeypatch, reason, payload, secret):
    cache_dir = tmp_path / "cachedir"
    cache_dir.mkdir(mode=0o700)
    cache = cache_dir / "usage.json"
    put(cache, payload)  # owner-only on disk; no permissive chmod
    if reason == "refused":
        _fake_stat(monkeypatch, regular=True, mode=stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR | stat.S_IWOTH)
    code = cli.main(["doctor", "--config", str(_doctor_cfg(tmp_path, cache))])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("claude_quota ")]
    assert len(lines) == 1, out
    assert lines[0] == f"claude_quota down QuotaCacheError: {reason}"
    assert code == 1
    assert "SECRET" not in out
    assert str(tmp_path) not in out


def test_error_detail_shows_reason_word_for_cache_error(tmp_path):
    cfg = cfg_from(tmp_path, "")
    assert cli._error_detail(cfg, quota_claude.QuotaCacheError("refused")) == "QuotaCacheError: refused"


def test_error_detail_hostile_cache_message_falls_back_to_class(tmp_path):
    cfg = cfg_from(tmp_path, "")
    exc = quota_claude.QuotaCacheError("refused")
    exc.args = ("HOSTILE /home/x/secret-token-value",)  # bypasses the constructor on purpose
    detail = cli._error_detail(cfg, exc)
    assert detail == "QuotaCacheError"
    assert "HOSTILE" not in detail and "secret" not in detail
