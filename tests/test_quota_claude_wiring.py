"""claude_quota wiring: config option, registry factory, quota line rendering."""

from __future__ import annotations

import pytest
from test_render_view import NOW, snap

from fafo.config import ConfigError, load_config
from fafo.registry import build
from fafo.render.view import build_views
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
