from pathlib import Path

import pytest

from fafo.config import Config, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_load_with_no_file(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert {"host", "claude", "ollama", "runners"} <= set(cfg.sources)
    for s in cfg.sources.values():
        assert s.enabled is True
        assert s.interval_s > 0 and s.timeout_s > 0
    assert cfg.aliases == {}
    assert "exec" in cfg.show_args
    assert cfg.snapshot_path == Path("/run/user/1000/fafo/snapshot.json")


def test_snapshot_default_falls_back_to_state_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    cfg = load_config(None)
    assert cfg.snapshot_path == Path.home() / ".local/state/fafo/snapshot.json"


def test_snapshot_default_resolved_at_load_time(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/a")
    first = load_config(None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/b")
    second = load_config(None)
    assert first.snapshot_path == Path("/run/a/fafo/snapshot.json")
    assert second.snapshot_path == Path("/run/b/fafo/snapshot.json")


def test_explicit_snapshot_path_and_overrides(tmp_path):
    p = write(
        tmp_path,
        'snapshot_path = "~/x/snap.json"\nshow_args = ["exec"]\n'
        "[sources.ollama]\nenabled = false\ninterval_s = 5\ntimeout_s = 2.5\n",
    )
    cfg = load_config(p)
    assert cfg.snapshot_path == Path.home() / "x/snap.json"
    assert cfg.show_args == {"exec"}
    assert cfg.sources["ollama"].enabled is False
    assert cfg.sources["ollama"].interval_s == 5
    assert cfg.sources["ollama"].timeout_s == 2.5
    assert cfg.sources["host"].enabled is True


def test_unknown_top_level_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="bogus_option"):
        load_config(write(tmp_path, "bogus_option = 1\n"))


def test_unknown_source_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="frequency"):
        load_config(write(tmp_path, "[sources.host]\nfrequency = 1\n"))


def test_unknown_source_name_rejected(tmp_path):
    with pytest.raises(ConfigError, match="nosuchsource"):
        load_config(write(tmp_path, "[sources.nosuchsource]\nenabled = true\n"))


@pytest.mark.parametrize("key", ["token", "password", "secret", "key", "api_key", "GH_TOKEN"])
def test_secret_looking_key_rejected(tmp_path, key):
    with pytest.raises(ConfigError, match="environment variable"):
        load_config(write(tmp_path, f'{key} = "x"\n'))


def test_secret_looking_key_nested_rejected(tmp_path):
    with pytest.raises(ConfigError, match="environment variable"):
        load_config(write(tmp_path, '[sources.ollama]\nauth_token = "x"\n'))


def test_secret_check_runs_before_unknown_key_check(tmp_path):
    with pytest.raises(ConfigError, match="environment variable"):
        load_config(write(tmp_path, 'bogus = 1\npassword = "x"\n'))


def test_alias_mapping_applied(tmp_path):
    p = write(
        tmp_path,
        '[aliases]\n"real-host.invalid" = "runner-1"\n"192.0.2.7" = "ai-server"\n',
    )
    cfg = load_config(p)
    out = cfg.alias("ssh real-host.invalid via 192.0.2.7, then 192.0.2.77")
    assert out == "ssh runner-1 via ai-server, then 192.0.2.77"


def test_alias_longest_match_first(tmp_path):
    p = write(tmp_path, '[aliases]\n"a.invalid" = "A"\n"b.a.invalid" = "B"\n')
    assert load_config(p).alias("b.a.invalid") == "B"


def test_alias_with_no_aliases_is_identity():
    assert load_config(None).alias("anything 10.0.0.1") == "anything 10.0.0.1"


def test_alias_value_must_be_string(tmp_path):
    with pytest.raises(ConfigError, match="aliases"):
        load_config(write(tmp_path, '[aliases]\n"h.invalid" = 5\n'))


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", '"fast"', "true", "nan", "inf"])
@pytest.mark.parametrize("field", ["interval_s", "timeout_s"])
def test_invalid_interval_or_timeout_rejected(tmp_path, field, bad):
    with pytest.raises(ConfigError, match=field):
        load_config(write(tmp_path, f"[sources.host]\n{field} = {bad}\n"))


def test_missing_explicit_path_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "this is = = not toml"))


def test_example_config_loads_and_has_only_placeholders():
    root = Path(__file__).resolve().parent.parent
    text = (root / "config.example.toml").read_text(encoding="utf-8")
    cfg = load_config(root / "config.example.toml")
    assert cfg.aliases
    assert chr(0x2014) not in text
    for host in cfg.aliases:
        assert host.endswith(".invalid") or host.startswith(("192.0.2.", "198.51.100."))


@pytest.mark.parametrize(
    "variant",
    ["realhost.example.invalid", "REALHOST.EXAMPLE.INVALID", "RealHost.Example.Invalid"],
)
def test_alias_matches_any_case(tmp_path, variant):
    cfg = load_config(write(tmp_path, '[aliases]\n"realhost.example.invalid" = "Runner-1"\n'))
    assert cfg.alias(f"ssh {variant} ok") == "ssh Runner-1 ok"


def test_alias_case_insensitive_longest_match_first(tmp_path):
    p = write(tmp_path, '[aliases]\n"a.invalid" = "A"\n"B.A.invalid" = "B"\n')
    assert load_config(p).alias("b.a.INVALID and A.Invalid") == "B and A"


def test_alias_boundary_prefix_not_matched(tmp_path):
    cfg = load_config(write(tmp_path, '[aliases]\n"192.0.2.7" = "x"\n'))
    assert cfg.alias("10.192.0.2.7 and xhost-192.0.2.7") == "10.192.0.2.7 and xhost-192.0.2.7"
    assert cfg.alias("192.0.2.77") == "192.0.2.77"


@pytest.mark.parametrize("key", ['""', '"   "', '"\\t"'])
def test_empty_or_blank_alias_key_rejected(tmp_path, key):
    with pytest.raises(ConfigError, match="aliases"):
        load_config(write(tmp_path, f'[aliases]\n{key} = "x"\n'))


def test_case_duplicate_alias_keys_rejected(tmp_path):
    p = write(tmp_path, '[aliases]\n"Host.invalid" = "a"\n"host.INVALID" = "b"\n')
    with pytest.raises(ConfigError, match="case"):
        load_config(p)


@pytest.mark.parametrize("cp", [0x130, 0x131, 0x17F, 0x212A])
def test_unicode_lookalikes_never_raise_and_never_match(tmp_path, cp):
    cfg = load_config(write(tmp_path, '[aliases]\n"fileserver.example.invalid" = "fs"\n'))
    # Replace each ASCII letter that has a Unicode case-fold lookalike in turn.
    text = "fileserver.example.invalid"
    for i, ch in enumerate(text):
        if ch.isalpha():
            lookalike = text[:i] + chr(cp) + text[i + 1 :]
            assert cfg.alias(lookalike) == lookalike
    assert cfg.alias("f" + chr(0x130) + "leserver.example.invalid") == (
        "f" + chr(0x130) + "leserver.example.invalid"
    )
    assert cfg.alias("see " + text + " " + chr(cp)) == "see fs " + chr(cp)


def test_non_ascii_alias_key_rejected(tmp_path):
    p = write(tmp_path, '[aliases]\n"f' + chr(0xE9) + 'server.invalid" = "fs"\n')
    with pytest.raises(ConfigError, match="ASCII"):
        load_config(p)


def test_punycode_alias_key_accepted(tmp_path):
    cfg = load_config(write(tmp_path, '[aliases]\n"xn--fserver-bya.invalid" = "fs"\n'))
    assert cfg.alias("XN--FSERVER-BYA.invalid") == "fs"


def test_alias_lookup_miss_yields_placeholder_not_raw_text(tmp_path):
    cfg = load_config(write(tmp_path, '[aliases]\n"h.invalid" = "x"\n'))
    object.__setattr__(cfg, "_alias_map", {})
    assert cfg.alias("see h.invalid") == "see <host>"
